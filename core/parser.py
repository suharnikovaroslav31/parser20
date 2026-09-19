"""
Асинхронный сканер публичных профилей Telegram и коллекционных подарков.

Источники кандидатов — люди вокруг сессии, не продавцы MRKT/Tonnel:
1. Кому недавно прилетел гифт в чатах сессии (получатель, не гифтер).
2. Открытые диалоги аккаунта.
3. Live-события: MessageActionStarGift / StarGiftUnique.
4. SEED_USERNAMES / SEED_USER_IDS / SEED_CHATS, если заданы.

Читаются только подарки, которые пользователь выставил в профиле
(`payments.getSavedStarGifts`). Приватные коллекции Telegram не отдаёт —
это обрабатывается как штатный skip, а не ошибка.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
import os
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Optional

from telethon import TelegramClient, events
from telethon.errors import (
    ChannelPrivateError,
    ChatAdminRequiredError,
    FloodWaitError,
    RPCError,
    UserPrivacyRestrictedError,
)
from telethon.sessions import SQLiteSession, StringSession
from telethon.tl.functions.contacts import GetContactsRequest
from telethon.tl.functions.payments import GetSavedStarGiftsRequest
from telethon.tl.functions.users import GetFullUserRequest
from telethon.tl.types import Channel, InputPeerUser, InputUser, PeerUser, StarGift, User

try:
    from telethon.tl.types import MessageActionStarGift
except ImportError:
    MessageActionStarGift = tuple()  # type: ignore[misc,assignment]

from config import Settings
from core.filters import account_age_days, compute_activity_score
from core.models import (
    AccountMetrics,
    GiftAttribute,
    ProfileSnapshot,
    RegularGift,
    UniqueGift,
)
from core.rate_limit import AsyncRateLimiter, compute_backoff
from core.storage import Storage
from core.ton_client import TonMarketClient, to_ton

LOGGER = logging.getLogger("tg_gifts.parser")
# Bothost хранит между рестартами только папку data/ (см. bothost.ru/docs/database-storage).
SESSION_SQLITE = Path("data/telethon")
SESSION_SQLITE_FILE = Path("data/telethon.session")
SESSION_FILE = Path("data/mtproto.session.txt")
SESSION_LOCK = Path("data/telethon.lock")
ENV_HASH_FILE = Path("data/mtproto.env.sha256")

try:
    from telethon.tl.types import MessageActionStarGiftUnique
except ImportError:  # старые слои Telethon без unique-action
    MessageActionStarGiftUnique = type("MessageActionStarGiftUnique", (), {})  # type: ignore[misc,assignment]

try:
    from telethon.tl.functions.payments import GetUniqueStarGiftValueInfoRequest
except ImportError:
    GetUniqueStarGiftValueInfoRequest = None  # type: ignore[misc,assignment]


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


def _write_session_file(raw: str) -> None:
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
    """Файл — живой ключ. Если TELEGRAM_SESSION в env сменили, берём её (новый аккаунт)."""
    env_raw = (env_raw or "").strip()
    file_raw = _read_session_file()
    if env_raw and _env_hash(env_raw) != _read_env_hash():
        LOGGER.info("TELEGRAM_SESSION новая — переключаю аккаунт сканера")
        return env_raw
    return file_raw or env_raw


def _take_session_lock() -> None:
    SESSION_LOCK.parent.mkdir(parents=True, exist_ok=True)
    if SESSION_LOCK.exists():
        LOGGER.warning(
            "data/telethon.lock уже есть — прошлый процесс не закрылся. "
            "Два парсера с одним ключом = Telegram отзовёт сессию через несколько часов."
        )
    SESSION_LOCK.write_text(str(os.getpid()), encoding="utf-8")


def _drop_session_lock() -> None:
    try:
        SESSION_LOCK.unlink()
    except OSError:
        pass


def _bootstrap_sqlite_from_string(raw: str) -> bool:
    """Первый запуск на Bothost: переносим TELEGRAM_SESSION в живущий файл data/telethon.session."""
    if SESSION_SQLITE_FILE.exists() and SESSION_SQLITE_FILE.stat().st_size > 100:
        return True
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
        LOGGER.info("Сессия перенесена в %s (папка data/ на Bothost не затирается)", SESSION_SQLITE_FILE)
        return SESSION_SQLITE_FILE.exists()
    except Exception as exc:
        LOGGER.warning("Не удалось собрать data/telethon.session: %s", exc)
        return False


class TelegramFloodControl:
    """FloodWait и зависшие RPC: не спим минутами, при timeout переподключаемся."""

    RPC_TIMEOUT = 18.0

    def __init__(self, limiter: AsyncRateLimiter) -> None:
        self.limiter = limiter
        self.client: Optional[TelegramClient] = None
        self.stop_event: Optional[asyncio.Event] = None
        self._reconnect_lock = asyncio.Lock()
        self._last_reconnect = 0.0
        self._last_ok = time.monotonic()
        self.session_dead = False
        self.cool_until = 0.0

    @property
    def cooling(self) -> bool:
        return time.monotonic() < self.cool_until

    def _stopping(self) -> bool:
        return self.stop_event is not None and self.stop_event.is_set()

    def _note_flood(self, wait: int) -> None:
        pause = min(max(wait, 15), 90)
        self.cool_until = max(self.cool_until, time.monotonic() + pause)

    async def _sleep(self, seconds: float) -> None:
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
        self._last_ok = time.monotonic()

    def mark_session_dead(self, reason: object) -> None:
        self.session_dead = True
        LOGGER.error(
            "Сессия Telegram отозвана (%s). Нужен новый TELEGRAM_SESSION — старый ключ уже не живой",
            reason,
        )

    async def ensure_connected(self) -> bool:
        client = self.client
        if client is None or self.session_dead:
            return False
        try:
            if not client.is_connected():
                LOGGER.warning("Telegram оффлайн — подключаюсь")
                await asyncio.wait_for(client.connect(), timeout=12)
            if not await asyncio.wait_for(client.is_user_authorized(), timeout=8):
                self.mark_session_dead("is_user_authorized=false")
                return False
            self.mark_ok()
            return True
        except Exception as exc:
            LOGGER.warning("Telegram connect: %s", exc)
            return False

    async def _recover(self, label: str) -> None:
        """Только если сокет мёртв. disconnect при живом ключе даёт AUTH_KEY_DUPLICATED."""
        client = self.client
        if client is None or self.session_dead:
            return
        async with self._reconnect_lock:
            now = time.monotonic()
            if now - self._last_reconnect < 45:
                return
            if client.is_connected():
                LOGGER.warning("Telegram timeout %s — сокет жив, disconnect не делаю", label)
                return
            self._last_reconnect = now
            LOGGER.warning("Telegram reconnect после обрыва %s", label)
            try:
                await asyncio.wait_for(client.connect(), timeout=12)
            except Exception as exc:
                LOGGER.warning("reconnect не удался: %s", exc)
                return
            try:
                if await asyncio.wait_for(client.is_user_authorized(), timeout=8):
                    self.mark_ok()
                    raw = StringSession.save(client.session)
                    _write_session_file(raw)
                else:
                    self.mark_session_dead("reconnect unauthorized")
            except Exception:
                pass

    async def _await_rpc(self, factory, label: str) -> Any:
        # Без shield: иначе зависший RPC держит слот семафора часами, сканер встаёт.
        task = asyncio.create_task(self.limiter.run(factory), name=f"tg:{label}")
        try:
            return await asyncio.wait_for(task, timeout=self.RPC_TIMEOUT)
        except asyncio.CancelledError:
            if not task.done():
                task.cancel()
                try:
                    await task
                except (Exception, asyncio.CancelledError):
                    pass
            raise
        except asyncio.TimeoutError:
            if not task.done():
                task.cancel()
                try:
                    await task
                except (Exception, asyncio.CancelledError):
                    pass
            LOGGER.warning("Telegram timeout %s", label)
            await self._recover(label)
            raise

    async def call(self, factory, *, retries: int = 4, label: str = "rpc") -> Any:
        last_error: BaseException | None = None
        for attempt in range(retries):
            if self._stopping():
                raise asyncio.CancelledError
            try:
                result = await self._await_rpc(factory, label)
                self.mark_ok()
                return result
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError as exc:
                last_error = exc
                continue
            except FloodWaitError as exc:
                wait = int(getattr(exc, "seconds", 1) or 1)
                self._note_flood(wait)
                if wait <= 25 and attempt + 1 < retries:
                    LOGGER.warning("Telegram FloodWait %s: пауза %ss", label, wait)
                    await self._sleep(wait)
                    last_error = exc
                    continue
                LOGGER.warning("Telegram FloodWait %s %ss — пропускаю", label, wait)
                raise
            except RPCError as exc:
                name = type(exc).__name__.upper()
                if any(token in name for token in ("AUTHKEY", "UNAUTHORIZED", "SESSIONREVOKED", "SESSIONEXPIRED")):
                    self.mark_session_dead(exc)
                    raise
                message = str(exc).upper()
                if any(token in message for token in ("PRIVACY", "GIFT", "SAVED_STAR", "USER_NOT_MUTUAL")):
                    LOGGER.debug("Telegram RPC skip %s: %s", label, exc)
                    raise
                delay = min(2.0, compute_backoff(attempt, base=0.4, cap=2.0))
                LOGGER.warning("Telegram RPC %s attempt=%s delay=%.1fs err=%s", label, attempt + 1, delay, exc)
                await self._sleep(delay)
                last_error = exc
        if last_error is not None:
            raise last_error
        raise RuntimeError(f"Не удалось выполнить {label}")


class ProfileScanner:
    """Сканер профилей + публичных NFT-подарков через Telethon Client API."""

    def __init__(
        self,
        settings: Settings,
        storage: Storage,
        market: TonMarketClient,
        *,
        fresh_login: bool = False,
    ) -> None:
        self.settings = settings
        self.storage = storage
        self.market = market
        self._limiter = AsyncRateLimiter(settings.telegram_concurrency, min_interval=0.1, max_jitter=0.03)
        self._flood = TelegramFloodControl(self._limiter)
        self._seen_at: dict[int, float] = {}
        self._me_id: Optional[int] = None
        self._queue: asyncio.Queue[tuple[User, str]] = asyncio.Queue()
        if fresh_login:
            LOGGER.info("Чистый логин: сессия из .env не берётся")
            session = StringSession()
        else:
            raw = resolve_session_string(self.settings.telegram_session)
            if SESSION_SQLITE_FILE.exists() or (raw and _bootstrap_sqlite_from_string(raw)):
                session: str | StringSession = str(SESSION_SQLITE)
            elif raw:
                try:
                    session = StringSession(raw)
                except Exception as exc:
                    LOGGER.error("TELEGRAM_SESSION не читается: %s", exc)
                    session = StringSession()
            else:
                session = settings.session_name
        self.client = TelegramClient(
            session,
            settings.api_id,
            settings.api_hash,
            # Не маскируемся под официальный Desktop: иначе Telegram через несколько
            # часов считает сессию дублем и отзывает AUTH_KEY.
            device_model="TGGiftsParser",
            system_version="Windows 10",
            app_version="1.0",
            lang_code="ru",
            system_lang_code="ru",
            timeout=15,
            request_retries=2,
            connection_retries=8,
            retry_delay=2,
            auto_reconnect=True,
            flood_sleep_threshold=0,
        )
        self._flood.client = self.client

    async def login_interactive(self) -> None:
        """Первичная авторизация MTProto (телефон + код). Нужен один раз."""
        await self.client.start()
        me = await self.client.get_me()
        LOGGER.info("Сессия сохранена для %s id=%s", _user_display(me), me.id)
        LOGGER.info("TELEGRAM_SESSION=%s", StringSession.save(self.client.session))
        self.persist_session()

    async def start(self) -> None:
        session_len = len(self.settings.telegram_session.strip())
        LOGGER.info(
            "MTProto: %s",
            f"{SESSION_SQLITE_FILE}" if SESSION_SQLITE_FILE.exists()
            else (f"TELEGRAM_SESSION ({session_len} символов)" if session_len else f"файл {self.settings.session_name}"),
        )
        _take_session_lock()
        await asyncio.wait_for(self.client.connect(), timeout=20)
        if not await self.client.is_user_authorized():
            try:
                await asyncio.wait_for(self.client.disconnect(), timeout=3)
            except Exception:
                pass
            if session_len:
                raise RuntimeError(
                    "TELEGRAM_SESSION недействительна (слетела или обрезана при вставке). "
                    "На ПК: python main.py --export-session — вставь строку ЦЕЛИКОМ в TELEGRAM_SESSION на хосте, без кавычек. "
                    "Этот аккаунт не запускай на ПК, пока крутится хост."
                )
            raise RuntimeError(
                "TELEGRAM_SESSION пустая. На ПК: python main.py --login, затем python main.py --export-session, "
                "строку вставь в TELEGRAM_SESSION на хосте."
            )
        me = await asyncio.wait_for(self.client.get_me(), timeout=15)
        self._me_id = int(me.id)
        self.persist_session()
        LOGGER.info("MTProto клиент вошёл как %s id=%s", _user_display(me), me.id)
        self._flood.mark_ok()
        self._register_live_handlers()

    async def ping(self) -> bool:
        """Держим updates alive без disconnect — иначе Telegram считает сессию брошенной."""
        if self._flood.session_dead or not self.client.is_connected():
            return False
        try:
            from telethon.tl.functions.updates import GetStateRequest

            await asyncio.wait_for(self.client(GetStateRequest()), timeout=10)
            self._flood.mark_ok()
            self.persist_session()
            return True
        except RPCError as exc:
            name = type(exc).__name__.upper()
            if any(token in name for token in ("AUTHKEY", "UNAUTHORIZED", "SESSIONREVOKED", "SESSIONEXPIRED")):
                self._flood.mark_session_dead(exc)
            else:
                LOGGER.warning("Telegram ping: %s", exc)
            return False
        except Exception as exc:
            LOGGER.warning("Telegram ping: %s", exc)
            return False

    def persist_session(self) -> None:
        """Пишем ключ в data/ — на Bothost только эта папка живёт между рестартами."""
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
            _write_session_file(raw)
            _write_env_hash(self.settings.telegram_session.strip())
        except OSError as exc:
            LOGGER.warning("не сохранил сессию на диск: %s", exc)

    async def close(self) -> None:
        self.persist_session()
        if self.client.is_connected():
            await self.client.disconnect()
        _drop_session_lock()

    def _register_live_handlers(self) -> None:
        """Мониторинг передач/получения подарков в чатах, доступных сессии."""

        @self.client.on(events.NewMessage(incoming=True, outgoing=True))
        async def _on_message(event: events.NewMessage.Event) -> None:  # type: ignore[misc]
            try:
                await self._handle_gift_message(event)
            except Exception as exc:
                LOGGER.debug("live handler: %s", exc)

        @self.client.on(events.Raw)
        async def _on_raw(update: Any) -> None:  # type: ignore[misc]
            name = type(update).__name__
            if "StarGift" not in name and "star_gift" not in name.lower():
                return
            LOGGER.debug("Raw gift update: %s", name)

    async def _handle_gift_message(self, event: events.NewMessage.Event) -> None:
        message = event.message
        action = getattr(message, "action", None)
        if action is None:
            return
        gift_types: tuple[type, ...] = tuple(
            cls
            for cls in (MessageActionStarGift, MessageActionStarGiftUnique)
            if isinstance(cls, type)
        )
        is_gift_action = gift_types and isinstance(action, gift_types)
        if not is_gift_action and "StarGift" not in type(action).__name__:
            return
        peer_user = await event.get_chat()
        recipient = await self._resolve_gift_recipient(action, peer_user)
        if recipient is not None:
            await self._enqueue_user(recipient, "live_gift_received", force=True)

    # ------------------------------------------------------------------
    # Источники кандидатов
    # ------------------------------------------------------------------
    async def bootstrap_queue(self) -> int:
        """Наполняет очередь seed-пользователями, чатами и диалогами."""
        enqueued = 0
        enqueued += await self._enqueue_seeds()
        enqueued += await self._enqueue_contacts()
        enqueued += await self._enqueue_recent_gift_recipients(dialogs=150, messages=40)
        enqueued += await self._enqueue_seed_chats()
        enqueued += await self._enqueue_dialogs(limit=300)
        LOGGER.info("Очередь кандидатов: %s профилей", enqueued)
        return enqueued

    async def refresh_people_queue(self) -> int:
        """Шире круг: новые гифты + свежие диалоги между кругами."""
        count = await self._enqueue_recent_gift_recipients(dialogs=80, messages=25)
        count += await self._enqueue_dialogs(limit=120)
        LOGGER.info("Обновление людей: +%s", count)
        return count

    async def drain_queue(self, *, limit: int = 160) -> AsyncIterator[ProfileSnapshot]:
        """Снимает уже накопленных людей без ожидания live-событий."""
        taken = 0
        while taken < limit:
            try:
                user, source = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            taken += 1
            snapshot = await self.build_snapshot(user, source)
            if snapshot is not None:
                yield snapshot

    @staticmethod
    def _action_recipient_id(action: Any) -> Optional[int]:
        for name in ("peer", "to_id"):
            obj = getattr(action, name, None)
            if obj is None:
                continue
            if isinstance(obj, int) and obj > 0:
                return int(obj)
            uid = getattr(obj, "user_id", None)
            if uid:
                return int(uid)
        return None

    async def _resolve_gift_recipient(self, action: Any, chat: Any) -> Optional[User]:
        recipient_id = self._action_recipient_id(action)
        if self._me_id and recipient_id == self._me_id:
            return None
        if recipient_id:
            if isinstance(chat, User) and chat.id == recipient_id:
                return chat
            for ref in (PeerUser(recipient_id), recipient_id):
                try:
                    entity = await self._flood.call(
                        lambda target=ref: self.client.get_entity(target),
                        label=f"gift_peer:{recipient_id}",
                    )
                except (RPCError, TypeError, ValueError):
                    entity = None
                if isinstance(entity, User):
                    return entity
        if isinstance(chat, User):
            return chat
        return None

    async def _enqueue_user(self, user: User, source: str, *, force: bool = False) -> bool:
        if not isinstance(user, User) or user.bot or user.deleted:
            return False
        if getattr(user, "min", False):
            upgraded = await self._upgrade_min_user(user)
            if upgraded is None:
                return False
            user = upgraded
        if self._me_id and user.id == self._me_id:
            return False
        now = time.time()
        marked = self._seen_at.get(user.id)
        if not force and marked is not None and now - marked < 45 * 60:
            return False
        self._seen_at[user.id] = now
        if len(self._seen_at) > 20_000:
            cutoff = now - 45 * 60
            self._seen_at = {uid: ts for uid, ts in self._seen_at.items() if ts >= cutoff}
        await self._queue.put((user, source))
        return True

    async def _upgrade_min_user(self, user: User) -> Optional[User]:
        """min-юзер из чата без полного профиля — иначе лохи из гифт-групп выпадали."""
        for ref in (user, PeerUser(user.id)):
            try:
                entity = await self._flood.call(
                    lambda target=ref: self.client.get_entity(target),
                    label=f"upgrade:{user.id}",
                )
            except (RPCError, TypeError, ValueError):
                continue
            if isinstance(entity, User) and not entity.bot and not entity.deleted:
                return entity
        if self._access_hash(user):
            return user
        return None

    async def _resolve_user(self, ref: str | int) -> Optional[User]:
        try:
            entity = await self._flood.call(lambda: self.client.get_entity(ref), label=f"get_entity:{ref}")
        except (FloodWaitError, RPCError, ValueError) as exc:
            LOGGER.info("Не удалось резолвить %s: %s", ref, exc)
            return None
        return entity if isinstance(entity, User) else None

    async def _enqueue_contacts(self) -> int:
        """Контакты сессии — живые люди, не витрина маркета."""
        try:
            result = await self._flood.call(
                lambda: self.client(GetContactsRequest(hash=0)),
                label="contacts",
            )
        except (RPCError, TypeError) as exc:
            LOGGER.info("контакты недоступны: %s", exc)
            return 0
        count = 0
        for user in getattr(result, "users", None) or []:
            if isinstance(user, User) and await self._enqueue_user(user, "contact"):
                count += 1
        if count:
            LOGGER.info("Контакты: +%s", count)
        return count

    async def _enqueue_seeds(self) -> int:
        count = 0
        for username in self.settings.seed_usernames:
            user = await self._resolve_user(username.lstrip("@"))
            if user and await self._enqueue_user(user, "seed_username"):
                count += 1
        for user_id in self.settings.seed_user_ids:
            user = await self._resolve_user(user_id)
            if user and await self._enqueue_user(user, "seed_id"):
                count += 1
        return count

    async def _enqueue_seed_chats(self) -> int:
        count = 0
        limit = self.settings.scan_chat_member_limit or None
        for chat_ref in self.settings.seed_chats:
            try:
                entity = await self._flood.call(
                    lambda ref=chat_ref: self.client.get_entity(ref),
                    label=f"chat:{chat_ref}",
                )
            except (RPCError, ValueError) as exc:
                LOGGER.warning("Чат %s недоступен: %s", chat_ref, exc)
                continue
            scanned = 0
            try:
                async for participant in self.client.iter_participants(entity, limit=limit):
                    if await self._enqueue_user(participant, f"chat:{chat_ref}"):
                        count += 1
                    scanned += 1
                    if limit and scanned >= limit:
                        break
            except (ChatAdminRequiredError, ChannelPrivateError, RPCError) as exc:
                LOGGER.warning("iter_participants %s: %s — пропускаем чат", chat_ref, exc)
        return count

    async def _enqueue_recent_gift_recipients(self, *, dialogs: int = 40, messages: int = 18) -> int:
        """Люди, которым недавно прилетел гифт в чатах сессии — не продавцы маркета."""
        count = 0
        try:
            async for dialog in self.client.iter_dialogs(limit=dialogs):
                entity = dialog.entity
                try:
                    async for message in self.client.iter_messages(entity, limit=messages):
                        action = getattr(message, "action", None)
                        if action is None or "StarGift" not in type(action).__name__:
                            continue
                        recipient = await self._resolve_gift_recipient(action, entity)
                        if recipient is not None and await self._enqueue_user(recipient, "recent_gift_peer"):
                            count += 1
                except (RPCError, TypeError, ValueError):
                    continue
        except RPCError as exc:
            LOGGER.warning("недавние гифты: %s", exc)
        LOGGER.info("Недавние гифты в чатах: %s людей", count)
        return count

    async def _enqueue_dialogs(self, limit: int = 200) -> int:
        count = 0
        try:
            async for dialog in self.client.iter_dialogs(limit=limit):
                entity = dialog.entity
                if isinstance(entity, User) and await self._enqueue_user(entity, "dialog"):
                    count += 1
        except RPCError as exc:
            LOGGER.warning("iter_dialogs: %s", exc)
        return count

    # ------------------------------------------------------------------
    # Публичный профиль и подарки
    # ------------------------------------------------------------------
    @staticmethod
    def _access_hash(user: User) -> int:
        return int(getattr(user, "access_hash", 0) or 0)

    async def _input_user(self, user: User) -> Any:
        access_hash = self._access_hash(user)
        if access_hash:
            return InputUser(user.id, access_hash)
        return await self._flood.call(
            lambda: self.client.get_input_entity(user),
            label=f"input:{user.id}",
        )

    async def _input_peer(self, user: User) -> Any:
        access_hash = self._access_hash(user)
        if access_hash:
            return InputPeerUser(user.id, access_hash)
        return await self._flood.call(
            lambda: self.client.get_input_entity(user),
            label=f"peer:{user.id}",
        )

    async def fetch_metrics(self, user: User) -> AccountMetrics:
        registered_at, age_days = account_age_days(user.id)
        bio = ""
        personal_channel_id: Optional[int] = None
        common_chats = 0
        public_channels = 0
        stars_level: Optional[int] = None
        stars_value: Optional[int] = None
        stars_fetched = False
        stargifts_count: Optional[int] = None
        try:
            input_user = await self._input_user(user)
            try:
                request = GetFullUserRequest(id=input_user)
            except TypeError:
                request = GetFullUserRequest(input_user)  # type: ignore[call-arg]
            full = await self._flood.call(
                lambda: self.client(request),
                label=f"full:{user.id}",
            )
            stars_fetched = True
            full_user = getattr(full, "full_user", None) or full
            bio = getattr(full_user, "about", None) or ""
            personal_channel_id = getattr(full_user, "personal_channel_id", None)
            common_chats = int(getattr(full_user, "common_chats_count", 0) or 0)
            rating = getattr(full_user, "stars_rating", None) or getattr(full, "stars_rating", None)
            if rating is not None:
                stars_level = getattr(rating, "level", None)
                stars_value = getattr(rating, "stars", None)
                if stars_level is None:
                    stars_level = getattr(rating, "current_level", None)
            for name in ("stargifts_count", "star_gifts_count"):
                raw_count = getattr(full_user, name, None)
                if raw_count is None:
                    continue
                try:
                    stargifts_count = int(raw_count)
                    break
                except (TypeError, ValueError):
                    continue
            for chat in getattr(full, "chats", []) or []:
                if isinstance(chat, Channel) and getattr(chat, "username", None):
                    public_channels += 1
        except UserPrivacyRestrictedError as exc:
            LOGGER.info("GetFullUser %s: %s", user.id, exc)
            stars_fetched = True
        except (RPCError, asyncio.TimeoutError) as exc:
            LOGGER.info("GetFullUser %s: %s", user.id, exc)

        username = user.username
        is_premium = bool(getattr(user, "premium", False) or getattr(user, "is_premium", False))
        score = compute_activity_score(
            username=username,
            is_premium=is_premium,
            is_verified=bool(getattr(user, "verified", False)),
            has_photo=user.photo is not None,
            bio=bio,
            personal_channel_id=personal_channel_id,
            common_chats_count=common_chats,
            unique_gift_count=0,
            public_channel_count=public_channels,
        )
        try:
            parsed_level = int(stars_level) if stars_level is not None else None
        except (TypeError, ValueError):
            parsed_level = None
        try:
            parsed_stars = int(stars_value) if stars_value is not None else None
        except (TypeError, ValueError):
            parsed_stars = None
        return AccountMetrics(
            user_id=user.id,
            username=username,
            first_name=user.first_name or "",
            last_name=user.last_name or "",
            is_premium=is_premium,
            is_verified=bool(getattr(user, "verified", False)),
            has_photo=user.photo is not None,
            bio=bio,
            personal_channel_id=personal_channel_id,
            common_chats_count=common_chats,
            approx_registered_at=registered_at,
            account_age_days=age_days,
            public_channel_count=public_channels,
            activity_score=score,
            stars_rating_level=parsed_level,
            stars_rating_stars=parsed_stars,
            stars_fetched=stars_fetched,
            stargifts_count=stargifts_count,
            lang_code=(getattr(user, "lang_code", None) or "") or None,
        )

    async def fetch_saved_gifts(
        self,
        user: User,
        *,
        stop_after_unique: Optional[int] = None,
    ) -> tuple[list[UniqueGift], list[RegularGift]]:
        """
        Все unique NFT профиля, включая скрытые с витрины.

        exclude_unsaved не ставим: иначе лох с спрятанной коллекцией выглядит как 1 NFT.
        exclude_unlimited: обычные гифты не забивают страницы — иначе не видим уникальные.
        """
        unique: list[UniqueGift] = []
        regular: list[RegularGift] = []
        offset = ""
        pages = 0
        max_pages = 2 if stop_after_unique is not None else 8
        input_peer = await self._input_peer(user)
        supported = inspect.signature(GetSavedStarGiftsRequest).parameters
        include_hidden = "exclude_unsaved" in supported
        unique_only = "exclude_unlimited" in supported
        while pages < max_pages:
            pages += 1
            kwargs: dict[str, Any] = {
                "peer": input_peer,
                "offset": offset,
                "limit": self.settings.gift_page_size,
            }
            if include_hidden:
                kwargs["exclude_unsaved"] = False
            if unique_only:
                kwargs["exclude_unlimited"] = True
            try:
                result = await self._flood.call(
                    lambda payload=dict(kwargs): self.client(GetSavedStarGiftsRequest(**payload)),
                    label=f"gifts:{user.id}",
                )
            except (RPCError, asyncio.TimeoutError) as exc:
                LOGGER.info("getSavedStarGifts user=%s: %s", user.id, exc)
                if pages == 1:
                    raise
                break

            gifts = getattr(result, "gifts", None) or []
            for saved in gifts:
                parsed_unique, parsed_regular = self._parse_saved_gift(saved)
                if parsed_unique is not None:
                    unique.append(parsed_unique)
                if parsed_regular is not None:
                    regular.append(parsed_regular)
            if stop_after_unique is not None and len(unique) >= stop_after_unique:
                break

            next_offset = getattr(result, "next_offset", None)
            if not next_offset or not gifts:
                break
            offset = next_offset
        return unique, regular

    @staticmethod
    def _gift_is_hidden(saved: Any) -> bool:
        if bool(getattr(saved, "unsaved", False)):
            return True
        if getattr(saved, "saved", None) is False:
            return True
        return False

    def _parse_saved_gift(self, saved: Any) -> tuple[Optional[UniqueGift], Optional[RegularGift]]:
        gift = getattr(saved, "gift", saved)
        gift_type = type(gift).__name__
        if gift_type == "StarGiftUnique" or getattr(gift, "slug", None):
            attributes = list(getattr(gift, "attributes", None) or [])
            model = backdrop = symbol = None
            parsed_attrs: list[GiftAttribute] = []
            for attr in attributes:
                name = type(attr).__name__
                value = getattr(attr, "name", None) or getattr(attr, "title", None)
                permille = getattr(attr, "rarity_permille", None)
                if value:
                    parsed_attrs.append(
                        GiftAttribute(trait=name, value=str(value), rarity_permille=permille)
                    )
                if "Model" in name:
                    model = value
                elif "Backdrop" in name:
                    backdrop = value
                elif "Pattern" in name or "Symbol" in name:
                    symbol = value
            unique = UniqueGift(
                slug=str(getattr(gift, "slug", "") or ""),
                title=str(getattr(gift, "title", "") or ""),
                number=getattr(gift, "num", None),
                gift_id=getattr(gift, "gift_id", None) or getattr(gift, "id", None),
                gift_address=getattr(gift, "gift_address", None),
                owner_address=getattr(gift, "owner_address", None),
                model=str(model) if model else None,
                backdrop=str(backdrop) if backdrop else None,
                symbol=str(symbol) if symbol else None,
                attributes=parsed_attrs,
                availability_issued=getattr(gift, "availability_issued", None),
                availability_total=getattr(gift, "availability_total", None),
                on_resale=bool(getattr(gift, "resell_amount", None)),
                unsaved=self._gift_is_hidden(saved),
            )
            return unique, None

        if isinstance(gift, StarGift) or gift_type == "StarGift":
            regular = RegularGift(
                gift_id=int(getattr(gift, "id", 0) or 0),
                title=str(getattr(gift, "title", None) or getattr(gift, "id", "")),
                stars=getattr(gift, "stars", None),
                limited=bool(getattr(gift, "limited", False)),
                sold_out=bool(getattr(gift, "sold_out", False)),
            )
            return None, regular
        return None, None

    async def _enrich_telegram_floor(self, gift: UniqueGift) -> None:
        """Официальная оценка Telegram: payments.getUniqueStarGiftValueInfo."""
        if GetUniqueStarGiftValueInfoRequest is None or not gift.slug:
            return
        try:
            info = await self._flood.call(
                lambda: self.client(GetUniqueStarGiftValueInfoRequest(slug=gift.slug)),
                retries=2,
                label=f"value:{gift.slug}",
            )
        except RPCError as exc:
            LOGGER.debug("valueInfo %s: %s", gift.slug, exc)
            return
        currency = str(getattr(info, "currency", "") or "").upper()
        floor_raw = getattr(info, "floor_price", None)
        last_sale = getattr(info, "last_sale_price", None)
        value_raw = getattr(info, "value", None)
        if currency in {"TON", "TONCOIN", ""}:
            gift.telegram_floor_ton = to_ton(floor_raw)
            gift.fair_value_ton = to_ton(last_sale) or to_ton(value_raw)
            if gift.telegram_floor_ton is None:
                gift.telegram_floor_ton = gift.fair_value_ton
        gift.listed_count = getattr(info, "listed_count", None)
        gift.fragment_url = getattr(info, "fragment_listed_url", None)

    async def build_snapshot(self, user: User, source: str) -> Optional[ProfileSnapshot]:
        started = time.perf_counter()
        try:
            metrics = await self.fetch_metrics(user)
            if metrics.stargifts_count == 0:
                return None
            unique, regular = await self.fetch_saved_gifts(user, stop_after_unique=4)
            metrics.gifts_fetched = True
            for gift in unique[:2]:
                await self._enrich_telegram_floor(gift)
            total_ton, min_floor, cheap = await self.market.estimate_portfolio(unique)
            if min_floor is None:
                priced = [gift.best_floor_ton for gift in unique if gift.best_floor_ton is not None]
                min_floor = min(priced) if priced else None
                total_ton = min_floor or 0.0
            if not cheap and unique:
                cheap = unique[:1]
            ton_usd = await self.market.get_ton_usd()
            metrics.activity_score = compute_activity_score(
                username=metrics.username,
                is_premium=metrics.is_premium,
                is_verified=metrics.is_verified,
                has_photo=metrics.has_photo,
                bio=metrics.bio,
                personal_channel_id=metrics.personal_channel_id,
                common_chats_count=metrics.common_chats_count,
                unique_gift_count=len(unique),
                public_channel_count=metrics.public_channel_count,
            )
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            return ProfileSnapshot(
                metrics=metrics,
                unique_gifts=unique,
                regular_gifts=regular,
                estimated_value_ton=total_ton,
                estimated_value_usd=total_ton * ton_usd,
                min_floor_ton=min_floor,
                cheap_gifts=cheap,
                ton_usd=ton_usd,
                processed_ms=elapsed_ms,
                source=source,
                fingerprint_key=f"user:{metrics.user_id}",
                listing_key=f"user:{metrics.user_id}",
            )
        except (UserPrivacyRestrictedError, RPCError) as exc:
            LOGGER.debug("snapshot user=%s skip: %s", user.id, exc)
            return None
        except Exception:
            LOGGER.exception("snapshot user=%s failed", user.id)
            return None

    async def scan_iter(
        self,
        *,
        live: bool = True,
        stop_event: asyncio.Event | None = None,
    ) -> AsyncIterator[ProfileSnapshot]:
        """
        Главный генератор: отдаёт готовые снимки профилей по мере обработки.

        Если live=True, после опустошения стартовой очереди ждёт новые события.
        """
        await self.bootstrap_queue()
        idle_rounds = 0
        while stop_event is None or not stop_event.is_set():
            try:
                user, source = await asyncio.wait_for(self._queue.get(), timeout=2.0)
            except asyncio.TimeoutError:
                idle_rounds += 1
                if not live and self._queue.empty():
                    break
                if live and idle_rounds % 15 == 0:
                    LOGGER.info("Ожидание live-событий подарков...")
                continue
            idle_rounds = 0
            snapshot = await self.build_snapshot(user, source)
            if snapshot is not None:
                yield snapshot
