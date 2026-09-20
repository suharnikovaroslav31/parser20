"""
Хранилище: PostgreSQL или локальный SQLite + Redis.

Redis — дедуп и кэш floor-price. Если Redis не установлен,
используется in-memory TTL, сервис не падает.
SQLite включается через DATABASE_URL=sqlite+aiosqlite:///./data/tg_gifts.db
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any, Optional

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from core.models import AnalyzedProfileRow, Base, GiftItemRow, ProfileSnapshot, ScanEventRow, utcnow

LOGGER = logging.getLogger("tg_gifts.storage")


class MemoryTTLCache:
    """Запасной кэш, если Redis не поднят. Пишем на диск — дедуп живёт после рестарта."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or Path("data/alert_cache.json")
        self._data: dict[str, tuple[float, str]] = {}
        self._load()

    def _purge(self) -> None:
        now = time.time()
        dead = [key for key, (expires, _) in self._data.items() if expires <= now]
        for key in dead:
            self._data.pop(key, None)

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        now = time.time()
        items = raw.get("keys") if isinstance(raw, dict) else None
        if not isinstance(items, dict):
            return
        for key, item in items.items():
            if not isinstance(item, list) or len(item) != 2:
                continue
            expires, value = float(item[0]), str(item[1])
            if expires > now and key:
                self._data[str(key)] = (expires, value)

    def _save(self) -> None:
        self._purge()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"keys": {key: [expires, value] for key, (expires, value) in self._data.items()}}
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.path)
        except OSError as exc:
            LOGGER.warning("не записал %s: %s", self.path, exc)

    async def get(self, key: str) -> Optional[str]:
        self._purge()
        item = self._data.get(key)
        if item is None:
            return None
        expires, value = item
        if expires <= time.time():
            self._data.pop(key, None)
            return None
        return value

    async def set(self, key: str, value: str, ttl: int) -> None:
        self._data[key] = (time.time() + max(int(ttl), 1), value)
        self._save()

    async def exists(self, key: str) -> bool:
        return await self.get(key) is not None


class Storage:
    """Фасад: Postgres для истории + Redis/память для дедупа."""

    def __init__(self, database_url: str, redis_url: str) -> None:
        self._database_url = database_url
        self._redis_url = redis_url
        engine_kwargs: dict[str, Any] = {"pool_pre_ping": True, "echo": False}
        if database_url.startswith("sqlite"):
            sqlite_path = database_url.split("///", 1)[-1]
            if sqlite_path and not sqlite_path.startswith(":memory:"):
                Path(sqlite_path).parent.mkdir(parents=True, exist_ok=True)
            engine_kwargs["connect_args"] = {"check_same_thread": False}
        else:
            engine_kwargs["pool_size"] = 5
            engine_kwargs["max_overflow"] = 10
        self._engine = create_async_engine(database_url, **engine_kwargs)
        self._session_factory = async_sessionmaker(self._engine, expire_on_commit=False, class_=AsyncSession)
        self._redis: Redis | None = None
        self._memory = MemoryTTLCache()
        self._redis_ok = False

    async def start(self) -> None:
        try:
            await asyncio.wait_for(self._create_schema(), timeout=6)
        except Exception as exc:
            LOGGER.warning("БД не открылась (%s) — работаем без истории", exc)
        url = (self._redis_url or "").strip()
        if not url:
            LOGGER.info("Redis не задан — in-memory кэш")
            return
        try:
            self._redis = Redis.from_url(
                url,
                encoding="utf-8",
                decode_responses=True,
                socket_connect_timeout=1,
                socket_timeout=1,
            )
            await asyncio.wait_for(self._redis.ping(), timeout=2)
            self._redis_ok = True
            LOGGER.info("Redis подключён")
        except Exception as exc:
            self._redis_ok = False
            LOGGER.warning("Redis недоступен (%s) — in-memory кэш", exc)

    async def _create_schema(self) -> None:
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def close(self) -> None:
        if self._redis is not None:
            await self._redis.aclose()
        await self._engine.dispose()

    async def cache_get(self, key: str) -> Optional[str]:
        if self._redis_ok and self._redis is not None:
            try:
                return await self._redis.get(key)
            except Exception as exc:
                LOGGER.warning("Redis GET %s упал: %s", key, exc)
                self._redis_ok = False
        return await self._memory.get(key)

    async def cache_set(self, key: str, value: str, ttl: int) -> None:
        if self._redis_ok and self._redis is not None:
            try:
                await self._redis.set(key, value, ex=ttl)
                return
            except Exception as exc:
                LOGGER.warning("Redis SET %s упал: %s", key, exc)
                self._redis_ok = False
        await self._memory.set(key, value, ttl)

    async def cache_json_get(self, key: str) -> Optional[Any]:
        raw = await self.cache_get(key)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    async def cache_json_set(self, key: str, value: Any, ttl: int) -> None:
        await self.cache_set(key, json.dumps(value, ensure_ascii=False, default=str), ttl)

    async def already_alerted(self, user_id: int, fingerprint: str, ttl: int) -> bool:
        """True, если такой же набор подарков уже отправляли в лог-группу."""
        key = f"tg:alert:{user_id}:{fingerprint}"
        if await self.cache_get(key):
            return True
        short = f"tg:alert:{user_id}"
        stored = await self.cache_get(short)
        return stored == fingerprint

    async def mark_alerted(self, user_id: int, fingerprint: str, ttl: int) -> None:
        await self.cache_set(f"tg:alert:{user_id}:{fingerprint}", "1", ttl)
        await self.cache_set(f"tg:alert:{user_id}", fingerprint, ttl)

    async def save_snapshot(self, snapshot: ProfileSnapshot, matched: bool) -> None:
        metrics = snapshot.metrics
        payload = {
            "source": snapshot.source,
            "cheap_slugs": [gift.slug for gift in snapshot.cheap_gifts],
            "min_floor_ton": snapshot.min_floor_ton,
            "activity_score": metrics.activity_score,
        }
        async with self._session_factory() as session:
            row = await session.scalar(
                select(AnalyzedProfileRow).where(AnalyzedProfileRow.user_id == metrics.user_id)
            )
            if row is None:
                row = AnalyzedProfileRow(user_id=metrics.user_id)
                session.add(row)
            row.username = metrics.username
            row.display_name = metrics.display_name
            row.is_premium = metrics.is_premium
            row.unique_gift_count = len(snapshot.unique_gifts)
            row.total_gift_count = len(snapshot.unique_gifts) + len(snapshot.regular_gifts)
            row.estimated_value_ton = snapshot.estimated_value_ton
            row.estimated_value_usd = snapshot.estimated_value_usd
            row.min_floor_ton = snapshot.min_floor_ton
            row.activity_score = metrics.activity_score
            row.approx_registered_at = metrics.approx_registered_at
            row.matched = matched
            row.fingerprint = snapshot.fingerprint
            row.payload = payload
            row.updated_at = utcnow()

            for gift in snapshot.unique_gifts:
                session.add(
                    GiftItemRow(
                        user_id=metrics.user_id,
                        slug=gift.slug,
                        title=gift.title,
                        number=gift.number,
                        gift_address=gift.gift_address,
                        floor_ton=gift.best_floor_ton,
                        floor_source=gift.market_source or "telegram",
                        listed=gift.on_resale,
                        raw={
                            "model": gift.model,
                            "backdrop": gift.backdrop,
                            "symbol": gift.symbol,
                            "fragment_url": gift.fragment_url,
                        },
                    )
                )
            await session.commit()

    async def log_scan_event(
        self,
        source: str,
        *,
        seen: int,
        matched: int,
        error: str | None = None,
    ) -> None:
        async with self._session_factory() as session:
            session.add(
                ScanEventRow(
                    source=source,
                    profiles_seen=seen,
                    profiles_matched=matched,
                    error_text=error,
                    finished_at=utcnow(),
                )
            )
            await session.commit()
