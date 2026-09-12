"""
Асинхронный сканер публичных профилей Telegram и коллекционных подарков.

Источники кандидатов (только то, к чему у сессии уже есть доступ):
1. SEED_USERNAMES / SEED_USER_IDS из конфига.
2. Участники чатов из SEED_CHATS (клиент должен быть участником).
3. Открытые диалоги аккаунта.
4. Live-события: MessageActionStarGift / StarGiftUnique в доступных чатах.

Читаются только подарки, которые пользователь выставил в профиле
(`payments.getSavedStarGifts`). Приватные коллекции Telegram не отдаёт —
это обрабатывается как штатный skip, а не ошибка.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import AsyncIterator
from typing import Any, Optional

from telethon import TelegramClient, events
from telethon.errors import (
    ChannelPrivateError,
    ChatAdminRequiredError,
    FloodWaitError,
    RPCError,
    UserPrivacyRestrictedError,
)
from telethon.sessions import StringSession
from telethon.tl.functions.payments import GetSavedStarGiftsRequest
from telethon.tl.functions.users import GetFullUserRequest
from telethon.tl.types import Channel, InputPeerUser, InputUser, StarGift, User

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


class TelegramFloodControl:
    """FloodWait и зависшие RPC: не спим минутами, при timeout переподключаемся."""

    RPC_TIMEOUT = 18.0

    def __init__(self, limiter: AsyncRateLimiter) -> None:
        self.limiter = limiter
        self.client: Optional[TelegramClient] = None
        self.stop_event: Optional[asyncio.Event] = None
        self._reconnect_lock = asyncio.Lock()
        self._last_reconnect = 0.0
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

    async def ensure_connected(self) -> bool:
        client = self.client
        if client is None:
            return False
        try:
            if not client.is_connected():
                LOGGER.warning("Telegram оффлайн — подключаюсь")
                await asyncio.wait_for(client.connect(), timeout=12)
            if not await asyncio.wait_for(client.is_user_authorized(), timeout=8):
                LOGGER.error("Сессия Telegram слетела — нужен новый TELEGRAM_SESSION")
                return False
            return True
        except Exception as exc:
            LOGGER.warning("Telegram connect: %s", exc)
            return False

    async def _recover(self, label: str) -> None:
        client = self.client
        if client is None:
            return
        async with self._reconnect_lock:
            self._last_reconnect = time.monotonic()
            LOGGER.warning("Telegram reconnect после зависания %s", label)
            try:
                await asyncio.wait_for(client.disconnect(), timeout=3)
            except Exception:
                pass
            try:
                await asyncio.wait_for(client.connect(), timeout=12)
            except Exception as exc:
                LOGGER.warning("reconnect не удался: %s", exc)

    async def _await_rpc(self, factory, label: str) -> Any:
        task = asyncio.create_task(self.limiter.run(factory), name=f"tg:{label}")
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=self.RPC_TIMEOUT)
        except asyncio.TimeoutError:
            LOGGER.warning("Telegram timeout %s — reconnect", label)
            await self._recover(label)
            try:
                await asyncio.wait_for(task, timeout=8)
            except Exception:
                if not task.done():
                    task.cancel()
                    try:
                        await task
                    except Exception:
                        pass
            raise
        except asyncio.CancelledError:
            if not task.done():
                task.cancel()
                try:
                    await task
                except Exception:
                    pass
            raise

    async def call(self, factory, *, retries: int = 4, label: str = "rpc") -> Any:
        last_error: BaseException | None = None
        for attempt in range(retries):
            if self._stopping():
                raise asyncio.CancelledError
            try:
                return await self._await_rpc(factory, label)
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
                    LOGGER.error("Сессия Telegram: %s", exc)
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
    ) -> None:
        self.settings = settings
        self.storage = storage
        self.market = market
        self._limiter = AsyncRateLimiter(settings.telegram_concurrency, min_interval=0.35)
        self._flood = TelegramFloodControl(self._limiter)
        self._seen_ids: set[int] = set()
        self._queue: asyncio.Queue[tuple[User, str]] = asyncio.Queue()
        raw = self.settings.telegram_session.strip()
        if raw:
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
            device_model="TG-Gifts Analytics",
            system_version="Windows 10",
            app_version="1.0.0",
            timeout=15,
            request_retries=1,
            connection_retries=2,
            retry_delay=1,
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

    async def start(self) -> None:
        session_len = len(self.settings.telegram_session.strip())
        LOGGER.info(
            "MTProto: %s",
            f"TELEGRAM_SESSION ({session_len} символов)" if session_len else f"файл {self.settings.session_name}",
        )
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
        LOGGER.info("MTProto клиент вошёл как %s id=%s", _user_display(me), me.id)

    async def close(self) -> None:
        if self.client.is_connected():
            await self.client.disconnect()

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
        sender = await event.get_sender()
        if isinstance(sender, User) and not sender.bot and not sender.deleted:
            await self._queue.put((sender, "live_gift_action"))
        peer_user = await event.get_chat()
        if isinstance(peer_user, User) and not peer_user.bot and not peer_user.deleted:
            await self._queue.put((peer_user, "live_gift_peer"))

    # ------------------------------------------------------------------
    # Источники кандидатов
    # ------------------------------------------------------------------
    async def bootstrap_queue(self) -> int:
        """Наполняет очередь seed-пользователями, чатами и диалогами."""
        enqueued = 0
        enqueued += await self._enqueue_seeds()
        enqueued += await self._enqueue_seed_chats()
        enqueued += await self._enqueue_dialogs()
        LOGGER.info("Очередь кандидатов: %s профилей", enqueued)
        return enqueued

    async def _enqueue_user(self, user: User, source: str) -> bool:
        if not isinstance(user, User) or user.bot or user.deleted or getattr(user, "min", False):
            return False
        if user.id in self._seen_ids:
            return False
        self._seen_ids.add(user.id)
        await self._queue.put((user, source))
        return True

    async def _resolve_user(self, ref: str | int) -> Optional[User]:
        try:
            entity = await self._flood.call(lambda: self.client.get_entity(ref), label=f"get_entity:{ref}")
        except (FloodWaitError, RPCError, ValueError) as exc:
            LOGGER.info("Не удалось резолвить %s: %s", ref, exc)
            return None
        return entity if isinstance(entity, User) else None

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

    async def _enqueue_dialogs(self) -> int:
        count = 0
        try:
            async for dialog in self.client.iter_dialogs():
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
        """
        unique: list[UniqueGift] = []
        regular: list[RegularGift] = []
        offset = ""
        pages = 0
        max_pages = 4 if stop_after_unique is not None else 40
        input_peer = await self._input_peer(user)
        supported = inspect.signature(GetSavedStarGiftsRequest).parameters
        include_hidden = "exclude_unsaved" in supported
        while pages < max_pages:
            pages += 1
            kwargs: dict[str, Any] = {
                "peer": input_peer,
                "offset": offset,
                "limit": self.settings.gift_page_size,
            }
            if include_hidden:
                kwargs["exclude_unsaved"] = False
            try:
                result = await self._flood.call(
                    lambda payload=kwargs: self.client(GetSavedStarGiftsRequest(**payload)),
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
            gift.telegram_floor_ton = to_ton(floor_raw) or to_ton(last_sale) or to_ton(value_raw)
        gift.listed_count = getattr(info, "listed_count", None)
        gift.fragment_url = getattr(info, "fragment_listed_url", None)

    async def build_snapshot(self, user: User, source: str) -> Optional[ProfileSnapshot]:
        started = time.perf_counter()
        try:
            metrics = await self.fetch_metrics(user)
            unique, regular = await self.fetch_saved_gifts(user)
            metrics.gifts_fetched = True
            for gift in unique:
                await self._enrich_telegram_floor(gift)
            total_ton, min_floor, cheap = await self.market.estimate_portfolio(unique)
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
