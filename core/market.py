"""
Сканер встроенного маркета Telegram Gifts.

Полный обход ресейла: все коллекции, свежие лоты + все дешёвые в диапазоне цены.
Продавцы берутся из result.users (без get_entity по ID).
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import AsyncIterator
from typing import Any, Optional

from telethon.errors import RPCError
from telethon.tl.functions.payments import GetResaleStarGiftsRequest, GetStarGiftsRequest
from telethon.tl.types import PeerUser, User

try:
    from telethon.tl.functions.payments import GetUniqueStarGiftRequest
except ImportError:
    GetUniqueStarGiftRequest = None  # type: ignore[misc,assignment]

from config import Settings
from core.filters import account_age_days, compute_activity_score
from core.listings import ListingTracker
from core.models import AccountMetrics, ProfileSnapshot, UniqueGift, utcnow
from core.parser import ProfileScanner
from core.ton_client import NANOTON, TonMarketClient, to_ton

LOGGER = logging.getLogger("tg_gifts.market")
CHEAP_PAGES = 4
NEW_PAGES = 1
EXTERNAL_LIMIT = 25
_SLUG_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*-\d+$")
_USER_RE = re.compile(r"^[A-Za-z0-9_]{4,32}$")


def _peer_user_id(peer: Any) -> Optional[int]:
    if peer is None:
        return None
    if isinstance(peer, int):
        return peer
    if isinstance(peer, PeerUser):
        return int(peer.user_id)
    return getattr(peer, "user_id", None)


def marketplace_slug(item: dict[str, Any]) -> str:
    """Slug подарка Telegram: PlushPepe-1. Без перебора user id."""
    for key in ("slug", "gift_id_string", "gift_slug", "tg_slug"):
        raw = str(item.get(key) or "").strip()
        if _SLUG_RE.match(raw):
            return raw
    name = str(
        item.get("name")
        or item.get("gift_name")
        or item.get("title")
        or item.get("collection_name")
        or item.get("collectionName")
        or ""
    ).strip()
    num = item.get("gift_num") or item.get("number") or item.get("num") or item.get("tg_id")
    if num is None and "#" in name:
        left, _, right = name.rpartition("#")
        name = left.strip()
        digits = "".join(ch for ch in right if ch.isdigit())
        num = int(digits) if digits else None
    if name and num is not None:
        compact = "".join(part.capitalize() for part in name.replace("-", " ").split())
        if compact:
            return f"{compact}-{int(num)}"
    return ""


def marketplace_username(item: dict[str, Any]) -> Optional[str]:
    owner = item.get("owner") or item.get("seller") or item.get("user") or {}
    candidates = []
    if isinstance(owner, dict):
        candidates.extend(
            [
                owner.get("username"),
                owner.get("telegram_username"),
                owner.get("name"),
            ]
        )
    elif isinstance(owner, str):
        candidates.append(owner)
    candidates.extend(
        [
            item.get("username"),
            item.get("seller_username"),
            item.get("owner_name"),
            item.get("seller_name"),
        ]
    )
    for raw in candidates:
        if not isinstance(raw, str):
            continue
        clean = raw.strip().lstrip("@")
        if _USER_RE.match(clean):
            return clean
    return None


def marketplace_http_price(item: dict[str, Any]) -> Optional[float]:
    return to_ton(
        item.get("sale_price")
        or item.get("salePrice")
        or item.get("price")
        or item.get("sale_price_ton")
        or item.get("sale_price_nano_tons")
        or item.get("ton_price")
        or item.get("price_ton")
    )


def profile_unique_gifts(
    profile_uniques: list[UniqueGift],
    listed: UniqueGift,
    *,
    gifts_fetched: bool,
) -> list[UniqueGift]:
    """NFT только с открытого профиля. Не угадываем, если подарки не прочитались."""
    if not gifts_fetched:
        return []
    if profile_uniques:
        return profile_uniques
    return [listed]


def listing_price_ton(
    gift: Any,
    *,
    ton_usd: float,
    stars_usd: float,
) -> Optional[float]:
    """Цена лота: TON из StarsTonAmount, иначе оценка Stars → TON."""
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
        usd = stars_price * stars_usd
        return usd / ton_usd
    return to_ton(getattr(gift, "value_amount", None))


class GiftMarketScanner:
    """Основной источник данных: маркет Telegram + MRKT."""

    def __init__(self, scanner: ProfileScanner, market: TonMarketClient, settings: Settings, live) -> None:
        self.scanner = scanner
        self.market = market
        self.settings = settings
        self.live = live
        self.stop_event = None
        self.tracker = ListingTracker()
        self._skipped_known = 0
        self._tg_priced = 0
        self._seller_cache: dict[int, tuple[AccountMetrics, list[UniqueGift], list]] = {}
        self._username_cache: dict[str, Optional[User]] = {}
        self.stage = "idle"

    def _stopping(self) -> bool:
        return self.stop_event is not None and self.stop_event.is_set()

    def _seller_from_users(self, owner_id: Optional[int], users: dict[int, User]) -> Optional[User]:
        """Только пользователи из ответа маркета. Без get_entity по ID."""
        if not owner_id:
            return None
        user = users.get(int(owner_id))
        if user is None or user.bot:
            return None
        return user

    def _lot_key(self, source: str, slug: str, extra: str = "") -> str:
        token = (slug or extra or "").strip()
        return f"{source}:{token}" if token else ""

    async def _ton_usd(self) -> float:
        try:
            return float(await asyncio.wait_for(self.market.get_ton_usd(), timeout=8))
        except Exception as exc:
            LOGGER.warning("курс TON: %s", exc)
            return 0.0

    async def iter_offers(self) -> AsyncIterator[ProfileSnapshot]:
        telegram_count = 0
        self._skipped_known = 0
        self._tg_priced = 0
        self._seller_cache = {}
        self._username_cache = {}
        finished = False
        LOGGER.info(
            "Фильтры сейчас: цена %s–%s TON, NFT %s–%s, рейтинг %s–%s, возраст ≤%sд",
            self.live.floor_min_ton,
            self.live.floor_max_ton,
            self.live.min_unique_gifts,
            self.live.max_unique_gifts,
            self.live.stars_rating_min,
            self.live.stars_rating_max,
            self.live.max_account_age_days if self.live.filter_seller_age else "выкл",
        )
        try:
            if not await self.scanner._flood.ensure_connected():
                LOGGER.error("Telegram нет связи — этот проход пропускаю")
                return
            self.stage = "telegram-catalog"
            LOGGER.info("Telegram: каталог коллекций")
            async for snapshot in self._iter_telegram_resale():
                if self._stopping():
                    return
                telegram_count += 1
                yield snapshot
            LOGGER.info(
                "Telegram: лотов в цене %s, снимков продавца %s, повторный пропуск %s",
                self._tg_priced,
                telegram_count,
                self._skipped_known,
            )
            if self._stopping():
                return

            self.stage = "mrkt"
            LOGGER.info("MRKT: HTTP-запрос лотов")
            mrkt_count = 0
            async for snapshot in self._iter_mrkt_listings():
                if self._stopping():
                    return
                mrkt_count += 1
                yield snapshot
            LOGGER.info("MRKT: снимков продавца %s", mrkt_count)
            if self._stopping():
                return

            self.stage = "tonnel"
            LOGGER.info("Tonnel: HTTP-запрос лотов")
            tonnel_count = 0
            async for snapshot in self._iter_tonnel_listings():
                if self._stopping():
                    return
                tonnel_count += 1
                yield snapshot
            LOGGER.info("Tonnel: снимков продавца %s", tonnel_count)
            if self._stopping():
                return

            self.stage = "portal"
            LOGGER.info("Portals: HTTP-запрос лотов")
            portal_count = 0
            async for snapshot in self._iter_portal_listings():
                if self._stopping():
                    return
                portal_count += 1
                yield snapshot
            LOGGER.info("Portals: снимков продавца %s", portal_count)
            if self._stopping():
                return

            self.stage = "getgems"
            LOGGER.info("Getgems: HTTP-запрос лотов")
            getgems_count = 0
            async for snapshot in self._iter_getgems_listings():
                if self._stopping():
                    return
                getgems_count += 1
                yield snapshot
            LOGGER.info("Getgems: снимков продавца %s", getgems_count)
            self.tracker.commit_scan()
            finished = True
            self.stage = "idle"
        except Exception:
            raise
        finally:
            if not finished:
                self.tracker.discard_partial()

    async def _iter_telegram_resale(self) -> AsyncIterator[ProfileSnapshot]:
        if not self.scanner.client.is_connected():
            LOGGER.warning("Telegram клиент не подключён — пропускаю встроенный маркет")
            return
        catalog = await self._gift_catalog()
        resale_types = [
            item
            for item in catalog
            if getattr(item, "availability_resale", None)
            or getattr(item, "sold_out", False)
            or getattr(item, "limited", False)
        ]
        if not resale_types:
            resale_types = list(catalog)
        LOGGER.info("Каталог Telegram Gifts: %s типов, к ресейлу %s", len(catalog), len(resale_types))
        ton_usd = await self._ton_usd()
        total = len(resale_types)
        for index, base in enumerate(resale_types, start=1):
            if self._stopping():
                return
            if self.scanner._flood.cooling:
                LOGGER.info("Telegram flood — останавливаю обход коллекций до следующего круга")
                return
            gift_id = int(getattr(base, "id", 0) or 0)
            title = str(getattr(base, "title", "") or gift_id)
            if not gift_id:
                continue
            LOGGER.info("Telegram market %s/%s: %s", index, total, title)
            self.stage = f"telegram {index}/{total} {title}"
            try:
                async for snapshot in self._resale_collection(gift_id, title, ton_usd):
                    yield snapshot
            except (RPCError, asyncio.TimeoutError, asyncio.CancelledError) as exc:
                if isinstance(exc, asyncio.CancelledError):
                    raise
                LOGGER.warning("resale %s (%s): %s", title, gift_id, exc)

    async def _gift_catalog(self) -> list[Any]:
        try:
            result = await self.scanner._flood.call(
                lambda: self.scanner.client(GetStarGiftsRequest(hash=0)),
                label="getStarGifts",
            )
        except (RPCError, asyncio.TimeoutError) as exc:
            LOGGER.error("getStarGifts: %s", exc)
            return []
        gifts = getattr(result, "gifts", None) or []
        return list(gifts)

    async def _resale_collection(
        self,
        gift_id: int,
        title: str,
        ton_usd: float,
    ) -> AsyncIterator[ProfileSnapshot]:
        async for snapshot in self._resale_pages(gift_id, title, ton_usd, sort_by_price=False, max_pages=NEW_PAGES):
            yield snapshot
        async for snapshot in self._resale_pages(gift_id, title, ton_usd, sort_by_price=True, max_pages=CHEAP_PAGES):
            yield snapshot

    async def _resale_pages(
        self,
        gift_id: int,
        title: str,
        ton_usd: float,
        *,
        sort_by_price: bool,
        max_pages: int,
    ) -> AsyncIterator[ProfileSnapshot]:
        offset = ""
        for _page in range(max_pages):
            if self._stopping():
                return
            result = await self.scanner._flood.call(
                lambda off=offset: self.scanner.client(
                    GetResaleStarGiftsRequest(
                        gift_id=gift_id,
                        offset=off,
                        limit=min(50, self.settings.gift_page_size),
                        sort_by_price=True if sort_by_price else None,
                    )
                ),
                label=f"resale:{gift_id}:{'price' if sort_by_price else 'new'}",
            )
            users = {
                user.id: user
                for user in (getattr(result, "users", None) or [])
                if isinstance(user, User)
            }
            gifts = getattr(result, "gifts", None) or []
            stop_price = False
            for raw in gifts:
                if self._stopping():
                    return
                price = listing_price_ton(
                    raw,
                    ton_usd=ton_usd,
                    stars_usd=self.settings.stars_usd,
                )
                if price is None:
                    continue
                if price < self.live.floor_min_ton or price >= self.live.floor_max_ton:
                    if sort_by_price and price >= self.live.floor_max_ton:
                        stop_price = True
                        break
                    continue
                self._tg_priced += 1
                slug = str(getattr(raw, "slug", "") or "")
                extra = f"{gift_id}:{getattr(raw, 'num', '')}:{getattr(raw, 'id', '')}"
                key = self._lot_key("tg", slug, extra)
                if not self.tracker.should_process(key):
                    self._skipped_known += 1
                    continue
                try:
                    snapshot = await self._snapshot_from_telegram_lot(
                        raw, users, price, ton_usd, source="tg_market"
                    )
                except (RPCError, asyncio.TimeoutError) as exc:
                    LOGGER.info("лот %s %s: %s", title, slug or extra, exc)
                    continue
                if snapshot is not None:
                    if snapshot.metrics.stars_fetched and snapshot.metrics.gifts_fetched:
                        self.tracker.mark(key)
                    yield snapshot
            if stop_price:
                return
            next_offset = str(getattr(result, "next_offset", "") or "")
            if not next_offset or next_offset == offset or not gifts:
                return
            offset = next_offset

    async def _snapshot_from_telegram_lot(
        self,
        raw: Any,
        users: dict[int, User],
        price: float,
        ton_usd: float,
        *,
        source: str = "tg_market",
    ) -> Optional[ProfileSnapshot]:
        started = time.perf_counter()
        unique, _regular = self.scanner._parse_saved_gift(raw)
        if unique is None:
            unique = UniqueGift(
                slug=str(getattr(raw, "slug", "") or ""),
                title=str(getattr(raw, "title", "") or ""),
                number=getattr(raw, "num", None),
                gift_id=getattr(raw, "gift_id", None) or getattr(raw, "id", None),
                gift_address=getattr(raw, "gift_address", None),
            )
        unique.on_resale = True
        unique.telegram_floor_ton = price
        unique.market_floor_ton = price
        unique.market_source = "telegram_resale" if source == "tg_market" else source
        owner_id = _peer_user_id(getattr(raw, "owner_id", None))
        unique.seller_id = owner_id
        unique.seller_name = getattr(raw, "owner_name", None)
        cached = self._seller_cache.get(int(owner_id)) if owner_id else None
        if cached is not None:
            metrics, profile_uniques, regular = cached
        else:
            user = self._seller_from_users(owner_id, users)
            profile_uniques, regular, gifts_ok = await self._load_profile_nfts(user, unique)
            metrics = await self._metrics_for_seller(user, owner_id, unique.seller_name)
            metrics.gifts_fetched = gifts_ok
            if owner_id and metrics.stars_fetched and gifts_ok:
                self._seller_cache[int(owner_id)] = (metrics, profile_uniques, regular)
        profile_uniques = profile_unique_gifts(
            profile_uniques, unique, gifts_fetched=metrics.gifts_fetched
        )
        metrics.activity_score = compute_activity_score(
            username=metrics.username,
            is_premium=metrics.is_premium,
            is_verified=metrics.is_verified,
            has_photo=metrics.has_photo,
            bio=metrics.bio,
            personal_channel_id=metrics.personal_channel_id,
            common_chats_count=metrics.common_chats_count,
            unique_gift_count=len(profile_uniques),
            public_channel_count=metrics.public_channel_count,
        )
        elapsed = (time.perf_counter() - started) * 1000.0
        return ProfileSnapshot(
            metrics=metrics,
            unique_gifts=profile_uniques,
            regular_gifts=regular,
            estimated_value_ton=price,
            estimated_value_usd=price * ton_usd,
            min_floor_ton=price,
            cheap_gifts=[unique],
            ton_usd=ton_usd,
            processed_ms=elapsed,
            source=source,
            fingerprint_key=f"{source}:{unique.slug}:{price:.4f}",
            captured_at=utcnow(),
        )

    async def _user_from_username(self, username: Optional[str]) -> Optional[User]:
        """Публичный @username. Не резолвим числовые Telegram ID."""
        if not username or not _USER_RE.match(username):
            return None
        key = username.lower()
        if key in self._username_cache:
            return self._username_cache[key]
        try:
            entity = await self.scanner._flood.call(
                lambda: self.scanner.client.get_entity(username),
                label=f"user:@{username}",
            )
        except (RPCError, asyncio.TimeoutError, ValueError) as exc:
            LOGGER.info("username @%s: %s", username, exc)
            entity = None
        user = entity if isinstance(entity, User) and not getattr(entity, "bot", False) else None
        self._username_cache[key] = user
        return user

    async def _unique_star_gift(self, slug: str) -> Optional[tuple[Any, dict[int, User]]]:
        if GetUniqueStarGiftRequest is None or not slug:
            return None
        try:
            request = GetUniqueStarGiftRequest(slug=slug)
        except TypeError:
            request = GetUniqueStarGiftRequest(slug)  # type: ignore[call-arg,misc]
        result = await self.scanner._flood.call(
            lambda: self.scanner.client(request),
            label=f"unique:{slug}",
        )
        gift = getattr(result, "gift", None)
        if gift is None:
            return None
        users = {
            user.id: user
            for user in (getattr(result, "users", None) or [])
            if isinstance(user, User)
        }
        return gift, users

    async def _iter_hydrated_listings(
        self,
        source: str,
        items: list[dict[str, Any]],
        ton_usd: float,
    ) -> AsyncIterator[ProfileSnapshot]:
        """Лоты внешних маркетов → slug/username → публичный профиль Telegram."""
        if self.scanner._flood.cooling:
            LOGGER.info("%s: Telegram flood — внешние лоты в этом круге пропускаю", source)
            return
        for item in items[:EXTERNAL_LIMIT]:
            if self._stopping():
                return
            slug = marketplace_slug(item)
            extra = str(item.get("id") or item.get("gift_id") or item.get("address") or "")
            key = self._lot_key(source, slug, extra)
            if not self.tracker.should_process(key):
                self._skipped_known += 1
                continue
            http_price = marketplace_http_price(item)
            snapshot = None
            if slug and not self.scanner._flood.cooling:
                try:
                    fetched = await self._unique_star_gift(slug)
                except RPCError as exc:
                    text = str(exc).upper()
                    if "SLUG" in text:
                        self.tracker.mark(key)
                        continue
                    LOGGER.info("getUniqueStarGift %s: %s", slug, exc)
                except asyncio.TimeoutError:
                    continue
                if fetched is not None:
                    raw, users = fetched
                    price = listing_price_ton(
                        raw,
                        ton_usd=ton_usd,
                        stars_usd=self.settings.stars_usd,
                    ) or http_price
                    if price is None:
                        continue
                    if price < self.live.floor_min_ton or price >= self.live.floor_max_ton:
                        self.tracker.mark(key)
                        continue
                    self._tg_priced += 1
                    snapshot = await self._snapshot_from_telegram_lot(
                        raw, users, price, ton_usd, source=source
                    )
            if snapshot is None:
                user = await self._user_from_username(marketplace_username(item))
                if user is None:
                    continue
                snapshot = await self._snapshot_from_http_user(
                    item, user, source, slug, ton_usd, http_price
                )
            if snapshot is None:
                continue
            if snapshot.metrics.stars_fetched and snapshot.metrics.gifts_fetched:
                self.tracker.mark(key)
            yield snapshot

    async def _snapshot_from_http_user(
        self,
        item: dict[str, Any],
        user: User,
        source: str,
        slug: str,
        ton_usd: float,
        price: Optional[float],
    ) -> Optional[ProfileSnapshot]:
        if price is None:
            return None
        if price < self.live.floor_min_ton or price >= self.live.floor_max_ton:
            return None
        title = str(
            item.get("name")
            or item.get("gift_name")
            or item.get("title")
            or item.get("collection_name")
            or source
        )
        number = item.get("gift_num") or item.get("number") or item.get("num")
        unique = UniqueGift(
            slug=slug or title,
            title=title,
            number=int(number) if number is not None else None,
            on_resale=True,
            market_floor_ton=price,
            telegram_floor_ton=price,
            market_source=source,
            seller_id=user.id,
            seller_name=user.username,
        )
        profile_uniques, regular, gifts_ok = await self._load_profile_nfts(user, unique)
        profile_uniques = profile_unique_gifts(
            profile_uniques, unique, gifts_fetched=gifts_ok
        )
        metrics = await self._metrics_for_seller(user, user.id, user.username)
        metrics.gifts_fetched = gifts_ok
        metrics.activity_score = compute_activity_score(
            username=metrics.username,
            is_premium=metrics.is_premium,
            is_verified=metrics.is_verified,
            has_photo=metrics.has_photo,
            bio=metrics.bio,
            personal_channel_id=metrics.personal_channel_id,
            common_chats_count=metrics.common_chats_count,
            unique_gift_count=len(profile_uniques),
            public_channel_count=metrics.public_channel_count,
        )
        return ProfileSnapshot(
            metrics=metrics,
            unique_gifts=profile_uniques,
            regular_gifts=regular,
            estimated_value_ton=price,
            estimated_value_usd=price * ton_usd,
            min_floor_ton=price,
            cheap_gifts=[unique],
            ton_usd=ton_usd,
            processed_ms=0.0,
            source=source,
            fingerprint_key=f"{source}:{unique.slug}:{price:.4f}",
            captured_at=utcnow(),
        )

    async def _iter_mrkt_listings(self) -> AsyncIterator[ProfileSnapshot]:
        token = self.market.mrkt_token
        LOGGER.info("MRKT: %s", "токен из env" if token else "без токена")
        ton_usd = await self._ton_usd()
        try:
            cheap = await asyncio.wait_for(
                self.market.list_mrkt_targets(
                    self.live.floor_max_ton,
                    min_ton=self.live.floor_min_ton,
                    max_pages=4,
                ),
                timeout=35,
            )
        except asyncio.TimeoutError:
            LOGGER.warning("MRKT HTTP timeout 35s — дальше Telegram")
            cheap = []
        except Exception as exc:
            LOGGER.warning("MRKT HTTP: %s — дальше Telegram", exc)
            cheap = []
        LOGGER.info("MRKT: лотов с маркета %s", len(cheap))
        async for snapshot in self._iter_hydrated_listings("mrkt", cheap, ton_usd):
            yield snapshot

    async def _iter_tonnel_listings(self) -> AsyncIterator[ProfileSnapshot]:
        ton_usd = await self._ton_usd()
        try:
            items = await asyncio.wait_for(
                self.market.list_tonnel_gifts(
                    self.live.floor_max_ton,
                    min_ton=self.live.floor_min_ton,
                    max_pages=4,
                ),
                timeout=35,
            )
        except asyncio.TimeoutError:
            LOGGER.warning("Tonnel HTTP timeout 35s")
            items = []
        except Exception as exc:
            LOGGER.warning("Tonnel HTTP: %s", exc)
            items = []
        LOGGER.info("Tonnel: кандидатов (новые+дешёвые): %s", len(items))
        async for snapshot in self._iter_hydrated_listings("tonnel", items, ton_usd):
            yield snapshot

    async def _iter_portal_listings(self) -> AsyncIterator[ProfileSnapshot]:
        ton_usd = await self._ton_usd()
        try:
            items = await asyncio.wait_for(
                self.market.list_portal_gifts(
                    self.live.floor_max_ton,
                    min_ton=self.live.floor_min_ton,
                ),
                timeout=25,
            )
        except asyncio.TimeoutError:
            LOGGER.warning("Portals HTTP timeout 25s")
            items = []
        except Exception as exc:
            LOGGER.warning("Portals HTTP: %s", exc)
            items = []
        LOGGER.info("Portals: лотов с маркета %s", len(items))
        async for snapshot in self._iter_hydrated_listings("portal", items, ton_usd):
            yield snapshot

    async def _iter_getgems_listings(self) -> AsyncIterator[ProfileSnapshot]:
        ton_usd = await self._ton_usd()
        try:
            items = await asyncio.wait_for(
                self.market.list_getgems_gifts(
                    self.live.floor_max_ton,
                    min_ton=self.live.floor_min_ton,
                ),
                timeout=25,
            )
        except asyncio.TimeoutError:
            LOGGER.warning("Getgems HTTP timeout 25s")
            items = []
        except Exception as exc:
            LOGGER.warning("Getgems HTTP: %s", exc)
            items = []
        LOGGER.info("Getgems: лотов с маркета %s", len(items))
        async for snapshot in self._iter_hydrated_listings("getgems", items, ton_usd):
            yield snapshot

    async def _snapshot_from_mrkt(self, item: dict[str, Any], ton_usd: float) -> Optional[ProfileSnapshot]:
        started = time.perf_counter()
        price = to_ton(
            item.get("sale_price")
            or item.get("salePrice")
            or item.get("price")
            or item.get("sale_price_ton")
            or item.get("sale_price_nano_tons")
        )
        if price is None:
            return None
        if price < self.live.floor_min_ton or price >= self.live.floor_max_ton:
            return None
        title = str(item.get("collection_name") or item.get("collectionName") or item.get("title") or item.get("name") or "")
        number = item.get("number") or item.get("gift_num") or item.get("num")
        slug = str(item.get("slug") or item.get("gift_id_string") or "")
        if not slug and title and number is not None:
            compact = "".join(part.capitalize() for part in title.split())
            slug = f"{compact}-{number}"
        unique = UniqueGift(
            slug=slug or title or "mrkt",
            title=title or "MRKT gift",
            number=int(number) if number is not None else None,
            gift_id=item.get("gift_id") or item.get("giftId"),
            model=item.get("model_name") or item.get("modelName") or item.get("model"),
            backdrop=item.get("backdrop_name") or item.get("backdropName"),
            symbol=item.get("symbol_name") or item.get("symbolName"),
            on_resale=True,
            market_floor_ton=price,
            market_source="mrkt",
            telegram_floor_ton=price,
        )
        owner = item.get("owner") or item.get("seller") or item.get("user") or {}
        if not isinstance(owner, dict):
            owner = {}
        owner_id = owner.get("telegram_id") or owner.get("telegramId") or owner.get("id") or item.get("telegram_id")
        try:
            owner_id_int = int(owner_id) if owner_id else None
        except (TypeError, ValueError):
            owner_id_int = None
        unique.seller_id = owner_id_int
        unique.seller_name = (
            owner.get("username")
            or owner.get("name")
            or item.get("owner_name")
            or item.get("username")
        )
        user = None
        metrics = await self._metrics_for_seller(user, owner_id_int, unique.seller_name)
        profile_uniques, regular, gifts_ok = await self._load_profile_nfts(user, unique)
        metrics.gifts_fetched = gifts_ok
        profile_uniques = profile_unique_gifts(
            profile_uniques, unique, gifts_fetched=gifts_ok
        )
        metrics.activity_score = compute_activity_score(
            username=metrics.username,
            is_premium=metrics.is_premium,
            is_verified=metrics.is_verified,
            has_photo=metrics.has_photo,
            bio=metrics.bio,
            personal_channel_id=metrics.personal_channel_id,
            common_chats_count=metrics.common_chats_count,
            unique_gift_count=len(profile_uniques),
            public_channel_count=metrics.public_channel_count,
        )
        elapsed = (time.perf_counter() - started) * 1000.0
        return ProfileSnapshot(
            metrics=metrics,
            unique_gifts=profile_uniques,
            regular_gifts=regular,
            estimated_value_ton=price,
            estimated_value_usd=price * ton_usd,
            min_floor_ton=price,
            cheap_gifts=[unique],
            ton_usd=ton_usd,
            processed_ms=elapsed,
            source="mrkt",
            fingerprint_key=f"mrkt:{slug}:{price:.4f}",
            captured_at=utcnow(),
        )

    async def _snapshot_from_tonnel(
        self,
        item: dict[str, Any],
        ton_usd: float,
        slug: str,
    ) -> Optional[ProfileSnapshot]:
        started = time.perf_counter()
        price = to_ton(item.get("price") or item.get("sale_price") or item.get("ton_price"))
        if price is None:
            return None
        if price < self.live.floor_min_ton or price >= self.live.floor_max_ton:
            return None
        title = str(item.get("name") or item.get("gift_name") or item.get("title") or "")
        number = item.get("gift_num") or item.get("number") or item.get("num")
        if not slug and title and number is not None:
            compact = "".join(part.capitalize() for part in title.split())
            slug = f"{compact}-{number}"
        unique = UniqueGift(
            slug=slug or title or "tonnel",
            title=title or "Tonnel gift",
            number=int(number) if number is not None else None,
            gift_id=item.get("gift_id") or item.get("id"),
            model=item.get("model"),
            backdrop=item.get("backdrop"),
            symbol=item.get("symbol"),
            on_resale=True,
            market_floor_ton=price,
            market_source="tonnel",
            telegram_floor_ton=price,
        )
        owner = item.get("seller") or item.get("owner") or item.get("user") or {}
        if not isinstance(owner, dict):
            owner_id = owner if isinstance(owner, (int, str)) else item.get("sellerId") or item.get("telegram_id")
            owner = {}
        else:
            owner_id = (
                owner.get("telegram_id")
                or owner.get("id")
                or item.get("sellerId")
                or item.get("telegram_id")
            )
        try:
            owner_id_int = int(owner_id) if owner_id else None
        except (TypeError, ValueError):
            owner_id_int = None
        unique.seller_id = owner_id_int
        unique.seller_name = (
            (owner.get("username") if isinstance(owner, dict) else None)
            or item.get("seller_username")
            or item.get("username")
        )
        user = None
        metrics = await self._metrics_for_seller(user, owner_id_int, unique.seller_name)
        profile_uniques, regular, gifts_ok = await self._load_profile_nfts(user, unique)
        metrics.gifts_fetched = gifts_ok
        profile_uniques = profile_unique_gifts(
            profile_uniques, unique, gifts_fetched=gifts_ok
        )
        metrics.activity_score = compute_activity_score(
            username=metrics.username,
            is_premium=metrics.is_premium,
            is_verified=metrics.is_verified,
            has_photo=metrics.has_photo,
            bio=metrics.bio,
            personal_channel_id=metrics.personal_channel_id,
            common_chats_count=metrics.common_chats_count,
            unique_gift_count=len(profile_uniques),
            public_channel_count=metrics.public_channel_count,
        )
        elapsed = (time.perf_counter() - started) * 1000.0
        return ProfileSnapshot(
            metrics=metrics,
            unique_gifts=profile_uniques,
            regular_gifts=regular,
            estimated_value_ton=price,
            estimated_value_usd=price * ton_usd,
            min_floor_ton=price,
            cheap_gifts=[unique],
            ton_usd=ton_usd,
            processed_ms=elapsed,
            source="tonnel",
            fingerprint_key=f"tonnel:{slug}:{price:.4f}",
            captured_at=utcnow(),
        )

    async def _load_profile_nfts(
        self,
        user: Optional[User],
        listed: UniqueGift,
    ) -> tuple[list[UniqueGift], list, bool]:
        """Все unique NFT, включая скрытые с витрины. False = профиль не открылся."""
        if user is None or user.bot or getattr(user, "deleted", False):
            return [], [], False
        try:
            uniques, regular = await self.scanner.fetch_saved_gifts(user, stop_after_unique=3)
        except Exception as exc:
            LOGGER.info("gifts профиля %s: %s", getattr(user, "id", "?"), exc)
            return [], [], False
        return uniques, regular, True

    async def _metrics_for_seller(
        self,
        user: Optional[User],
        owner_id: Optional[int],
        owner_name: Optional[str],
    ) -> AccountMetrics:
        if user is not None:
            try:
                return await self.scanner.fetch_metrics(user)
            except Exception as exc:
                LOGGER.info("metrics seller %s: %s", user.id, exc)
        uid = int(owner_id or (user.id if user else 0) or 0)
        registered_at, age_days = account_age_days(uid) if uid else (None, None)
        username = None
        first_name = owner_name or "Продавец маркета"
        is_premium = False
        has_photo = False
        if user is not None:
            username = user.username
            first_name = user.first_name or first_name
            is_premium = bool(getattr(user, "premium", False))
            has_photo = user.photo is not None
            uid = user.id
            registered_at, age_days = account_age_days(uid)
        score = compute_activity_score(
            username=username,
            is_premium=is_premium,
            is_verified=bool(getattr(user, "verified", False)) if user else False,
            has_photo=has_photo,
            bio="",
            personal_channel_id=None,
            common_chats_count=0,
            unique_gift_count=1,
            public_channel_count=0,
        )
        return AccountMetrics(
            user_id=uid,
            username=username.lstrip("@") if isinstance(username, str) and username else None,
            first_name=first_name or "",
            last_name=(user.last_name if user else "") or "",
            is_premium=is_premium,
            is_verified=bool(getattr(user, "verified", False)) if user else False,
            has_photo=has_photo,
            bio="",
            personal_channel_id=None,
            common_chats_count=0,
            approx_registered_at=registered_at,
            account_age_days=age_days,
            public_channel_count=0,
            activity_score=score,
            stars_fetched=False,
            gifts_fetched=False,
        )
