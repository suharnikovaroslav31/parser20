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
from typing import Awaitable, Callable, Optional

from config import Settings, get_settings
from bot.app import build_bot, build_dispatcher
from bot.claims import ClaimStore
from bot.logger import GiftLogger
from core.filters import ProfileFilter
from core.market import GiftMarketScanner
from core.parser import ProfileScanner
from core.runtime import BUILD, LiveFilters
from core.storage import Storage
from core.ton_client import TonMarketClient

LOGGER = logging.getLogger("tg_gifts")


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
        self._pass_task: Optional[asyncio.Task] = None
        self._alert_tasks: set[asyncio.Task] = set()
        self._pending_alerts: set[str] = set()

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
        settings = self.settings
        LOGGER.info("сборка %s", BUILD)
        try:
            await asyncio.wait_for(self.storage.start(), timeout=8)
        except Exception as exc:
            LOGGER.warning("storage start: %s", exc)
        try:
            await self.market.http.start()
        except Exception as exc:
            LOGGER.warning("http start: %s", exc)
        await asyncio.wait_for(self.scanner.start(), timeout=25)
        try:
            me = await asyncio.wait_for(self.bot.get_me(), timeout=10)
            LOGGER.info("Бот карточек @%s id=%s", me.username, me.id)
        except Exception as exc:
            LOGGER.warning("getMe бота: %s", exc)
        await self.logger_bot.probe()
        await self.logger_bot.announce_build(BUILD, settings.admin_id)
        LOGGER.info("Лог-группа %s | админ %s | сборка %s", self.logger_bot.log_group_id, settings.admin_id, BUILD)
        LOGGER.info(
            "Telegram NEW-лоты | рейтинг %s–%s | NFT %s–%s | лот %s–%s TON",
            self.live.stars_rating_min,
            self.live.stars_rating_max,
            self.live.min_unique_gifts,
            self.live.max_unique_gifts,
            self.live.floor_min_ton,
            self.live.floor_max_ton,
        )

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
        try:
            decision = self.filters.evaluate(snapshot)
            try:
                await asyncio.wait_for(self.storage.save_snapshot(snapshot, decision.matched), timeout=4)
            except Exception:
                LOGGER.exception("Не удалось сохранить снимок user=%s", snapshot.metrics.user_id)
            opened = snapshot.metrics.stars_fetched and snapshot.metrics.gifts_fetched
            key = snapshot.listing_key
            if not decision.matched:
                if opened:
                    self.markets.tracker.mark(key)
                return
            cooldown = self.live.alert_cooldown_sec
            if await self.storage.already_alerted(snapshot.metrics.user_id, snapshot.fingerprint, cooldown):
                LOGGER.info("Дедуп user=%s", snapshot.metrics.user_id)
                if opened:
                    self.markets.tracker.mark(key)
                return
            fp = snapshot.fingerprint
            if fp in self._pending_alerts:
                return
            self._pending_alerts.add(fp)
            task = asyncio.create_task(
                self._emit_alert(snapshot, decision, key, opened, cooldown, fp),
                name=f"alert:{snapshot.metrics.user_id}",
            )
            self._alert_tasks.add(task)
            task.add_done_callback(self._alert_tasks.discard)
        except Exception:
            LOGGER.exception(
                "сбой карточки user=%s — круг сканера не рву",
                snapshot.metrics.user_id,
            )

    async def _emit_alert(self, snapshot, decision, key: str, opened: bool, cooldown: int, fp: str) -> None:
        try:
            sent = await asyncio.wait_for(self.logger_bot.send(decision), timeout=12)
            if sent:
                await self.storage.mark_alerted(snapshot.metrics.user_id, snapshot.fingerprint, cooldown)
                if opened:
                    self.markets.tracker.mark(key)
                self._matched += 1
                LOGGER.info(
                    "ALERT #%s user=%s rating=%s gifts=%s floor=%s",
                    self._matched,
                    snapshot.metrics.user_id,
                    snapshot.metrics.stars_rating_level,
                    len(snapshot.unique_gifts),
                    snapshot.min_floor_ton,
                )
                return
            LOGGER.error(
                "MATCH user=%s, карточка в группу не ушла — лот повторю в следующем круге",
                snapshot.metrics.user_id,
            )
        except Exception:
            LOGGER.exception(
                "сбой карточки user=%s — круг сканера не рву",
                snapshot.metrics.user_id,
            )
        finally:
            self._pending_alerts.discard(fp)

    async def _one_pass(self) -> None:
        self.filters.reset_stats()
        async for snapshot in self.markets.iter_offers():
            if self._stop.is_set() or not self.live.scanner_enabled:
                break
            await self.handle_snapshot(snapshot)
        LOGGER.info("круг %s | %s", BUILD, self.filters.dump_stats())

    async def _scan_loop(self, live: bool) -> None:
        while not self._stop.is_set():
            if not self.live.scanner_enabled:
                LOGGER.info("Сканер выключен из админки, ждём")
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.live.market_poll_sec)
                except asyncio.TimeoutError:
                    continue
                continue
            if self.scanner._flood.session_dead:
                LOGGER.error("Сессия Telegram отозвана — пауза 15 мин, пока не вставишь новый TELEGRAM_SESSION")
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=900)
                except asyncio.TimeoutError:
                    continue
                continue
            if not await self.scanner._flood.ensure_connected():
                LOGGER.error("Нет сессии Telegram — пауза 45с")
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=45)
                except asyncio.TimeoutError:
                    continue
                continue
            LOGGER.info("Старт прохода: свежие NEW-лоты Telegram")
            self._pass_task = asyncio.create_task(self._one_pass(), name="market-pass")
            try:
                await self._pass_task
            except asyncio.CancelledError:
                if self._stop.is_set():
                    raise
                LOGGER.warning("проход сорван — сразу новый круг")
            except Exception as exc:
                name = type(exc).__name__
                if any(token in name.upper() for token in ("AUTHKEY", "UNAUTHORIZED", "SESSIONREVOKED")):
                    self.scanner._flood.mark_session_dead(exc)
                    try:
                        await asyncio.wait_for(self._stop.wait(), timeout=60)
                    except asyncio.TimeoutError:
                        pass
                else:
                    LOGGER.exception("Ошибка прохода по маркету")
            finally:
                self._pass_task = None
            LOGGER.info("Фильтры за проход: %s", self.filters.dump_stats())
            LOGGER.info("Проход: seen=%s matched=%s", self._seen, self._matched)
            if not live or self._stop.is_set():
                break
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=min(8, max(3, self.live.market_poll_sec)))
            except asyncio.TimeoutError:
                continue

    async def _heartbeat(self) -> None:
        ticks = 0
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=20)
                return
            except asyncio.TimeoutError:
                ticks += 1
                connected = False
                try:
                    connected = bool(self.scanner.client.is_connected())
                except Exception:
                    pass
                LOGGER.info(
                    "жив | сборка %s | stage=%s seen=%s matched=%s tg=%s pass=%s | %s",
                    BUILD,
                    self.markets.stage,
                    self._seen,
                    self._matched,
                    "ok" if connected else "нет",
                    "да" if self._pass_task is not None and not self._pass_task.done() else "нет",
                    self.filters.dump_stats(),
                )
                scanning = self._pass_task is not None and not self._pass_task.done()
                if connected and ticks % 2 == 0 and not scanning:
                    await self.scanner.ping()

    async def _watchdog(self) -> None:
        last = ""
        stale = 0
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=20)
                return
            except asyncio.TimeoutError:
                pass
            mark = f"{self.markets.stage}|{self._seen}|{self._matched}"
            active = self._pass_task is not None and not self._pass_task.done()
            if not active or self.markets.stage in {"idle", ""}:
                last = mark
                stale = 0
                continue
            if mark == last:
                stale += 1
            else:
                stale = 0
            last = mark
            if stale < 4:
                continue
            LOGGER.error(
                "сканер завис на %s seen=%s — рву проход, Telegram не трогаю",
                self.markets.stage,
                self._seen,
            )
            task = self._pass_task
            if task is not None and not task.done():
                task.cancel()
            stale = 0

    async def _forever(self, name: str, factory: Callable[[], Awaitable[None]]) -> None:
        while not self._stop.is_set():
            try:
                await factory()
            except asyncio.CancelledError:
                if self._stop.is_set():
                    raise
                LOGGER.warning("%s отменён — поднимаю снова", name)
            except Exception:
                LOGGER.exception("%s упал", name)
            if self._stop.is_set():
                return
            LOGGER.error("%s перезапуск через 5с", name)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=5)
                return
            except asyncio.TimeoutError:
                continue

    async def run(self, *, live: bool) -> None:
        poll_task = scan_task = beat_task = watch_task = stopper = None
        try:
            try:
                await self.start()
            except Exception:
                LOGGER.exception("старт не удался")
                raise
            poll_task = asyncio.create_task(
                self._forever(
                    "бот",
                    lambda: self.dispatcher.start_polling(self.bot, handle_signals=False),
                ),
                name="aiogram-polling",
            )
            if live:
                scan_task = asyncio.create_task(
                    self._forever("сканер", lambda: self._scan_loop(True)),
                    name="market-scan",
                )
            else:
                scan_task = asyncio.create_task(self._scan_loop(False), name="market-scan")
            beat_task = asyncio.create_task(self._heartbeat(), name="heartbeat")
            watch_task = asyncio.create_task(self._watchdog(), name="watchdog")
            self._tasks = [poll_task, scan_task, beat_task, watch_task]
            stopper = asyncio.create_task(self._stop.wait(), name="stop-wait")
            if live:
                await stopper
            else:
                await scan_task
                self._stop.set()
        except asyncio.CancelledError:
            self._stop.set()
            raise
        finally:
            LOGGER.info("Останавливаю задачи...")
            self._stop.set()
            for task in (scan_task, poll_task, beat_task, watch_task, stopper, self._pass_task):
                if task is not None:
                    task.cancel()
            await asyncio.gather(
                *(
                    task
                    for task in (scan_task, poll_task, beat_task, watch_task, stopper, self._pass_task)
                    if task is not None
                ),
                return_exceptions=True,
            )
            await self.close()
            LOGGER.info("Бот остановлен")
            if sys.platform == "win32":
                os._exit(0)


def _write_telegram_session_env(value: str) -> None:
    from pathlib import Path

    path = Path(".env")
    if not path.exists():
        return
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    out: list[str] = []
    found = False
    for line in lines:
        if line.startswith("TELEGRAM_SESSION="):
            out.append(f"TELEGRAM_SESSION={value}\n")
            found = True
        else:
            out.append(line)
    if not found:
        if out and not str(out[-1]).endswith("\n"):
            out.append("\n")
        out.append(f"TELEGRAM_SESSION={value}\n")
    path.write_text("".join(out), encoding="utf-8")
    LOGGER.info("TELEGRAM_SESSION записана в .env")


async def _export_session() -> None:
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    from core.parser import resolve_session_string

    settings = get_settings()
    raw = resolve_session_string(settings.telegram_session)
    session = StringSession(raw) if raw else settings.session_name
    client = TelegramClient(session, settings.api_id, settings.api_hash)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            LOGGER.error("Локальной сессии нет. Сначала выполните: python main.py --login")
            return
        me = await client.get_me()
        value = StringSession.save(client.session)
        LOGGER.info("Экспорт сессии %s id=%s", getattr(me, "first_name", ""), me.id)
        print("\nСкопируйте это значение в TELEGRAM_SESSION на хосте:\n")
        print(value)
        print()
    finally:
        await client.disconnect()


async def _login() -> None:
    from telethon.sessions import StringSession

    settings = get_settings()
    scanner = ProfileScanner(
        settings,
        Storage(settings.database_url, settings.redis_url),
        TonMarketClient(settings),
        fresh_login=True,
    )
    try:
        await scanner.login_interactive()
        raw = StringSession.save(scanner.client.session)
        _write_telegram_session_env(raw)
    finally:
        await scanner.close()


async def _amain(once: bool) -> None:
    settings = get_settings()
    app = AnalyticsApp(settings)
    loop = asyncio.get_running_loop()

    def _on_asyncio_error(_loop: asyncio.AbstractEventLoop, context: dict) -> None:
        LOGGER.error("asyncio: %s", context.get("message"), exc_info=context.get("exception"))

    loop.set_exception_handler(_on_asyncio_error)
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
            signal.signal(signal.SIGBREAK, signal.SIG_IGN)
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
