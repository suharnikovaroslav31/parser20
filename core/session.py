"""
Вход в Telegram userbot. Только сессия — без поиска лотов.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from pathlib import Path
from typing import Any, Optional

from telethon import TelegramClient
from telethon.errors import FloodWaitError, RPCError
from telethon.sessions import SQLiteSession, StringSession
from telethon.tl.types import User

from config import Settings
from core.rate_limit import AsyncRateLimiter, compute_backoff

LOGGER = logging.getLogger("tg_gifts.session")
SESSION_SQLITE = Path("data/telethon")
SESSION_SQLITE_FILE = Path("data/telethon.session")
SESSION_FILE = Path("data/mtproto.session.txt")
SESSION_LOCK = Path("data/telethon.lock")
ENV_HASH_FILE = Path("data/mtproto.env.sha256")


def _user_display(user: User) -> str:
    name = f"{user.first_name or ''} {user.last_name or ''}".strip()
    if user.username:
        return f"{name} @{user.username}".strip()
    return name or str(user.id)


def _read_session_file() -> str:
    try:
        return SESSION_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def write_session_file(raw: str) -> None:
    if not raw:
        return
    SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = SESSION_FILE.with_suffix(".txt.tmp")
    tmp.write_text(raw, encoding="utf-8")
    tmp.replace(SESSION_FILE)


def _env_hash(raw: str) -> str:
    if not raw:
        return ""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _read_env_hash() -> str:
    try:
        return ENV_HASH_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _write_env_hash(env_raw: str) -> None:
    ENV_HASH_FILE.parent.mkdir(parents=True, exist_ok=True)
    ENV_HASH_FILE.write_text(_env_hash(env_raw), encoding="utf-8")


def resolve_session_string(env_raw: str) -> str:
    env_raw = (env_raw or "").strip()
    file_raw = _read_session_file()
    if env_raw and _env_hash(env_raw) != _read_env_hash():
        LOGGER.info("TELEGRAM_SESSION новая — переключаю аккаунт")
        return env_raw
    return file_raw or env_raw


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _take_session_lock() -> None:
    SESSION_LOCK.parent.mkdir(parents=True, exist_ok=True)
    if SESSION_LOCK.exists():
        try:
            old = int(SESSION_LOCK.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            old = 0
        if old and old != os.getpid() and _pid_alive(old):
            LOGGER.warning("telethon.lock pid=%s ещё жив — два парсера убьют сессию", old)
        else:
            LOGGER.info("старый telethon.lock снят")
    SESSION_LOCK.write_text(str(os.getpid()), encoding="utf-8")


def _drop_session_lock() -> None:
    try:
        SESSION_LOCK.unlink()
    except OSError:
        pass


def _sqlite_usable() -> bool:
    try:
        return SESSION_SQLITE_FILE.exists() and SESSION_SQLITE_FILE.stat().st_size > 100
    except OSError:
        return False


def _drop_sqlite_session() -> None:
    for path in (
        SESSION_SQLITE_FILE,
        Path(f"{SESSION_SQLITE}.session-journal"),
        Path("data/telethon.session-journal"),
    ):
        try:
            path.unlink()
        except OSError:
            pass


def _bootstrap_sqlite_from_string(raw: str) -> bool:
    if _sqlite_usable():
        return True
    _drop_sqlite_session()
    if not raw:
        return False
    try:
        src = StringSession(raw)
        if not getattr(src, "auth_key", None):
            return False
        dest = SQLiteSession(str(SESSION_SQLITE))
        try:
            dest.set_dc(src.dc_id, src.server_address, src.port)
            dest.auth_key = src.auth_key
            dest.save()
        finally:
            dest.close()
        LOGGER.info("Сессия в %s", SESSION_SQLITE_FILE)
        return _sqlite_usable()
    except Exception as exc:
        LOGGER.warning("Не собрал telethon.session: %s", exc)
        _drop_sqlite_session()
        return False


class FloodGate:
    RPC_TIMEOUT = 10.0

    def __init__(self, limiter: AsyncRateLimiter) -> None:
        self.limiter = limiter
        self.client: Optional[TelegramClient] = None
        self.stop_event: Optional[asyncio.Event] = None
        self.session_dead = False
        self.cool_until = 0.0

    @property
    def cooling(self) -> bool:
        return time.monotonic() < self.cool_until

    def _stopping(self) -> bool:
        return self.stop_event is not None and self.stop_event.is_set()

    def note_flood(self, wait: int) -> None:
        pause = min(max(int(wait or 0), 0), 12)
        if pause >= 3:
            self.cool_until = max(self.cool_until, time.monotonic() + pause)

    async def sleep(self, seconds: float) -> None:
        if seconds <= 0:
            return
        if self.stop_event is None:
            await asyncio.sleep(seconds)
            return
        try:
            await asyncio.wait_for(self.stop_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            return
        raise asyncio.CancelledError

    def mark_ok(self) -> None:
        pass

    def mark_session_dead(self, reason: object) -> None:
        self.session_dead = True
        LOGGER.error("Сессия отозвана (%s). Нужен новый TELEGRAM_SESSION", reason)

    async def ensure_connected(self) -> bool:
        client = self.client
        if client is None or self.session_dead:
            return False
        try:
            if not client.is_connected():
                await asyncio.wait_for(client.connect(), timeout=12)
            if not await asyncio.wait_for(client.is_user_authorized(), timeout=8):
                self.mark_session_dead("unauthorized")
                return False
            return True
        except Exception as exc:
            LOGGER.warning("connect: %s", exc)
            return False

    async def call(self, factory, *, retries: int = 3, label: str = "rpc") -> Any:
        last_error: BaseException | None = None
        for attempt in range(retries):
            if self._stopping():
                raise asyncio.CancelledError
            try:
                result = await asyncio.wait_for(self.limiter.run(factory), timeout=self.RPC_TIMEOUT)
                self.mark_ok()
                return result
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError as exc:
                last_error = exc
                LOGGER.warning("timeout %s", label)
                continue
            except FloodWaitError as exc:
                wait = int(getattr(exc, "seconds", 1) or 1)
                if 0 < wait <= 8 and attempt + 1 < retries:
                    LOGGER.warning("FloodWait %s %ss — жду", label, wait)
                    await self.sleep(wait + 0.3)
                    last_error = exc
                    continue
                self.note_flood(wait)
                LOGGER.warning("FloodWait %s %ss — skip", label, wait)
                raise
            except RPCError as exc:
                name = type(exc).__name__.upper()
                if any(token in name for token in ("AUTHKEY", "UNAUTHORIZED", "SESSIONREVOKED", "SESSIONEXPIRED")):
                    self.mark_session_dead(exc)
                    raise
                delay = min(2.0, compute_backoff(attempt, base=0.4, cap=2.0))
                LOGGER.warning("RPC %s attempt=%s: %s", label, attempt + 1, exc)
                await self.sleep(delay)
                last_error = exc
        if last_error is not None:
            raise last_error
        raise RuntimeError(label)


class TelegramAccount:
    """Userbot: вход, пинг, сохранение ключа. Поиск лотов — в mammoth."""

    def __init__(self, settings: Settings, *, fresh_login: bool = False) -> None:
        self.settings = settings
        self._limiter = AsyncRateLimiter(1, min_interval=0.35, max_jitter=0.08)
        self.flood = FloodGate(self._limiter)
        self._session_raw = ""
        self.me_id: Optional[int] = None
        if fresh_login:
            session: str | StringSession = StringSession()
        else:
            env_raw = settings.telegram_session.strip()
            if env_raw and _env_hash(env_raw) != _read_env_hash():
                _drop_sqlite_session()
            raw = resolve_session_string(env_raw)
            self._session_raw = raw
            if raw and _bootstrap_sqlite_from_string(raw):
                session = str(SESSION_SQLITE)
            elif raw:
                try:
                    session = StringSession(raw)
                except Exception as exc:
                    LOGGER.error("TELEGRAM_SESSION битая: %s", exc)
                    session = StringSession()
            else:
                session = settings.session_name
        self.client = TelegramClient(
            session,
            settings.api_id,
            settings.api_hash,
            device_model="TGGiftsParser",
            system_version="Windows 10",
            app_version="2.0",
            lang_code="ru",
            system_lang_code="ru",
            timeout=15,
            request_retries=2,
            connection_retries=8,
            retry_delay=2,
            auto_reconnect=True,
            flood_sleep_threshold=0,
        )
        self.flood.client = self.client

    async def login_interactive(self) -> None:
        await self.client.start()
        me = await self.client.get_me()
        LOGGER.info("Сессия для %s id=%s", _user_display(me), me.id)
        LOGGER.info("TELEGRAM_SESSION=%s", StringSession.save(self.client.session))
        self.persist()

    async def start(self) -> None:
        session_len = len(self.settings.telegram_session.strip())
        LOGGER.info("MTProto sqlite=%s env_len=%s", _sqlite_usable(), session_len)
        _take_session_lock()
        await asyncio.wait_for(self.client.connect(), timeout=20)
        if not await self.client.is_user_authorized():
            raw = self._session_raw or self.settings.telegram_session.strip()
            try:
                await asyncio.wait_for(self.client.disconnect(), timeout=3)
            except Exception:
                pass
            _drop_sqlite_session()
            if raw:
                self.client = TelegramClient(
                    StringSession(raw),
                    self.settings.api_id,
                    self.settings.api_hash,
                    device_model="TGGiftsParser",
                    system_version="Windows 10",
                    app_version="2.0",
                    lang_code="ru",
                    system_lang_code="ru",
                    timeout=15,
                    request_retries=2,
                    connection_retries=8,
                    retry_delay=2,
                    auto_reconnect=True,
                    flood_sleep_threshold=0,
                )
                self.flood.client = self.client
                await asyncio.wait_for(self.client.connect(), timeout=20)
            if not self.client.is_connected() or not await self.client.is_user_authorized():
                raise RuntimeError(
                    "TELEGRAM_SESSION недействительна. На ПК: python main.py --login "
                    "затем --export-session. Не гоняй аккаунт на ПК и Bothost вместе."
                )
        me = await asyncio.wait_for(self.client.get_me(), timeout=15)
        self.me_id = int(me.id)
        self.persist()
        LOGGER.info("Вошёл как %s id=%s", _user_display(me), me.id)

    def persist(self) -> None:
        try:
            saver = getattr(self.client.session, "save", None)
            if callable(saver):
                saver()
        except Exception:
            pass
        try:
            raw = StringSession.save(self.client.session)
        except Exception:
            return
        try:
            write_session_file(raw)
            _write_env_hash(self.settings.telegram_session.strip())
        except OSError as exc:
            LOGGER.warning("не сохранил сессию: %s", exc)

    async def ping(self) -> bool:
        if self.flood.session_dead or not self.client.is_connected():
            return False
        try:
            from telethon.tl.functions.updates import GetStateRequest

            await asyncio.wait_for(self.client(GetStateRequest()), timeout=10)
            self.persist()
            return True
        except RPCError as exc:
            name = type(exc).__name__.upper()
            if any(token in name for token in ("AUTHKEY", "UNAUTHORIZED", "SESSIONREVOKED")):
                self.flood.mark_session_dead(exc)
            return False
        except Exception as exc:
            LOGGER.warning("ping: %s", exc)
            return False

    async def close(self) -> None:
        self.persist()
        if self.client.is_connected():
            await self.client.disconnect()
        _drop_session_lock()

    def export_session_string(self) -> str:
        return StringSession.save(self.client.session)
