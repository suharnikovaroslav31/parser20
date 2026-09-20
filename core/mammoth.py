"""
Новый охотник: новички для перекупа дешёвых лотов.

Источник — самые дешёвые лоты Telegram Gift Marketplace.
Новичок = Stars ур.1, ровно 1 NFT, бедный профиль, цена у флора / чуть выше.
Перекупов и флипперов режем. Одного человека — один раз.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from telethon.errors import FloodWaitError, RPCError, UserPrivacyRestrictedError
from telethon.tl.functions.payments import GetResaleStarGiftsRequest, GetSavedStarGiftsRequest, GetStarGiftsRequest
from telethon.tl.functions.users import GetFullUserRequest
from telethon.tl.types import Channel, InputPeerUser, InputUser, PeerUser, User

from config import Settings
from core.models import AccountMetrics, FilterDecision, GiftAttribute, ProfileSnapshot, UniqueGift, utcnow
from core.runtime import BUILD
from core.session import TelegramAccount

LOGGER = logging.getLogger("tg_gifts.mammoth")

FLOOR_MIN = 0.0
FLOOR_MAX = 10.0
RATING_LEVEL = 1
MIN_NFT = 1
MAX_NFT = 1  # только один NFT — без коллекции
# Дешёвый лот: не дороже флора +15%. Дороже = не для перекупа.
CHEAP_MARKUP = 1.15
MAX_RICHNESS = 40
MAX_STARGIFTS = 4
COLLECTIONS_PER_PASS = 48
CHEAP_PAGES = 3
FLOOR_SAMPLE = 20
FULL_PER_COLLECTION = 50
NANOTON = 1_000_000_000
SEEN_PATH = Path("data/seen_mammoths.json")
SEEN_TTL = 7 * 24 * 3600

_CYRILLIC = re.compile(r"[А-Яа-яЁёІіЇїЄєҐґЎў]")
_FOREIGN = re.compile(
    r"[\u4e00-\u9fff\u3400-\u4dbf\u3040-\u30ff\uac00-\ud7af\u0600-\u06ff\u0e00-\u0e7f\u0900-\u097f]"
)
_FOREIGN_LANG = {
    "en", "zh", "ar", "tr", "es", "pt", "de", "fr", "id", "hi", "th", "vi",
    "ko", "ja", "fa", "it", "pl", "nl", "ro", "ms", "fil", "he", "el",
}
_TRADER_NICK = re.compile(
    r"(nft|нфт|gifts?|гифт|resale|ресейл|tonnel|portals?|mrkt|fragment|floor|flip|snipe|getgems)",
    re.IGNORECASE,
)
_RESELLER_BIO = re.compile(
    r"(\bnft\b|нфт|resale|ресейл|tonnel|portals?|getgems|\bmrkt\b|fragment|"
    r"скупк|купл[юи]\s*(нфт|nft)|продам\s*(нфт|nft)|t\.me/nft)",
    re.IGNORECASE,
)
_COMMON_LATIN = {
    "alex", "max", "mike", "anna", "ivan", "john", "david", "daniel", "maria",
    "olga", "nick", "kevin", "chris", "james", "robert", "michael", "sarah",
    "kate", "lisa", "tom", "tim", "adam", "mark", "paul", "peter", "jack",
    "leo", "artem", "kirill", "andrey", "sergey", "dmitry", "alexander", "dima",
}

# грубая шкала id → дата для карточки
_ID_ANCHORS = (
    (1, datetime(2013, 8, 14, tzinfo=timezone.utc)),
    (100_000_000, datetime(2016, 1, 1, tzinfo=timezone.utc)),
    (600_000_000, datetime(2018, 7, 1, tzinfo=timezone.utc)),
    (1_500_000_000, datetime(2021, 1, 1, tzinfo=timezone.utc)),
    (5_000_000_000, datetime(2023, 1, 1, tzinfo=timezone.utc)),
    (8_000_000_000, datetime(2025, 1, 1, tzinfo=timezone.utc)),
)


def _age_for(user_id: int) -> tuple[Optional[datetime], Optional[int]]:
    if user_id <= 0:
        return None, None
    anchors = _ID_ANCHORS
    if user_id <= anchors[0][0]:
        registered = anchors[0][1]
    elif user_id >= anchors[-1][0]:
        registered = anchors[-1][1]
    else:
        registered = anchors[0][1]
        for i in range(len(anchors) - 1):
            left_id, left_dt = anchors[i]
            right_id, right_dt = anchors[i + 1]
            if left_id <= user_id <= right_id:
                span = right_id - left_id or 1
                ratio = (user_id - left_id) / span
                registered = left_dt + (right_dt - left_dt) * ratio
                break
    days = max(0, int((datetime.now(timezone.utc) - registered).total_seconds() // 86400))
    return registered, days


def listing_price_ton(gift: Any, *, stars_usd: float = 0.013, ton_usd: float = 5.0) -> Optional[float]:
    amounts = getattr(gift, "resell_amount", None) or []
    ton_price: Optional[float] = None
    stars_price: Optional[float] = None
    for amount in amounts:
        name = type(amount).__name__
        raw = getattr(amount, "amount", None)
        if raw is None:
            continue
        if "Ton" in name:
            ton_price = float(raw) / NANOTON if float(raw) >= 1_000_000 else float(raw)
        else:
            nanos = float(getattr(amount, "nanos", 0) or 0)
            stars_price = float(raw) + nanos / NANOTON
    if ton_price is not None:
        return ton_price
    if stars_price is not None and ton_usd > 0 and stars_usd > 0:
        return (stars_price * stars_usd) / ton_usd
    return None


def at_floor(ask: Optional[float], floor: Optional[float], *, band: float = 0.08) -> bool:
    if ask is None or floor is None or floor <= 0 or ask <= 0:
        return False
    return abs(ask - floor) / floor <= band


def is_cheap_lot(ask: Optional[float], floor: Optional[float]) -> bool:
    """Дешёвый для перекупа: у флора или чуть выше, не наценка."""
    if ask is None or ask <= 0:
        return False
    if floor is None or floor <= 0:
        return ask < FLOOR_MAX
    return ask <= floor * CHEAP_MARKUP


def profile_richness(metrics: AccountMetrics) -> int:
    """У новичка профиль бедный; у прошаренного — жирный."""
    score = 0
    if metrics.username:
        score += 20
    if metrics.is_premium:
        score += 20
    if metrics.is_verified:
        score += 15
    if metrics.has_photo:
        score += 10
    if (metrics.bio or "").strip():
        score += 10
    if metrics.personal_channel_id:
        score += 15
    score += min(10, max(0, metrics.public_channel_count) * 5)
    score += min(10, max(0, metrics.common_chats_count) * 2)
    return max(0, min(100, score))


def looks_like_shell(metrics: AccountMetrics) -> bool:
    return (not metrics.username) and (not (metrics.bio or "").strip()) and (not metrics.has_photo)


def looks_russian(first: str, last: str, bio: str, lang: Optional[str]) -> bool:
    base = (lang or "").strip().lower().replace("_", "-").split("-", 1)[0]
    if base in {"ru", "uk", "be", "kk"}:
        return True
    text = f"{first or ''} {last or ''} {bio or ''}"
    if _CYRILLIC.search(text):
        return True
    if base in _FOREIGN_LANG:
        return False
    if _FOREIGN.search(text):
        return False
    return True


def is_burner(first: str, last: str = "") -> bool:
    joined = f"{first or ''} {last or ''}".strip()
    if not joined or _CYRILLIC.search(joined):
        return False
    tokens = re.findall(r"[A-Za-z]+", joined)
    if any(token.lower() in _COMMON_LATIN for token in tokens):
        return False
    letters = [ch.lower() for ch in joined if ch.isalpha()]
    if len(letters) < 10:
        return False
    vowels = sum(1 for ch in letters if ch in "aeiouy")
    if vowels / len(letters) <= 0.32:
        return True
    if len(tokens) >= 2 and all(len(token) >= 6 for token in tokens[:2]):
        return bool(re.search(r"[bcdfghjklmnpqrstvwxz]{4}", joined, re.IGNORECASE))
    return False


def peer_user_id(peer: Any) -> Optional[int]:
    if peer is None:
        return None
    if isinstance(peer, int):
        return peer
    if isinstance(peer, PeerUser):
        return int(peer.user_id)
    return getattr(peer, "user_id", None)


def lot_owner_id(raw: Any) -> Optional[int]:
    gift = getattr(raw, "gift", None)
    for obj in (raw, gift):
        if obj is None:
            continue
        uid = peer_user_id(getattr(obj, "owner_id", None))
        if uid:
            return uid
    return None


@dataclass
class MammothVerdict:
    ok: bool
    reasons: list[str]


def is_mammoth(
    metrics: AccountMetrics,
    unique: list[UniqueGift],
    ask: float,
    floor: Optional[float] = None,
) -> MammothVerdict:
    fail: list[str] = []
    if not metrics.stars_fetched:
        fail.append("профиль не открыт")
    if not metrics.gifts_fetched:
        fail.append("NFT не прочитаны")
    if metrics.is_verified:
        fail.append("verified")
    if not looks_russian(metrics.first_name, metrics.last_name, metrics.bio, metrics.lang_code):
        fail.append("не русский")
    if _FOREIGN.search(f"{metrics.first_name or ''} {metrics.last_name or ''}"):
        fail.append("чужой скрипт")
    if is_burner(metrics.first_name, metrics.last_name):
        fail.append("рандомное имя")
    if looks_like_shell(metrics):
        fail.append("пустой акк — альт")
    if _TRADER_NICK.search(metrics.username or ""):
        fail.append("юзернейм перекупа")
    if _RESELLER_BIO.search(metrics.bio or ""):
        fail.append("био перекупа")
    richness = profile_richness(metrics)
    if richness > MAX_RICHNESS:
        fail.append(f"профиль шарит {richness} > {MAX_RICHNESS}")
    if metrics.stargifts_count is not None and metrics.stargifts_count > MAX_STARGIFTS:
        fail.append(f"гифтов {metrics.stargifts_count} > {MAX_STARGIFTS}")
    hidden = [g for g in unique if g.unsaved]
    if hidden:
        fail.append(f"скрытые NFT: {len(hidden)}")
    visible = [g for g in unique if not g.unsaved]
    n = len(visible)
    if n < MIN_NFT:
        fail.append(f"NFT {n} < {MIN_NFT}")
    if n > MAX_NFT:
        fail.append(f"NFT {n} > {MAX_NFT}")
    on_sale = [g for g in visible if g.on_resale]
    if len(on_sale) >= 2:
        fail.append("несколько на ресейле — флиппер")
    level = metrics.stars_rating_level
    if level is None:
        fail.append("рейтинг скрыт")
    elif int(level) != RATING_LEVEL:
        fail.append(f"рейтинг ур.{level} ≠ {RATING_LEVEL}")
    if ask < FLOOR_MIN or ask >= FLOOR_MAX:
        fail.append(f"цена {ask:g} вне 0–10")
    if floor is not None and not is_cheap_lot(ask, floor):
        fail.append(f"дорого для перекупа {ask:g} > флора×{CHEAP_MARKUP:g} ({floor:g})")
    if fail:
        return MammothVerdict(False, fail)
    return MammothVerdict(
        True,
        [
            f"новичок: ур.{level}, {n} NFT, дешёвый {ask:g} TON"
            + (f" (флор {floor:g})" if floor else "")
            + f", профиль {richness}/100, акк ~{metrics.account_age_days}д"
        ],
    )


class SeenPeople:
    def __init__(self, path: Path = SEEN_PATH) -> None:
        self.path = path
        self._done: dict[str, float] = {}
        self._load()

    def seen(self, user_id: Optional[int]) -> bool:
        if not user_id:
            return False
        key = str(int(user_id))
        marked = self._done.get(key)
        if marked is None:
            return False
        if time.time() - marked >= SEEN_TTL:
            self._done.pop(key, None)
            return False
        return True

    def mark(self, user_id: int) -> None:
        self._done[str(int(user_id))] = time.time()

    def release(self, user_id: int) -> None:
        self._done.pop(str(int(user_id)), None)

    def save(self) -> None:
        now = time.time()
        self._done = {k: v for k, v in self._done.items() if now - v < SEEN_TTL}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"keys": self._done}, ensure_ascii=False), encoding="utf-8")
        LOGGER.info("уже слали мамонтов: %s", len(self._done))

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        keys = raw.get("keys") if isinstance(raw, dict) else None
        now = time.time()
        if isinstance(keys, dict):
            self._done = {
                str(k): float(v) for k, v in keys.items() if k and now - float(v) < SEEN_TTL
            }


class MammothHunter:
    """Telegram NEW → профиль → только мамонт."""

    def __init__(self, account: TelegramAccount, settings: Settings) -> None:
        self.account = account
        self.settings = settings
        self.seen = SeenPeople()
        self.stop_event: Optional[asyncio.Event] = None
        self.stage = "idle"
        self._floors: dict[int, float] = {}
        self._catalog_offset = 0
        self._seller_cache: dict[int, tuple[AccountMetrics, list[UniqueGift]]] = {}

    def _stopping(self) -> bool:
        return self.stop_event is not None and self.stop_event.is_set()

    async def iter_mammoths(self) -> AsyncIterator[FilterDecision]:
        self.stage = "telegram"
        self._seller_cache.clear()
        if not await self.account.flood.ensure_connected():
            LOGGER.error("нет Telegram — проход skip")
            return
        LOGGER.info(
            "охота на новичков: дешёвые лоты, ур.%s, ровно %s NFT, ≤%.0f%% флора, профиль ≤%s",
            RATING_LEVEL,
            MAX_NFT,
            CHEAP_MARKUP * 100,
            MAX_RICHNESS,
        )
        matched = 0
        try:
            async for decision in self._scan_resale():
                if self._stopping():
                    return
                matched += 1
                yield decision
        finally:
            self.seen.save()
            self.stage = "idle"
            LOGGER.info("круг закончен, мамонтов %s", matched)

    async def _scan_resale(self) -> AsyncIterator[FilterDecision]:
        try:
            result = await self.account.flood.call(
                lambda: self.account.client(GetStarGiftsRequest(hash=0)),
                label="catalog",
            )
        except (RPCError, asyncio.TimeoutError) as exc:
            LOGGER.error("каталог: %s", exc)
            return
        catalog = list(getattr(result, "gifts", None) or [])
        types = [
            item
            for item in catalog
            if getattr(item, "availability_resale", None)
            or getattr(item, "sold_out", False)
            or getattr(item, "limited", False)
        ] or catalog
        types.sort(key=lambda item: int(getattr(item, "stars", 10**9) or 10**9))
        total = len(types)
        if not total:
            LOGGER.warning("каталог пуст")
            return
        start = self._catalog_offset % total
        batch = [types[(start + i) % total] for i in range(min(COLLECTIONS_PER_PASS, total))]
        self._catalog_offset = (start + len(batch)) % total
        LOGGER.info("коллекций %s, круг %s с %s", total, len(batch), start + 1)
        for index, base in enumerate(batch, start=1):
            if self._stopping():
                return
            gift_id = int(getattr(base, "id", 0) or 0)
            title = str(getattr(base, "title", "") or gift_id)
            if not gift_id:
                continue
            self.stage = f"telegram {index}/{len(batch)} {title}"
            LOGGER.info("коллекция %s/%s: %s", index, len(batch), title)
            try:
                async for decision in self._scan_collection(gift_id, title):
                    yield decision
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOGGER.warning("коллекция %s: %s", title, exc)
                if self.account.flood.cooling:
                    left = max(0.0, self.account.flood.cool_until - time.monotonic())
                    await asyncio.sleep(min(max(left, 2.0), 8.0))

    async def _learn_floor(self, gift_id: int, title: str) -> Optional[float]:
        known = self._floors.get(gift_id)
        if known is not None:
            return known
        try:
            result = await self.account.flood.call(
                lambda: self.account.client(
                    GetResaleStarGiftsRequest(
                        gift_id=gift_id,
                        offset="",
                        limit=FLOOR_SAMPLE,
                        sort_by_price=True,
                    )
                ),
                label=f"floor:{gift_id}",
            )
        except (FloodWaitError, RPCError, asyncio.TimeoutError) as exc:
            LOGGER.info("флор %s: %s", title, exc)
            return None
        prices = []
        for raw in getattr(result, "gifts", None) or []:
            price = listing_price_ton(raw, stars_usd=self.settings.stars_usd)
            if price and price > 0:
                prices.append(price)
        if not prices:
            return None
        floor = min(prices)
        self._floors[gift_id] = floor
        LOGGER.info("флор %s = %.2f — беру дешёвые ≤%.0f%% флора", title, floor, CHEAP_MARKUP * 100)
        return floor

    async def _scan_collection(self, gift_id: int, title: str) -> AsyncIterator[FilterDecision]:
        floor = await self._learn_floor(gift_id, title)
        if floor is not None and floor >= FLOOR_MAX:
            LOGGER.info("коллекция %s: флор %.2f ≥ %.0f — skip", title, floor, FLOOR_MAX)
            return
        left = FULL_PER_COLLECTION
        offset = ""
        for _ in range(CHEAP_PAGES):
            if self._stopping() or left <= 0:
                return
            result = await self.account.flood.call(
                lambda off=offset: self.account.client(
                    GetResaleStarGiftsRequest(
                        gift_id=gift_id,
                        offset=off,
                        limit=min(50, self.settings.gift_page_size),
                        sort_by_price=True,
                    )
                ),
                label=f"cheap:{gift_id}",
            )
            users = {
                u.id: u
                for u in (getattr(result, "users", None) or [])
                if isinstance(u, User)
            }
            hit_expensive = False
            for raw in getattr(result, "gifts", None) or []:
                if self._stopping() or left <= 0:
                    return
                price = listing_price_ton(raw, stars_usd=self.settings.stars_usd)
                if price is None or price < FLOOR_MIN:
                    continue
                if price >= FLOOR_MAX or (floor is not None and not is_cheap_lot(price, floor)):
                    hit_expensive = True
                    break
                owner_id = lot_owner_id(raw)
                if owner_id and self.seen.seen(owner_id):
                    continue
                user = users.get(int(owner_id)) if owner_id else None
                if user is None or user.bot:
                    continue
                first = user.first_name or ""
                last = user.last_name or ""
                if is_burner(first, last) or _FOREIGN.search(f"{first} {last}"):
                    continue
                left -= 1
                decision = await self._open_seller(user, raw, price, gift_id, floor)
                if decision is not None:
                    yield decision
            if hit_expensive:
                return
            next_offset = str(getattr(result, "next_offset", "") or "")
            if not next_offset or next_offset == offset:
                return
            offset = next_offset

    def _parse_unique(self, saved: Any) -> Optional[UniqueGift]:
        gift = getattr(saved, "gift", saved)
        if not (type(gift).__name__ == "StarGiftUnique" or getattr(gift, "slug", None)):
            return None
        model = backdrop = symbol = None
        attrs: list[GiftAttribute] = []
        for attr in getattr(gift, "attributes", None) or []:
            name = type(attr).__name__
            value = getattr(attr, "name", None) or getattr(attr, "title", None)
            if value:
                attrs.append(GiftAttribute(trait=name, value=str(value), rarity_permille=getattr(attr, "rarity_permille", None)))
            if "Model" in name:
                model = value
            elif "Backdrop" in name:
                backdrop = value
            elif "Pattern" in name or "Symbol" in name:
                symbol = value
        unsaved = bool(getattr(saved, "unsaved", False)) or getattr(saved, "saved", None) is False
        return UniqueGift(
            slug=str(getattr(gift, "slug", "") or ""),
            title=str(getattr(gift, "title", "") or ""),
            number=getattr(gift, "num", None),
            gift_id=getattr(gift, "gift_id", None) or getattr(gift, "id", None),
            gift_address=getattr(gift, "gift_address", None),
            model=str(model) if model else None,
            backdrop=str(backdrop) if backdrop else None,
            symbol=str(symbol) if symbol else None,
            attributes=attrs,
            on_resale=bool(getattr(gift, "resell_amount", None)),
            unsaved=unsaved,
        )

    async def _fetch_metrics(self, user: User) -> AccountMetrics:
        registered, age = _age_for(int(user.id))
        bio = ""
        personal = None
        common = 0
        publics = 0
        level = None
        stars = None
        fetched = False
        count = None
        try:
            access = int(getattr(user, "access_hash", 0) or 0)
            input_user = InputUser(user.id, access) if access else await self.account.client.get_input_entity(user)
            try:
                req = GetFullUserRequest(id=input_user)
            except TypeError:
                req = GetFullUserRequest(input_user)  # type: ignore[call-arg]
            full = await self.account.flood.call(
                lambda: self.account.client(req),
                retries=3,
                label=f"full:{user.id}",
            )
            fetched = True
            full_user = getattr(full, "full_user", None) or full
            bio = getattr(full_user, "about", None) or ""
            personal = getattr(full_user, "personal_channel_id", None)
            common = int(getattr(full_user, "common_chats_count", 0) or 0)
            rating = getattr(full_user, "stars_rating", None) or getattr(full, "stars_rating", None)
            if rating is not None:
                level = getattr(rating, "level", None) or getattr(rating, "current_level", None)
                stars = getattr(rating, "stars", None)
            for name in ("stargifts_count", "star_gifts_count"):
                raw = getattr(full_user, name, None)
                if raw is not None:
                    try:
                        count = int(raw)
                        break
                    except (TypeError, ValueError):
                        pass
            for chat in getattr(full, "chats", []) or []:
                if isinstance(chat, Channel) and getattr(chat, "username", None):
                    publics += 1
        except UserPrivacyRestrictedError:
            fetched = True
        except FloodWaitError as exc:
            wait = int(getattr(exc, "seconds", 30) or 30)
            LOGGER.info("GetFullUser %s flood %ss", user.id, wait)
        except (RPCError, asyncio.TimeoutError, TypeError, ValueError) as exc:
            LOGGER.info("GetFullUser %s: %s", user.id, exc)
        try:
            level_i = int(level) if level is not None else None
        except (TypeError, ValueError):
            level_i = None
        try:
            stars_i = int(stars) if stars is not None else None
        except (TypeError, ValueError):
            stars_i = None
        return AccountMetrics(
            user_id=user.id,
            username=user.username,
            first_name=user.first_name or "",
            last_name=user.last_name or "",
            is_premium=bool(getattr(user, "premium", False)),
            is_verified=bool(getattr(user, "verified", False)),
            has_photo=user.photo is not None,
            bio=bio,
            personal_channel_id=personal,
            common_chats_count=common,
            approx_registered_at=registered,
            account_age_days=age,
            public_channel_count=publics,
            activity_score=0,
            stars_rating_level=level_i,
            stars_rating_stars=stars_i,
            stars_fetched=fetched,
            stargifts_count=count,
            lang_code=(getattr(user, "lang_code", None) or "") or None,
        )

    async def _fetch_gifts(self, user: User) -> tuple[list[UniqueGift], bool]:
        try:
            access = int(getattr(user, "access_hash", 0) or 0)
            peer = InputPeerUser(user.id, access) if access else await self.account.client.get_input_entity(user)
            kwargs: dict[str, Any] = {"peer": peer, "offset": "", "limit": 20}
            supported = inspect.signature(GetSavedStarGiftsRequest).parameters
            if "exclude_unsaved" in supported:
                kwargs["exclude_unsaved"] = False
            if "exclude_unlimited" in supported:
                kwargs["exclude_unlimited"] = True
            result = await self.account.flood.call(
                lambda: self.account.client(GetSavedStarGiftsRequest(**kwargs)),
                retries=2,
                label=f"gifts:{user.id}",
            )
        except Exception as exc:
            LOGGER.info("gifts %s: %s", user.id, exc)
            return [], False
        uniques: list[UniqueGift] = []
        for saved in getattr(result, "gifts", None) or []:
            parsed = self._parse_unique(saved)
            if parsed is not None:
                uniques.append(parsed)
        return uniques, True

    async def _open_seller(
        self,
        user: User,
        raw: Any,
        price: float,
        collection_id: int,
        floor: Optional[float],
    ) -> Optional[FilterDecision]:
        uid = int(user.id)
        if self.seen.seen(uid):
            return None
        cached = self._seller_cache.get(uid)
        if cached is not None:
            metrics, profile = cached
            if not profile and (
                metrics.stars_rating_level != RATING_LEVEL
                or profile_richness(metrics) > MAX_RICHNESS
                or (metrics.stargifts_count is not None and metrics.stargifts_count > MAX_STARGIFTS)
            ):
                return None
        else:
            metrics = await self._fetch_metrics(user)
            if not metrics.stars_fetched:
                return None
            if metrics.stars_rating_level != RATING_LEVEL:
                self._seller_cache[uid] = (metrics, [])
                return None
            if metrics.stargifts_count is not None and metrics.stargifts_count > MAX_STARGIFTS:
                self._seller_cache[uid] = (metrics, [])
                return None
            if profile_richness(metrics) > MAX_RICHNESS:
                self._seller_cache[uid] = (metrics, [])
                return None
            profile, ok = await self._fetch_gifts(user)
            metrics.gifts_fetched = ok
            if metrics.stars_fetched:
                self._seller_cache[uid] = (metrics, profile)
        listed = self._parse_unique(raw)
        if listed is None:
            listed = UniqueGift(
                slug=str(getattr(raw, "slug", "") or ""),
                title=str(getattr(raw, "title", "") or ""),
                number=getattr(raw, "num", None),
                on_resale=True,
            )
        listed.on_resale = True
        listed.market_floor_ton = price
        listed.telegram_floor_ton = floor if floor is not None else self._floors.get(collection_id)
        listed.market_source = "telegram"
        listed.seller_id = uid
        if not profile:
            profile = [listed]
            metrics.gifts_fetched = True
        elif not any(g.slug == listed.slug for g in profile if listed.slug):
            profile = [listed] + profile
        use_floor = listed.telegram_floor_ton
        verdict = is_mammoth(metrics, profile, price, use_floor)
        if not verdict.ok:
            LOGGER.info("SKIP %s %s", uid, "; ".join(verdict.reasons[:3]))
            return None
        LOGGER.info("MATCH %s %s", uid, verdict.reasons[0])
        snap = ProfileSnapshot(
            metrics=metrics,
            unique_gifts=[g for g in profile if not g.unsaved] or [listed],
            regular_gifts=[],
            estimated_value_ton=price,
            estimated_value_usd=0.0,
            min_floor_ton=price,
            cheap_gifts=[listed],
            ton_usd=0.0,
            processed_ms=0.0,
            source="tg_market",
            fingerprint_key=f"mammoth:{uid}",
            listing_key=f"tg:{listed.slug or uid}",
            captured_at=utcnow(),
        )
        return FilterDecision(
            matched=True,
            reasons=verdict.reasons,
            snapshot=snap,
            cheapest_gift=listed,
        )
