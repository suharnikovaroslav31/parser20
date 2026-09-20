"""
TG-Gifts — новый парсер мамонтов.

Оставляет вход в аккаунт (Telethon). Ищет только лохов-новичков: NEW Telegram,
рейтинг 1, ≤3 NFT, мимо флора, без перекупов/флипперов.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
import warnings
from pathlib import Path
from typing import Optional

from config import Settings, get_settings
from bot.app import build_bot, build_dispatcher
from bot.claims import ClaimStore
from bot.logger import GiftLogger
from core.mammoth import MammothHunter
from core.runtime import BUILD, LiveFilters
from core.session import TelegramAccount, write_session_file
from core.storage import Storage

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


class MammothApp:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.live = LiveFilters.from_settings(settings)
        self.storage = Storage(settings.database_url, settings.redis_url)
        self.account = TelegramAccount(settings)
        self.hunter = MammothHunter(self.account, settings)
        self.bot = build_bot(settings)
        self.claims = ClaimStore()
        self.dispatcher = build_dispatcher(self.live, self.claims)
        self.logger_bot = GiftLogger(self.bot, settings.log_group_id, self.live, self.claims)
        self._stop = asyncio.Event()
        self.account.flood.stop_event = self._stop
        self.hunter.stop_event = self._stop
        self._sigint = 0
        self._matched = 0
        self._pending: set[int] = set()
        self._tasks: list[asyncio.Task] = []

    def request_stop(self, *_args: object) -> None:
        self._sigint += 1
        LOGGER.info("Остановка (%s/2)", self._sigint)
        try:
            self._stop.set()
        except Exception:
            os._exit(0)
        if self._sigint >= 2:
            os._exit(0)

    async def start(self) -> None:
        LOGGER.info("сборка %s — только мамонты", BUILD)
        try:
            await asyncio.wait_for(self.storage.start(), timeout=8)
        except Exception as exc:
            LOGGER.warning("storage: %s", exc)
        await asyncio.wait_for(self.account.start(), timeout=25)
        try:
            me = await asyncio.wait_for(self.bot.get_me(), timeout=10)
            LOGGER.info("бот карточек @%s", me.username)
        except Exception as exc:
            LOGGER.warning("getMe: %s", exc)
        await self.logger_bot.probe()
        await self.logger_bot.announce_build(BUILD, self.settings.admin_id)
        LOGGER.info(
            "лог-группа %s | лох = ур.1 · ≤3 NFT · мимо флора · без флипперов",
            self.logger_bot.log_group_id,
        )

    async def close(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        try:
            await self.account.close()
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

    async def _emit(self, decision) -> None:
        uid = int(decision.snapshot.metrics.user_id)
        try:
            sent = await asyncio.wait_for(self.logger_bot.send(decision), timeout=12)
            if sent:
                self.hunter.seen.mark(uid)
                self._matched += 1
                LOGGER.info(
                    "ALERT #%s user=%s rating=%s floor=%s",
                    self._matched,
                    uid,
                    decision.snapshot.metrics.stars_rating_level,
                    decision.snapshot.min_floor_ton,
                )
            else:
                self.hunter.seen.release(uid)
                LOGGER.error("карточка user=%s не ушла", uid)
        except Exception:
            self.hunter.seen.release(uid)
            LOGGER.exception("сбой карточки user=%s", uid)
        finally:
            self._pending.discard(uid)

    async def _one_pass(self) -> None:
        async for decision in self.hunter.iter_mammoths():
            if self._stop.is_set():
                break
            uid = int(decision.snapshot.metrics.user_id)
            if uid in self._pending or self.hunter.seen.seen(uid):
                continue
            self._pending.add(uid)
            self.hunter.seen.mark(uid)
            task = asyncio.create_task(self._emit(decision), name=f"alert:{uid}")
            self._tasks.append(task)
            task.add_done_callback(
                lambda t: self._tasks.remove(t) if t in self._tasks else None
            )

    async def _scan_loop(self, once: bool) -> None:
        while not self._stop.is_set():
            if self.account.flood.session_dead:
                LOGGER.error("сессия мертва — пауза 15 мин")
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=900)
                except asyncio.TimeoutError:
                    continue
                continue
            if not await self.account.flood.ensure_connected():
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=30)
                except asyncio.TimeoutError:
                    continue
                continue
            LOGGER.info("старт круга мамонтов")
            try:
                await self._one_pass()
            except asyncio.CancelledError:
                if self._stop.is_set():
                    raise
            except Exception:
                LOGGER.exception("круг упал")
            LOGGER.info("мамонтов за сессию: %s", self._matched)
            if once or self._stop.is_set():
                break
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=1)
            except asyncio.TimeoutError:
                continue

    async def _heartbeat(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=20)
                return
            except asyncio.TimeoutError:
                ok = False
                try:
                    ok = bool(self.account.client.is_connected())
                except Exception:
                    pass
                LOGGER.info(
                    "жив | %s | stage=%s matched=%s tg=%s",
                    BUILD,
                    self.hunter.stage,
                    self._matched,
                    "ok" if ok else "нет",
                )
                if ok:
                    await self.account.ping()

    async def run(self, *, once: bool = False) -> None:
        await self.start()
        self._tasks = [
            asyncio.create_task(self._scan_loop(once), name="scan"),
            asyncio.create_task(self._heartbeat(), name="hb"),
            asyncio.create_task(self.dispatcher.start_polling(self.bot), name="bot"),
        ]
        try:
            await self._stop.wait()
        finally:
            await self.close()


def _write_telegram_session_env(raw: str) -> None:
    path = Path(".env")
    line = f"TELEGRAM_SESSION={raw}\n"
    if not path.exists():
        path.write_text(line, encoding="utf-8")
        return
    text = path.read_text(encoding="utf-8")
    if "TELEGRAM_SESSION=" in text:
        rows = []
        for row in text.splitlines(keepends=True):
            if row.startswith("TELEGRAM_SESSION="):
                rows.append(line if line.endswith("\n") else line + "\n")
            else:
                rows.append(row)
        path.write_text("".join(rows), encoding="utf-8")
    else:
        path.write_text(text.rstrip() + "\n" + line, encoding="utf-8")


async def _login() -> None:
    settings = get_settings()
    account = TelegramAccount(settings, fresh_login=True)
    try:
        await account.login_interactive()
        raw = account.export_session_string()
        write_session_file(raw)
        _write_telegram_session_env(raw)
        LOGGER.info("сессия сохранена в .env и data/")
    finally:
        await account.close()


async def _export_session() -> None:
    settings = get_settings()
    account = TelegramAccount(settings)
    try:
        await account.start()
        raw = account.export_session_string()
        write_session_file(raw)
        print(raw, flush=True)
        LOGGER.info("TELEGRAM_SESSION len=%s", len(raw))
    finally:
        await account.close()


async def _amain(once: bool) -> None:
    settings = get_settings()
    app = MammothApp(settings)
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
            signal.signal(signal.SIGBREAK, signal.SIG_IGN)
        except (OSError, ValueError):
            pass
    await app.run(once=once)


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="TG-Gifts mammoth hunter")
    parser.add_argument("--login", action="store_true", help="Авторизация Telethon")
    parser.add_argument("--export-session", action="store_true", help="Вывести TELEGRAM_SESSION")
    parser.add_argument("--once", action="store_true", help="Один круг")
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
        LOGGER.info("стоп")


if __name__ == "__main__":
    main()
