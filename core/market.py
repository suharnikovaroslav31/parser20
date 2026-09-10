"""
Сканер встроенного маркета Telegram Gifts и MRKT.

Цикл:
1. Каталог коллекций `payments.getStarGifts`.
2. Telegram: свежие лоты + самые дешёвые (две первые страницы на коллекцию).
3. MRKT и Tonnel (жёлтый маркет) — новые и дешёвые лоты.
4. Фильтр: рейтинг ур.1, ≤2 NFT, молодой аккаунт, цена в диапазоне.
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator
from typing import Any, Optional
from urllib.parse import unquote, urlparse, parse_qs

from telethon.errors import RPCError
from telethon.tl.functions.messages import RequestAppWebViewRequest, RequestWebViewRequest
from telethon.tl.functions.payments import GetResaleStarGiftsRequest, GetStarGiftsRequest
from telethon.tl.types import InputBotAppShortName, InputUser, PeerUser, User

from config import Settings
from core.filters import account_age_days, compute_activity_score
from core.listings import ListingTracker
from core.models import AccountMetrics, ProfileSnapshot, UniqueGift, utcnow
from core.parser import ProfileScanner
from core.ton_client import NANOTON, TonMarketClient, to_ton

LOGGER = logging.getLogger("tg_gifts.market")


def _peer_user_id(peer: Any) -> Optional[int]:
    if peer is None:
        return None
    if isinstance(peer, int):
        return peer
    if isinstance(peer, PeerUser):
        return int(peer.user_id)
    return getattr(peer, "user_id", None)


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

    def _stopping(self) -> bool:
        return self.stop_event is not None and self.stop_event.is_set()

    async def _resolve_seller(self, owner_id: Optional[int], users: dict[int, User]) -> Optional[User]:
        if not owner_id:
            return None
        user = users.get(int(owner_id))
        if user is not None and not getattr(user, "min", False) and not user.bot:
            return user
        resolved = await self.scanner._resolve_user(int(owner_id))
        if isinstance(resolved, User):
            return resolved
        return user if isinstance(user, User) else None

    def _lot_key(self, source: str, slug: str, extra: str = "") -> str:
        token = (slug or extra or "").strip()
        return f"{source}:{token}" if token else ""

    async def iter_offers(self) -> AsyncIterator[ProfileSnapshot]:
        telegram_count = 0
        self._skipped_known = 0
        self._tg_priced = 0
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
            async for snapshot in self._iter_telegram_resale():
                if self._stopping():
                    return
                telegram_count += 1
                yield snapshot
            if self._stopping():
                return
            LOGGER.info(
                "Telegram: лотов в цене %s, снимков продавца %s, уже разобранных пропуск %s",
                self._tg_priced,
                telegram_count,
                self._skipped_known,
            )

            mrkt_count = 0
            async for snapshot in self._iter_mrkt_listings():
                if self._stopping():
                    return
                mrkt_count += 1
                yield snapshot
            LOGGER.info("MRKT: снимков продавца %s", mrkt_count)

            tonnel_count = 0
            async for snapshot in self._iter_tonnel_listings():
                if self._stopping():
                    return
                tonnel_count += 1
                yield snapshot
            LOGGER.info("Tonnel: снимков продавца %s", tonnel_count)
            self.tracker.commit_scan()
            finished = True
        except Exception:
            raise
        finally:
            if not finished:
                self.tracker.discard_partial()

    async def _iter_telegram_resale(self) -> AsyncIterator[ProfileSnapshot]:
        catalog = await self._gift_catalog()
        resale_types = [
            item
            for item in catalog
            if getattr(item, "availability_resale", None)
            or getattr(item, "sold_out", False)
            or getattr(item, "limited", False)
        ]
        LOGGER.info("Каталог Telegram Gifts: %s типов, к ресейлу %s", len(catalog), len(resale_types))
        ton_usd = await self.market.get_ton_usd()

        for index, base in enumerate(resale_types, start=1):
            if self._stopping():
                return
            gift_id = int(getattr(base, "id", 0) or 0)
            title = str(getattr(base, "title", "") or gift_id)
            if not gift_id:
                continue
            if index == 1 or index % 25 == 0:
                LOGGER.info("Telegram market %s/%s: %s", index, len(resale_types), title)
            try:
                async for snapshot in self._resale_page(gift_id, title, ton_usd, sort_by_price=False):
                    yield snapshot
                async for snapshot in self._resale_page(gift_id, title, ton_usd, sort_by_price=True):
                    yield snapshot
            except RPCError as exc:
                LOGGER.warning("resale %s (%s): %s", title, gift_id, exc)

    async def _gift_catalog(self) -> list[Any]:
        try:
            result = await self.scanner._flood.call(
                lambda: self.scanner.client(GetStarGiftsRequest(hash=0)),
                label="getStarGifts",
            )
        except RPCError as exc:
            LOGGER.error("getStarGifts: %s", exc)
            return []
        gifts = getattr(result, "gifts", None) or []
        return list(gifts)

    async def _resale_page(
        self,
        gift_id: int,
        title: str,
        ton_usd: float,
        *,
        sort_by_price: bool,
    ) -> AsyncIterator[ProfileSnapshot]:
        """Одна страница: newest (sort_by_price=False) или cheapest."""
        result = await self.scanner._flood.call(
            lambda: self.scanner.client(
                GetResaleStarGiftsRequest(
                    gift_id=gift_id,
                    offset="",
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
                continue
            self._tg_priced += 1
            slug = str(getattr(raw, "slug", "") or "")
            extra = f"{gift_id}:{getattr(raw, 'num', '')}:{getattr(raw, 'id', '')}"
            key = self._lot_key("tg", slug, extra)
            if not self.tracker.observe(key):
                self._skipped_known += 1
                continue
            snapshot = await self._snapshot_from_telegram_lot(raw, users, price, ton_usd)
            if snapshot is not None:
                yield snapshot

    async def _snapshot_from_telegram_lot(
        self,
        raw: Any,
        users: dict[int, User],
        price: float,
        ton_usd: float,
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
        unique.market_source = "telegram_resale"
        owner_id = _peer_user_id(getattr(raw, "owner_id", None))
        unique.seller_id = owner_id
        unique.seller_name = getattr(raw, "owner_name", None)
        user = await self._resolve_seller(owner_id, users)
        inventory = await self._load_profile_nfts(user, unique)
        profile_uniques, regular = inventory
        metrics = await self._metrics_for_seller(user, owner_id, unique.seller_name)
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
            source="tg_market",
            fingerprint_key=f"tg_market:{unique.slug}:{price:.4f}",
            captured_at=utcnow(),
        )

    async def ensure_mrkt_auth(self) -> Optional[str]:
        if self.market.mrkt_token:
            return self.market.mrkt_token
        client = self.scanner.client
        for username in ("mrkt", "MRKT", "tgmrkt"):
            try:
                bot = await self.scanner._flood.call(
                    lambda name=username: client.get_entity(name),
                    label=f"entity:{username}",
                )
            except Exception as exc:
                LOGGER.info("MRKT entity @%s: %s", username, exc)
                continue
            if not isinstance(bot, User) or not bot.access_hash:
                continue
            input_bot = InputUser(bot.id, bot.access_hash)
            for short in ("app", "market", "start"):
                try:
                    web = await self.scanner._flood.call(
                        lambda s=short: client(
                            RequestAppWebViewRequest(
                                peer=bot,
                                app=InputBotAppShortName(bot_id=input_bot, short_name=s),
                                platform="android",
                                write_allowed=True,
                            )
                        ),
                        label=f"mrkt:webview:{short}",
                    )
                except Exception as exc:
                    LOGGER.info("MRKT WebView %s: %s", short, exc)
                    continue
                url = getattr(web, "url", "") or ""
                init_data = _extract_webapp_data(url)
                if not init_data:
                    continue
                token = await self._mrkt_exchange(init_data)
                if token:
                    return token
            try:
                web = await self.scanner._flood.call(
                    lambda: client(
                        RequestWebViewRequest(
                            peer=bot,
                            bot=input_bot,
                            url="https://cdn.tgmrkt.io/",
                            platform="android",
                        )
                    ),
                    label="mrkt:webview:url",
                )
                url = getattr(web, "url", "") or ""
                init_data = _extract_webapp_data(url)
                if init_data:
                    token = await self._mrkt_exchange(init_data)
                    if token:
                        return token
            except Exception as exc:
                LOGGER.info("MRKT RequestWebView: %s", exc)
        LOGGER.warning("MRKT: Mini App сессия не получена, пробую API без токена")
        return None

    async def _mrkt_exchange(self, init_data: str) -> Optional[str]:
        payload = await self.market.http.request_json(
            "POST",
            f"{self.settings.mrkt_api_url.rstrip('/')}/auth",
            json_body={"data": init_data},
            headers={"Referer": "https://cdn.tgmrkt.io/", "Accept": "application/json"},
        )
        token = None
        if isinstance(payload, dict):
            token = payload.get("token") or payload.get("accessToken")
        if token:
            self.market.set_mrkt_token(str(token))
            LOGGER.info("MRKT: сессия Mini App получена")
            return str(token)
        LOGGER.warning("MRKT auth ответ без token: %s", payload)
        return None

    async def _iter_mrkt_listings(self) -> AsyncIterator[ProfileSnapshot]:
        token = await self.ensure_mrkt_auth()
        LOGGER.info("MRKT: %s", "авторизован" if token else "без токена")
        ton_usd = await self.market.get_ton_usd()
        cheap = await self.market.list_mrkt_targets(
            self.live.floor_max_ton,
            min_ton=self.live.floor_min_ton,
            max_pages=10,
        )
        LOGGER.info("MRKT: лотов с маркета %s", len(cheap))
        for item in cheap:
            if self._stopping():
                return
            slug = str(item.get("slug") or item.get("gift_id_string") or "")
            extra = str(item.get("id") or item.get("gift_id") or "")
            key = self._lot_key("mrkt", slug, extra)
            if not self.tracker.observe(key):
                continue
            snapshot = await self._snapshot_from_mrkt(item, ton_usd)
            if snapshot is not None:
                yield snapshot

    async def _iter_tonnel_listings(self) -> AsyncIterator[ProfileSnapshot]:
        ton_usd = await self.market.get_ton_usd()
        items = await self.market.list_tonnel_gifts(
            self.live.floor_max_ton,
            min_ton=self.live.floor_min_ton,
            max_pages=4,
        )
        LOGGER.info("Tonnel: кандидатов (новые+дешёвые): %s", len(items))
        for item in items:
            if self._stopping():
                return
            slug = str(item.get("slug") or "")
            extra = str(item.get("gift_id") or item.get("id") or "")
            if not slug:
                name = str(item.get("name") or item.get("gift_name") or "")
                num = item.get("gift_num") or item.get("number")
                if name and num is not None:
                    compact = "".join(part.capitalize() for part in name.split())
                    slug = f"{compact}-{num}"
            key = self._lot_key("tonnel", slug, extra)
            if not self.tracker.observe(key):
                continue
            snapshot = await self._snapshot_from_tonnel(item, ton_usd, slug)
            if snapshot is not None:
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
        if owner_id_int:
            try:
                entity = await self.scanner._resolve_user(owner_id_int)
                if isinstance(entity, User):
                    user = entity
            except Exception:
                user = None
        metrics = await self._metrics_for_seller(user, owner_id_int, unique.seller_name)
        inventory = await self._load_profile_nfts(user, unique)
        profile_uniques, regular = inventory
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
        if owner_id_int:
            try:
                entity = await self.scanner._resolve_user(owner_id_int)
                if isinstance(entity, User):
                    user = entity
            except Exception:
                user = None
        metrics = await self._metrics_for_seller(user, owner_id_int, unique.seller_name)
        inventory = await self._load_profile_nfts(user, unique)
        profile_uniques, regular = inventory
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
    ) -> tuple[list[UniqueGift], list]:
        """Публичные unique NFT продавца. Пустой список — фильтр сам отвергнет."""
        if user is None or user.bot or getattr(user, "deleted", False):
            return [], []
        try:
            uniques, regular = await self.scanner.fetch_saved_gifts(user)
        except Exception as exc:
            LOGGER.info("gifts профиля %s: %s", getattr(user, "id", "?"), exc)
            return [], []
        return uniques, regular

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
        )


def _extract_webapp_data(url: str) -> str:
    if not url:
        return ""
    if "tgWebAppData=" in url:
        chunk = url.split("tgWebAppData=", 1)[1]
        chunk = chunk.split("&tgWebAppVersion", 1)[0]
        chunk = chunk.split("#", 1)[0]
        return unquote(chunk)
    parsed = urlparse(url.replace("#", "?", 1) if "#" in url and "?" not in url.split("#", 1)[0] else url)
    query = parse_qs(parsed.query)
    fragment = parse_qs(parsed.fragment)
    for pool in (query, fragment):
        if "tgWebAppData" in pool:
            return unquote(pool["tgWebAppData"][0])
    return ""
