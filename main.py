"""
TG-Gifts Analytics — сканер маркета + живой админ-бот.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
import warnings
from typing import Optional

from config import Settings, get_settings
from bot.admin import ADMIN_ID
from bot.app import build_bot, build_dispatcher
from bot.claims import ClaimStore
from bot.logger import GiftLogger
from core.filters import ProfileFilter
from core.market import GiftMarketScanner
from core.parser import ProfileScanner
from core.runtime import LiveFilters
from core.storage import Storage
from core.ton_client import TonMarketClient

LOGGER = logging.getLogger("tg_gifts")
BUILD = "20260910-7"


def setup_logging() -> None:
    os.environ["PYTHONUNBUFFERED"] = "1"
    try:
        sys.stdout.reconfigure(line_buffering=True, write_through=True)
        sys.stderr.reconfigure(line_buffering=True, write_through=True)
    except Exception:
        pass

    class _Flush(logging.StreamHandler):
        def emit(self, record: logging.LogRecord) -> None:
            super().emit(record)
            self.flush()

    handler = _Flush(sys.stderr)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    logging.getLogger("telethon").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    logging.getLogger("aiogram").setLevel(logging.WARNING)


class AnalyticsApp:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.live = LiveFilters.from_settings(settings)
        self.storage = Storage(settings.database_url, settings.redis_url)
        self.market = TonMarketClient(settings, self.storage)
        self.scanner = ProfileScanner(settings, self.storage, self.market)
        self.markets = GiftMarketScanner(self.scanner, self.market, settings, self.live)
        self.filters = ProfileFilter(self.live)
        self.bot = build_bot(settings)
        self.claims = ClaimStore()
        self.dispatcher = build_dispatcher(self.live, self.claims)
        self.logger_bot = GiftLogger(self.bot, settings.log_group_id, self.live, self.claims)
        self._stop = asyncio.Event()
        self.scanner._flood.stop_event = self._stop
        self.markets.stop_event = self._stop
        self._sigint = 0
        self._seen = 0
        self._matched = 0
        self._tasks: list[asyncio.Task] = []

    def request_stop(self, *_args: object) -> None:
        self._sigint += 1
        LOGGER.info("Остановка (%s/2). Ещё раз Ctrl+C — принудительный выход", self._sigint)
        try:
            self._stop.set()
        except Exception:
            os._exit(0)
        if self._sigint >= 2:
            LOGGER.warning("Принудительный выход")
            os._exit(0)

    async def start(self) -> None:
        try:
            await asyncio.wait_for(self.storage.start(), timeout=8)
        except Exception as exc:
            LOGGER.warning("storage start: %s", exc)
        try:
            await self.market.http.start()
        except Exception as exc:
            LOGGER.warning("http start: %s", exc)
        await asyncio.wait_for(self.scanner.start(), timeout=25)
        LOGGER.info(
            "Маркет + бот | рейтинг %s–%s | NFT %s–%s | лот %s–%s TON",
            self.live.stars_rating_min,
            self.live.stars_rating_max,
            self.live.min_unique_gifts,
            self.live.max_unique_gifts,
            self.live.floor_min_ton,
            self.live.floor_max_ton,
        )
        try:
            await self.bot.send_message(
                ADMIN_ID,
                "Сканер запущен. /admin — панель фильтров.",
                disable_web_page_preview=True,
            )
        except Exception as exc:
            LOGGER.info("Не отправил старт админу (напишите боту /start): %s", exc)

    async def close(self) -> None:
        LOGGER.info("Закрываю соединения...")

        async def _shutdown() -> None:
            try:
                await self.dispatcher.stop_polling()
            except Exception:
                pass
            try:
                await self.storage.log_scan_event("shutdown", seen=self._seen, matched=self._matched)
            except Exception:
                pass
            try:
                await self.scanner.close()
            except Exception:
                pass
            try:
                await self.market.close()
            except Exception:
                pass
            try:
                await self.bot.session.close()
            except Exception:
                pass
            try:
                await self.storage.close()
            except Exception:
                pass

        try:
            await asyncio.wait_for(_shutdown(), timeout=5)
        except (asyncio.TimeoutError, Exception) as exc:
            LOGGER.warning("Закрытие зависло (%s) — выходим", exc)

    async def handle_snapshot(self, snapshot) -> None:
        if self._stop.is_set():
            return
        self._seen += 1
        decision = self.filters.evaluate(snapshot)
        try:
            await self.storage.save_snapshot(snapshot, decision.matched)
        except Exception:
            LOGGER.exception("Не удалось сохранить снимок user=%s", snapshot.metrics.user_id)
        if not decision.matched:
            return
        cooldown = self.live.alert_cooldown_sec
        if await self.storage.already_alerted(snapshot.metrics.user_id, snapshot.fingerprint, cooldown):
            LOGGER.info("Дедуп user=%s", snapshot.metrics.user_id)
            return
        sent = await self.logger_bot.send(decision)
        if sent:
            await self.storage.mark_alerted(snapshot.metrics.user_id, snapshot.fingerprint, cooldown)
            self._matched += 1
            LOGGER.info(
                "ALERT #%s user=%s rating=%s gifts=%s floor=%s",
                self._matched,
                snapshot.metrics.user_id,
                snapshot.metrics.stars_rating_level,
                len(snapshot.unique_gifts),
                snapshot.min_floor_ton,
            )
        else:
            LOGGER.error("MATCH user=%s, карточка в группу не ушла", snapshot.metrics.user_id)

    async def _scan_loop(self, live: bool) -> None:
        while not self._stop.is_set():
            if not self.live.scanner_enabled:
                LOGGER.info("Сканер выключен из админки, ждём")
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.live.market_poll_sec)
                except asyncio.TimeoutError:
                    continue
                continue
            LOGGER.info("Старт прохода Telegram Gift Market + MRKT")
            self.filters.reset_stats()
            try:
                async for snapshot in self.markets.iter_offers():
                    if self._stop.is_set() or not self.live.scanner_enabled:
                        break
                    await self.handle_snapshot(snapshot)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("Ошибка прохода по маркету")
            LOGGER.info("Фильтры за проход: %s", self.filters.dump_stats())
            LOGGER.info("Проход: seen=%s matched=%s", self._seen, self._matched)
            if not live or self._stop.is_set():
                break
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.live.market_poll_sec)
            except asyncio.TimeoutError:
                continue

    async def run(self, *, live: bool) -> None:
        await self.start()
        poll_task = asyncio.create_task(
            self.dispatcher.start_polling(self.bot, handle_signals=False),
            name="aiogram-polling",
        )
        scan_task = asyncio.create_task(self._scan_loop(live), name="market-scan")
        self._tasks = [poll_task, scan_task]
        stopper = asyncio.create_task(self._stop.wait(), name="stop-wait")
        try:
            if live:
                await stopper
            else:
                await scan_task
                self._stop.set()
        except asyncio.CancelledError:
            self._stop.set()
        finally:
            LOGGER.info("Останавливаю задачи...")
            self._stop.set()
            stopper.cancel()
            scan_task.cancel()
            poll_task.cancel()
            await asyncio.gather(scan_task, poll_task, stopper, return_exceptions=True)
            await self.close()
            LOGGER.info("Бот остановлен")
            if sys.platform == "win32":
                os._exit(0)


async def _export_session() -> None:
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    settings = get_settings()
    client = TelegramClient(settings.session_name, settings.api_id, settings.api_hash)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            LOGGER.error("Локальной сессии нет. Сначала выполните: python main.py --login")
            return
        value = StringSession.save(client.session)
        print("\nСкопируйте это значение в TELEGRAM_SESSION на хосте:\n")
        print(value)
        print()
    finally:
        await client.disconnect()


async def _login() -> None:
    settings = get_settings()
    scanner = ProfileScanner(settings, Storage(settings.database_url, settings.redis_url), TonMarketClient(settings))
    try:
        await scanner.login_interactive()
    finally:
        await scanner.close()


async def _amain(once: bool) -> None:
    settings = get_settings()
    app = AnalyticsApp(settings)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, app.request_stop)
        except (NotImplementedError, AttributeError, ValueError):
            try:
                signal.signal(sig, lambda *_: app.request_stop())
            except (OSError, ValueError):
                pass
    if hasattr(signal, "SIGBREAK"):
        try:
            signal.signal(signal.SIGBREAK, lambda *_: app.request_stop())
        except (OSError, ValueError):
            pass
    await app.run(live=not once)


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="TG-Gifts Analytics")
    parser.add_argument("--login", action="store_true", help="Авторизация Telethon")
    parser.add_argument("--export-session", action="store_true", help="Вывести TELEGRAM_SESSION для хоста")
    parser.add_argument("--once", action="store_true", help="Один проход маркета")
    return parser.parse_args(argv)


def main() -> None:
    print(f"tg-gifts {BUILD}", flush=True)
    setup_logging()
    args = parse_args()
    if sys.platform == "win32":
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", category=DeprecationWarning)
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        if args.login:
            asyncio.run(_login())
        elif args.export_session:
            asyncio.run(_export_session())
        else:
            asyncio.run(_amain(once=args.once))
    except KeyboardInterrupt:
        LOGGER.info("Остановлено пользователем")


if __name__ == "__main__":
    main()
