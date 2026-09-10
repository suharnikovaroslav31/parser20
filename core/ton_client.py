"""
Интеграция с открытыми API экосистемы TON:

* Getgems Read API + GraphQL — floor price, активные листинги, NFT по адресу.
* MRKT (api.tgmrkt.io) — коллекции Telegram Gifts и аномально дешёвые лоты.
* Portal Market — дополнительный источник floor.
* Fragment — публичные страницы NFT-юзернеймов / номеров.
* TON Center + TonAPI — состояние аккаунта и on-chain NFT.

Все HTTP-вызовы идут через Semaphore и exponential backoff
(`core.rate_limit.HttpClient`), чтобы соблюдать лимиты, а не обходить их.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

from tonsdk.utils import Address

from config import Settings
from core.models import UniqueGift
from core.rate_limit import HttpClient

LOGGER = logging.getLogger("tg_gifts.ton")

NANOTON = 1_000_000_000
GETGEMS_FLOOR_QUERY = """
query NftCollectionFloor($address: String!) {
  nftCollectionByAddress(address: $address) {
    name
    address
    approximateItemsCount
    floorPrice
  }
}
"""
GETGEMS_NFT_QUERY = """
query NftItem($address: String!) {
  nftItemByAddress(address: $address) {
    name
    address
    collection { name address }
    sale { fullPrice price }
  }
}
"""


def to_ton(value: Any) -> Optional[float]:
    """Нормализует цену из nanoTON / строки / словаря Getgems к float TON."""
    if value is None or value == "":
        return None
    if isinstance(value, dict):
        for key in ("value", "amount", "fullPrice", "price", "nano", "nanoton"):
            if key in value:
                return to_ton(value[key])
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        # Getgems/TonAPI часто отдают nanoTON (>= 1e6). Маленькие числа — уже TON.
        if number >= 1_000_000:
            return number / NANOTON
        return number
    text = str(value).strip().replace(" ", "").replace(",", ".")
    text = re.sub(r"[^0-9.]", "", text)
    if not text:
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    if number >= 1_000_000:
        return number / NANOTON
    return number


def safe_address(raw: str | None) -> Optional[str]:
    """Приводит адрес к user-friendly bounceable (EQ...) для ссылок Getgems."""
    if not raw:
        return None
    try:
        return Address(raw).to_string(True, True, True)
    except Exception:
        return raw


class TonMarketClient:
    """Единая точка доступа к Getgems / MRKT / Fragment / TON RPC."""

    def __init__(self, settings: Settings, storage: Optional[Any] = None) -> None:
        self.settings = settings
        self.storage = storage
        self.http = HttpClient(
            timeout_sec=settings.http_timeout_sec,
            max_retries=settings.http_max_retries,
            concurrency=settings.market_concurrency,
        )
        self._mrkt_index: dict[str, dict[str, Any]] | None = None
        self._mrkt_token = (settings.mrkt_auth_token or "").strip()

    @property
    def mrkt_token(self) -> str:
        return self._mrkt_token

    def set_mrkt_token(self, token: str) -> None:
        self._mrkt_token = token.strip()

    def _mrkt_headers(self) -> dict[str, str]:
        headers = {"Referer": "https://cdn.tgmrkt.io/", "Accept": "application/json"}
        if self._mrkt_token:
            headers["Authorization"] = self._mrkt_token
        return headers

    async def __aenter__(self) -> "TonMarketClient":
        await self.http.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self.http.close()

    # ------------------------------------------------------------------
    # Кэш
    # ------------------------------------------------------------------
    async def _cached(self, key: str, ttl: int, factory) -> Any:
        if self.storage is not None:
            hit = await self.storage.cache_json_get(key)
            if hit is not None:
                return hit
        value = await factory()
        if self.storage is not None and value is not None:
            await self.storage.cache_json_set(key, value, ttl)
        return value

    # ------------------------------------------------------------------
    # Курс TON/USD
    # ------------------------------------------------------------------
    async def get_ton_usd(self) -> float:
        ttl = self.settings.rate_cache_ttl_sec

        async def _load() -> float:
            # 1) TonAPI
            try:
                headers = {}
                if self.settings.tonapi_key:
                    headers["Authorization"] = f"Bearer {self.settings.tonapi_key}"
                payload = await self.http.request_json(
                    "GET",
                    f"{self.settings.tonapi_base_url.rstrip('/')}/v2/rates",
                    headers=headers or None,
                    params={"tokens": "TON", "currencies": "USD"},
                )
                if isinstance(payload, dict):
                    rates = payload.get("rates") or payload
                    ton = rates.get("TON") or rates.get("ton")
                    if isinstance(ton, dict):
                        prices = ton.get("prices") or {}
                        usd = prices.get("USD") or prices.get("usd")
                        if usd:
                            return float(usd)
            except Exception as exc:
                LOGGER.warning("TonAPI rates недоступен: %s", exc)

            # 2) CoinGecko (публичный, без ключа)
            payload = await self.http.request_json(
                "GET",
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": "the-open-network", "vs_currencies": "usd"},
            )
            if isinstance(payload, dict):
                usd = (payload.get("the-open-network") or {}).get("usd")
                if usd:
                    return float(usd)
            LOGGER.warning("Не удалось получить курс TON/USD, используем 0")
            return 0.0

        value = await self._cached("tg:ton_usd", ttl, _load)
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    # ------------------------------------------------------------------
    # Getgems
    # ------------------------------------------------------------------
    def _getgems_headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.settings.getgems_api_key:
            headers["Authorization"] = f"Bearer {self.settings.getgems_api_key}"
        return headers

    async def getgems_collection_floor(self, collection_address: str) -> Optional[float]:
        """Минимальная цена коллекции: Read API on-sale, затем GraphQL."""
        address = safe_address(collection_address)
        if not address:
            return None
        cache_key = f"tg:floor:getgems:{address}"

        async def _load() -> Optional[float]:
            base = self.settings.getgems_api_url.rstrip("/")
            headers = self._getgems_headers()

            # Read API: активные фикс-прайс листинги.
            try:
                payload = await self.http.request_json(
                    "GET",
                    f"{base}/public-api/v1/nfts/on-sale/{address}",
                    headers=headers,
                    params={"limit": 30},
                )
                floor = self._min_listing_price(payload)
                if floor is not None:
                    return floor
            except Exception as exc:
                LOGGER.info("Getgems on-sale %s: %s", address, exc)

            # GraphQL витрины getgems.io — публичный источник статистики.
            try:
                payload = await self.http.request_json(
                    "POST",
                    self.settings.getgems_graphql_url,
                    json_body={
                        "query": GETGEMS_FLOOR_QUERY,
                        "variables": {"address": address},
                    },
                )
                if isinstance(payload, dict):
                    data = ((payload.get("data") or {}).get("nftCollectionByAddress")) or {}
                    floor = to_ton(data.get("floorPrice"))
                    if floor is not None:
                        return floor
            except Exception as exc:
                LOGGER.info("Getgems GraphQL floor %s: %s", address, exc)
            return None

        cached = await self._cached(cache_key, self.settings.floor_cache_ttl_sec, _load)
        return to_ton(cached)

    async def getgems_nft_listing(self, nft_address: str) -> dict[str, Any]:
        """Карточка конкретного NFT: цена листинга, коллекция, имя."""
        address = safe_address(nft_address)
        if not address:
            return {}
        cache_key = f"tg:nft:getgems:{address}"

        async def _load() -> dict[str, Any]:
            result: dict[str, Any] = {"address": address}
            base = self.settings.getgems_api_url.rstrip("/")
            headers = self._getgems_headers()
            try:
                payload = await self.http.request_json(
                    "GET",
                    f"{base}/public-api/v1/nfts/{address}",
                    headers=headers,
                )
                if isinstance(payload, dict):
                    item = payload.get("response") or payload.get("item") or payload
                    if isinstance(item, dict):
                        result.update(self._extract_nft_card(item))
            except Exception as exc:
                LOGGER.info("Getgems NFT REST %s: %s", address, exc)

            if result.get("price_ton") is None:
                try:
                    payload = await self.http.request_json(
                        "POST",
                        self.settings.getgems_graphql_url,
                        json_body={"query": GETGEMS_NFT_QUERY, "variables": {"address": address}},
                    )
                    if isinstance(payload, dict):
                        item = ((payload.get("data") or {}).get("nftItemByAddress")) or {}
                        result.update(self._extract_nft_card(item))
                except Exception as exc:
                    LOGGER.info("Getgems NFT GraphQL %s: %s", address, exc)
            return result

        cached = await self._cached(cache_key, self.settings.floor_cache_ttl_sec, _load)
        return cached if isinstance(cached, dict) else {}

    async def getgems_owner_nfts(self, owner_address: str) -> list[dict[str, Any]]:
        """On-chain NFT, принадлежащие адресу (после withdraw подарка на TON)."""
        address = safe_address(owner_address)
        if not address:
            return []
        base = self.settings.getgems_api_url.rstrip("/")
        try:
            payload = await self.http.request_json(
                "GET",
                f"{base}/public-api/v1/nfts/owner/{address}",
                headers=self._getgems_headers(),
                params={"limit": 100},
            )
        except Exception as exc:
            LOGGER.info("Getgems owner NFTs %s: %s", address, exc)
            return []
        items = self._as_items(payload)
        return [self._extract_nft_card(item) for item in items if isinstance(item, dict)]

    @staticmethod
    def _as_items(payload: Any) -> list[Any]:
        if payload is None:
            return []
        if isinstance(payload, list):
            return payload
        if not isinstance(payload, dict):
            return []
        for key in ("items", "nfts", "response", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
            if isinstance(value, dict):
                nested = value.get("items") or value.get("nfts")
                if isinstance(nested, list):
                    return nested
        return []

    def _min_listing_price(self, payload: Any) -> Optional[float]:
        prices: list[float] = []
        for item in self._as_items(payload):
            if not isinstance(item, dict):
                continue
            card = self._extract_nft_card(item)
            price = card.get("price_ton")
            if isinstance(price, (int, float)):
                prices.append(float(price))
        return min(prices) if prices else None

    @staticmethod
    def _extract_nft_card(item: dict[str, Any]) -> dict[str, Any]:
        sale = item.get("sale") or item.get("auction") or {}
        price = to_ton(
            sale.get("fullPrice")
            or sale.get("price")
            or sale.get("minBid")
            or sale.get("lastBidAmount")
            or item.get("price")
            or item.get("floorPrice")
        )
        collection = item.get("collection") or {}
        return {
            "address": item.get("address") or item.get("nftAddress"),
            "name": item.get("name") or item.get("title"),
            "collection_address": collection.get("address") or item.get("collectionAddress"),
            "collection_name": collection.get("name") or item.get("collectionName"),
            "owner": item.get("ownerAddress") or item.get("owner"),
            "price_ton": price,
            "url": item.get("url"),
        }

    # ------------------------------------------------------------------
    # MRKT / Portal — маркетплейсы Telegram Gifts
    # ------------------------------------------------------------------
    async def _mrkt_collections(self) -> dict[str, dict[str, Any]]:
        if self._mrkt_index is not None:
            return self._mrkt_index

        async def _load() -> list[dict[str, Any]]:
            headers = self._mrkt_headers()
            payload = await self.http.request_json(
                "GET",
                f"{self.settings.mrkt_api_url.rstrip('/')}/gifts/collections",
                headers=headers,
            )
            if isinstance(payload, list):
                return payload
            if isinstance(payload, dict):
                for key in ("collections", "items", "data"):
                    value = payload.get(key)
                    if isinstance(value, list):
                        return value
            return []

        try:
            raw = await self._cached("tg:mrkt:collections", self.settings.floor_cache_ttl_sec, _load)
        except Exception as exc:
            LOGGER.warning("MRKT collections недоступны: %s", exc)
            raw = []
        index: dict[str, dict[str, Any]] = {}
        if isinstance(raw, list):
            for item in raw:
                if not isinstance(item, dict):
                    continue
                names = [
                    item.get("name"),
                    item.get("title"),
                    item.get("collectionName"),
                    item.get("collection_title"),
                ]
                for name in names:
                    if name:
                        index[str(name).strip().lower()] = item
        self._mrkt_index = index
        return index

    async def mrkt_collection_floor(self, collection_name: str) -> Optional[float]:
        if not collection_name:
            return None
        index = await self._mrkt_collections()
        item = index.get(collection_name.strip().lower())
        if not item:
            return None
        return to_ton(
            item.get("floor_price_nano_tons")
            or item.get("floorPriceNanoTons")
            or item.get("floor_price_ton")
            or item.get("floorPrice")
            or item.get("floor")
        )

    async def mrkt_cheapest_listing(self, collection_name: str) -> Optional[float]:
        """Самый дешёвый активный лот коллекции (поиск аномалий цены)."""
        if not collection_name:
            return None
        cache_key = f"tg:mrkt:floor:{collection_name.strip().lower()}"

        async def _load() -> Optional[float]:
            if not self.mrkt_token:
                return await self.mrkt_collection_floor(collection_name)
            headers = self._mrkt_headers()
            body = {
                "collectionNames": [collection_name],
                "modelNames": [],
                "backdropNames": [],
                "symbolNames": [],
                "ordering": "Price",
                "lowToHigh": True,
                "maxPrice": None,
                "minPrice": None,
                "count": 20,
                "cursor": "",
                "query": None,
                "promotedFirst": False,
            }
            try:
                payload = await self.http.request_json(
                    "POST",
                    f"{self.settings.mrkt_api_url.rstrip('/')}/gifts/saling",
                    headers=headers,
                    json_body=body,
                )
            except Exception as exc:
                LOGGER.info("MRKT saling %s: %s", collection_name, exc)
                return await self.mrkt_collection_floor(collection_name)
            gifts = []
            if isinstance(payload, dict):
                gifts = payload.get("gifts") or payload.get("items") or []
            prices: list[float] = []
            for gift in gifts:
                if not isinstance(gift, dict):
                    continue
                price = to_ton(
                    gift.get("sale_price")
                    or gift.get("salePrice")
                    or gift.get("price")
                    or gift.get("sale_price_ton")
                )
                if price is not None:
                    prices.append(price)
            if prices:
                return min(prices)
            return await self.mrkt_collection_floor(collection_name)

        cached = await self._cached(cache_key, self.settings.floor_cache_ttl_sec, _load)
        return to_ton(cached)

    async def list_recent_gifts(
        self,
        max_ton: float,
        *,
        min_ton: float = 0.0,
        max_pages: int = 8,
    ) -> list[dict[str, Any]]:
        """Свежие лоты MRKT (новые выставления), затем фильтр по цене."""
        found: list[dict[str, Any]] = []
        cursor = ""
        ordering = "Date"
        for page in range(max_pages):
            body = {
                "collectionNames": [],
                "modelNames": [],
                "backdropNames": [],
                "symbolNames": [],
                "ordering": ordering,
                "lowToHigh": False,
                "maxPrice": None,
                "minPrice": None,
                "count": 20,
                "cursor": cursor,
                "query": None,
                "promotedFirst": False,
            }
            try:
                payload = await self.http.request_json(
                    "POST",
                    f"{self.settings.mrkt_api_url.rstrip('/')}/gifts/saling",
                    headers=self._mrkt_headers(),
                    json_body=body,
                )
            except Exception as exc:
                LOGGER.warning("MRKT recent /gifts/saling (%s): %s", ordering, exc)
                if page == 0 and ordering == "Date":
                    ordering = "Latest"
                    continue
                break
            gifts: list[Any] = []
            next_cursor = ""
            if isinstance(payload, dict):
                gifts = payload.get("gifts") or payload.get("items") or []
                next_cursor = str(payload.get("cursor") or "")
            elif isinstance(payload, list):
                gifts = payload
            if not gifts:
                if page == 0 and ordering == "Date":
                    ordering = "Latest"
                    cursor = ""
                    continue
                break
            stop_old = False
            for gift in gifts:
                if not isinstance(gift, dict):
                    continue
                price = to_ton(
                    gift.get("sale_price")
                    or gift.get("salePrice")
                    or gift.get("price")
                    or gift.get("sale_price_ton")
                    or gift.get("sale_price_nano_tons")
                )
                if price is None:
                    continue
                if price < min_ton or price >= max_ton:
                    continue
                found.append(gift)
            if not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor
            if page >= 2 and len(found) == 0:
                stop_old = True
            if stop_old:
                break
        return found

    async def list_mrkt_targets(
        self,
        max_ton: float,
        *,
        min_ton: float = 0.0,
        max_pages: int = 10,
    ) -> list[dict[str, Any]]:
        """MRKT: свежие выставления + самые дешёвые, без дублей."""
        recent = await self.list_recent_gifts(max_ton, min_ton=min_ton, max_pages=max_pages)
        cheap = await self.list_cheap_gifts(max_ton, max_pages=max_pages)
        merged: dict[str, dict[str, Any]] = {}
        for gift in recent + cheap:
            if not isinstance(gift, dict):
                continue
            price = to_ton(
                gift.get("sale_price")
                or gift.get("salePrice")
                or gift.get("price")
                or gift.get("sale_price_ton")
                or gift.get("sale_price_nano_tons")
            )
            if price is None or price < min_ton or price >= max_ton:
                continue
            key = str(
                gift.get("slug")
                or gift.get("gift_id_string")
                or gift.get("id")
                or gift.get("gift_id")
                or ""
            )
            if not key:
                continue
            prev = merged.get(key)
            if prev is None or (to_ton(prev.get("sale_price") or prev.get("price")) or 1e9) > price:
                merged[key] = gift
        return list(merged.values())

    async def list_tonnel_gifts(
        self,
        max_ton: float,
        *,
        min_ton: float = 0.0,
        max_pages: int = 4,
    ) -> list[dict[str, Any]]:
        """Tonnel (жёлтый маркет): новые и самые дешёвые лоты."""
        base = self.settings.tonnel_api_url.rstrip("/")
        headers = {
            "Origin": "https://market.tonnel.network",
            "Referer": "https://market.tonnel.network/",
            "Accept": "application/json",
        }
        common_filter = {
            "price": {"$exists": True},
            "refunded": {"$ne": True},
            "buyer": {"$exists": False},
            "export_at": {"$exists": True},
            "asset": "TON",
        }
        lo = max(0, int(min_ton))
        hi = max(lo + 1, int(max_ton) if max_ton >= 1 else 10)
        sorts = (
            {"message_post_time": -1, "gift_id": -1},
            {"price": 1, "gift_id": -1},
        )
        merged: dict[str, dict[str, Any]] = {}
        range_value: list[int] | None = [lo, hi]
        for sort in sorts:
            for page in range(1, max_pages + 1):
                body = {
                    "page": page,
                    "limit": 30,
                    "sort": json.dumps(sort),
                    "filter": json.dumps(common_filter),
                    "price_range": range_value,
                    "user_auth": "",
                }
                try:
                    payload = await self.http.request_json(
                        "POST",
                        f"{base}/api/pageGifts",
                        headers=headers,
                        json_body=body,
                    )
                except Exception as exc:
                    LOGGER.warning("Tonnel pageGifts: %s", exc)
                    break
                if payload is None and range_value is not None:
                    LOGGER.info("Tonnel: price_range отклонён, запрашиваю без него")
                    range_value = None
                    body["price_range"] = None
                    try:
                        payload = await self.http.request_json(
                            "POST",
                            f"{base}/api/pageGifts",
                            headers=headers,
                            json_body=body,
                        )
                    except Exception as exc:
                        LOGGER.warning("Tonnel pageGifts: %s", exc)
                        break
                if payload is None:
                    break
                gifts: list[Any] = []
                if isinstance(payload, list):
                    gifts = payload
                elif isinstance(payload, dict):
                    gifts = payload.get("gifts") or payload.get("data") or payload.get("items") or []
                if not gifts:
                    break
                for gift in gifts:
                    if not isinstance(gift, dict):
                        continue
                    price = to_ton(gift.get("price") or gift.get("sale_price") or gift.get("ton_price"))
                    if price is None or price < min_ton or price >= max_ton:
                        continue
                    name = str(gift.get("name") or gift.get("gift_name") or "")
                    num = gift.get("gift_num") or gift.get("number")
                    key = str(gift.get("gift_id") or gift.get("id") or f"{name}-{num}")
                    prev = merged.get(key)
                    if prev is None or (to_ton(prev.get("price")) or 1e9) > price:
                        merged[key] = gift
        LOGGER.info("Tonnel: уникальных лотов в диапазоне %s", len(merged))
        return list(merged.values())

    async def list_cheap_gifts(self, max_ton: float, *, max_pages: int = 10) -> list[dict[str, Any]]:
        """Все активные лоты MRKT дешевле порога, цена по возрастанию."""
        nano = int(max_ton * NANOTON)
        cursor = ""
        found: list[dict[str, Any]] = []
        use_max_price = True
        for _page in range(max_pages):
            body = {
                "collectionNames": [],
                "modelNames": [],
                "backdropNames": [],
                "symbolNames": [],
                "ordering": "Price",
                "lowToHigh": True,
                "maxPrice": nano if use_max_price else None,
                "minPrice": None,
                "count": 20,
                "cursor": cursor,
                "query": None,
                "promotedFirst": False,
            }
            try:
                payload = await self.http.request_json(
                    "POST",
                    f"{self.settings.mrkt_api_url.rstrip('/')}/gifts/saling",
                    headers=self._mrkt_headers(),
                    json_body=body,
                )
            except Exception as exc:
                LOGGER.warning("MRKT /gifts/saling: %s", exc)
                break
            gifts: list[Any] = []
            next_cursor = ""
            if isinstance(payload, dict):
                gifts = payload.get("gifts") or payload.get("items") or []
                next_cursor = str(payload.get("cursor") or "")
            elif isinstance(payload, list):
                gifts = payload
            if not gifts:
                if use_max_price and _page == 0:
                    use_max_price = False
                    continue
                break
            stop = False
            for gift in gifts:
                if not isinstance(gift, dict):
                    continue
                price = to_ton(
                    gift.get("sale_price")
                    or gift.get("salePrice")
                    or gift.get("price")
                    or gift.get("sale_price_ton")
                    or gift.get("sale_price_nano_tons")
                )
                if price is None:
                    found.append(gift)
                    continue
                if price >= max_ton:
                    stop = True
                    break
                found.append(gift)
            if stop or not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor
        return found

    async def portal_collection_floor(self, collection_name: str) -> Optional[float]:
        if not collection_name:
            return None
        cache_key = f"tg:portal:{collection_name.strip().lower()}"

        async def _load() -> Optional[float]:
            payload = await self.http.request_json(
                "GET",
                f"{self.settings.portal_api_url.rstrip('/')}/collections",
                params={"search": collection_name, "limit": 10},
            )
            collections: list[Any] = []
            if isinstance(payload, dict):
                collections = payload.get("collections") or payload.get("items") or []
            elif isinstance(payload, list):
                collections = payload
            needle = collection_name.strip().lower()
            for item in collections:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or item.get("title") or "").strip().lower()
                if name == needle or needle in name:
                    return to_ton(item.get("floor_price") or item.get("floorPrice") or item.get("floor"))
            return None

        try:
            cached = await self._cached(cache_key, self.settings.floor_cache_ttl_sec, _load)
            return to_ton(cached)
        except Exception as exc:
            LOGGER.info("Portal floor %s: %s", collection_name, exc)
            return None

    # ------------------------------------------------------------------
    # Fragment — публичные страницы юзернеймов и номеров
    # ------------------------------------------------------------------
    async def fragment_username(self, username: str) -> dict[str, Any]:
        """Оценка NFT-юзернейма по публичной странице fragment.com/username/<name>."""
        clean = username.lstrip("@").strip()
        if not clean:
            return {}
        cache_key = f"tg:fragment:user:{clean.lower()}"

        async def _load() -> dict[str, Any]:
            url = f"{self.settings.fragment_base_url.rstrip('/')}/username/{clean}"
            html = await self.http.request_json("GET", url, accept_text=True)
            if not isinstance(html, str):
                return {"username": clean, "url": url, "available": False}
            return self._parse_fragment_page(html, kind="username", ident=clean, url=url)

        try:
            cached = await self._cached(cache_key, self.settings.floor_cache_ttl_sec, _load)
            return cached if isinstance(cached, dict) else {}
        except Exception as exc:
            LOGGER.info("Fragment username %s: %s", clean, exc)
            return {"username": clean, "error": str(exc)}

    async def fragment_number(self, number: str) -> dict[str, Any]:
        clean = re.sub(r"\D", "", number)
        if not clean:
            return {}
        cache_key = f"tg:fragment:num:{clean}"

        async def _load() -> dict[str, Any]:
            url = f"{self.settings.fragment_base_url.rstrip('/')}/number/{clean}"
            html = await self.http.request_json("GET", url, accept_text=True)
            if not isinstance(html, str):
                return {"number": clean, "url": url}
            return self._parse_fragment_page(html, kind="number", ident=clean, url=url)

        try:
            cached = await self._cached(cache_key, self.settings.floor_cache_ttl_sec, _load)
            return cached if isinstance(cached, dict) else {}
        except Exception as exc:
            LOGGER.info("Fragment number %s: %s", clean, exc)
            return {"number": clean, "error": str(exc)}

    @staticmethod
    def _parse_fragment_page(html: str, *, kind: str, ident: str, url: str) -> dict[str, Any]:
        """Достаёт цену/статус из публичного HTML Fragment без авторизации."""
        result: dict[str, Any] = {kind: ident, "url": url}
        lowered = html.lower()
        if "sold" in lowered:
            result["status"] = "sold"
        elif "on auction" in lowered or "auction" in lowered:
            result["status"] = "auction"
        elif "available" in lowered:
            result["status"] = "available"
        else:
            result["status"] = "unknown"

        amounts = re.findall(
            r"([0-9]+(?:\.[0-9]+)?)\s*(?:ton|💎)",
            html,
            flags=re.IGNORECASE,
        )
        if amounts:
            try:
                result["price_ton"] = float(amounts[0].replace(",", ""))
            except ValueError:
                pass
        bid = re.search(r'"minimum_bid"\s*:\s*"?([0-9.]+)"?', html)
        if bid:
            result["price_ton"] = to_ton(bid.group(1)) or result.get("price_ton")
        return result

    # ------------------------------------------------------------------
    # TON RPC (TON Center) + TonAPI
    # ------------------------------------------------------------------
    async def toncenter_account(self, address: str) -> dict[str, Any]:
        friendly = safe_address(address)
        if not friendly:
            return {}
        params: dict[str, str | int | float] = {"address": friendly}
        if self.settings.toncenter_api_key:
            params["api_key"] = self.settings.toncenter_api_key
        try:
            payload = await self.http.request_json(
                "GET",
                f"{self.settings.toncenter_api_url.rstrip('/')}/getAddressInformation",
                params=params,
            )
        except Exception as exc:
            LOGGER.info("TON Center %s: %s", friendly, exc)
            return {}
        if isinstance(payload, dict):
            return payload.get("result") or payload
        return {}

    async def tonapi_account_nfts(self, address: str) -> list[dict[str, Any]]:
        friendly = safe_address(address)
        if not friendly:
            return []
        headers = {}
        if self.settings.tonapi_key:
            headers["Authorization"] = f"Bearer {self.settings.tonapi_key}"
        try:
            payload = await self.http.request_json(
                "GET",
                f"{self.settings.tonapi_base_url.rstrip('/')}/v2/accounts/{friendly}/nfts",
                headers=headers or None,
                params={"limit": 50, "indirect_ownership": "false"},
            )
        except Exception as exc:
            LOGGER.info("TonAPI NFTs %s: %s", friendly, exc)
            return []
        if isinstance(payload, dict):
            items = payload.get("nft_items") or payload.get("nfts") or []
            return items if isinstance(items, list) else []
        return []

    # ------------------------------------------------------------------
    # Агрегация floor для конкретного подарка
    # ------------------------------------------------------------------
    async def enrich_gift(self, gift: UniqueGift) -> UniqueGift:
        """Подтягивает floor Getgems/MRKT/Portal и выбирает минимум."""
        quotes: list[tuple[str, float]] = []

        if gift.telegram_floor_ton is not None:
            quotes.append(("telegram", gift.telegram_floor_ton))

        if gift.gift_address:
            card = await self.getgems_nft_listing(gift.gift_address)
            price = to_ton(card.get("price_ton"))
            if price is not None:
                quotes.append(("getgems_item", price))
            collection = card.get("collection_address")
            if collection:
                floor = await self.getgems_collection_floor(str(collection))
                if floor is not None:
                    quotes.append(("getgems", floor))

        mrkt = await self.mrkt_cheapest_listing(gift.title)
        if mrkt is not None:
            quotes.append(("mrkt", mrkt))

        portal = await self.portal_collection_floor(gift.title)
        if portal is not None:
            quotes.append(("portal", portal))

        if quotes:
            source, price = min(quotes, key=lambda pair: pair[1])
            gift.market_floor_ton = price
            gift.market_source = source
        return gift

    async def estimate_portfolio(self, gifts: list[UniqueGift]) -> tuple[float, Optional[float], list[UniqueGift]]:
        """Суммарная оценка unique-подарков и список «дешёвых» относительно порога."""
        total = 0.0
        floors: list[float] = []
        cheap: list[UniqueGift] = []
        threshold = self.settings.floor_threshold_ton
        for gift in gifts:
            await self.enrich_gift(gift)
            floor = gift.best_floor_ton
            if floor is None:
                continue
            total += floor
            floors.append(floor)
            if floor < threshold:
                cheap.append(gift)
        return total, (min(floors) if floors else None), cheap
