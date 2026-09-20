"""Трекер уже разобранных лотов."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Optional

LOGGER = logging.getLogger("tg_gifts.listings")
DEFAULT_PATH = Path("data/seen_listings.json")
VERSION = 10
TTL_SEC = 2 * 3600


class ListingTracker:
    """Помечаем лот после разбора. Через TTL снова открываем — очередь не умирает."""

    def __init__(self, path: Path = DEFAULT_PATH) -> None:
        self.path = path
        self._done: dict[str, float] = {}
        self._load()

    def should_process(self, key: str) -> bool:
        if not key:
            return False
        marked = self._done.get(key)
        if marked is None:
            return True
        if time.time() - marked >= TTL_SEC:
            self._done.pop(key, None)
            return True
        return False

    def mark(self, key: str) -> None:
        if key:
            self._done[key] = time.time()

    def commit_scan(self) -> None:
        self._purge()
        self._save()
        LOGGER.info("Трекер лотов: разобрано %s", len(self._done))

    def discard_partial(self) -> None:
        self._save()

    def _purge(self) -> None:
        now = time.time()
        dead = [key for key, marked in self._done.items() if now - marked >= TTL_SEC]
        for key in dead:
            self._done.pop(key, None)
        if len(self._done) > 80_000:
            oldest = sorted(self._done.items(), key=lambda item: item[1])
            extra = len(self._done) - 50_000
            for key, _marked in oldest[:extra]:
                self._done.pop(key, None)

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            LOGGER.warning("Не прочитал %s: %s", self.path, exc)
            return
        if not isinstance(raw, dict) or raw.get("version") != VERSION:
            LOGGER.info("Сбрасываю старый трекер — снова разбираю текущие лоты")
            return
        keys = raw.get("keys")
        now = time.time()
        if isinstance(keys, dict):
            self._done = {
                str(key): float(value)
                for key, value in keys.items()
                if key and now - float(value) < TTL_SEC
            }

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": VERSION, "keys": self._done}
        self.path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


SELLER_TTL_SEC = 7 * 24 * 3600
SELLER_PATH = Path("data/seen_sellers.json")


class SeenSellers:
    """Один человек — одна карточка. Повторно не шлём даже с другим лотом."""

    def __init__(self, path: Path = SELLER_PATH, ttl_sec: int = SELLER_TTL_SEC) -> None:
        self.path = path
        self.ttl_sec = ttl_sec
        self._done: dict[str, float] = {}
        self._load()

    def seen(self, user_id: Optional[int]) -> bool:
        if not user_id:
            return False
        key = str(int(user_id))
        marked = self._done.get(key)
        if marked is None:
            return False
        if time.time() - marked >= self.ttl_sec:
            self._done.pop(key, None)
            return False
        return True

    def mark(self, user_id: Optional[int]) -> None:
        if user_id:
            self._done[str(int(user_id))] = time.time()

    def release(self, user_id: Optional[int]) -> None:
        if user_id:
            self._done.pop(str(int(user_id)), None)

    def seed(self, user_ids: list[int] | set[int]) -> None:
        now = time.time()
        for uid in user_ids:
            if uid:
                self._done.setdefault(str(int(uid)), now)

    def commit(self) -> None:
        now = time.time()
        dead = [key for key, marked in self._done.items() if now - marked >= self.ttl_sec]
        for key in dead:
            self._done.pop(key, None)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"keys": self._done}
        self.path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        LOGGER.info("Уже слали людей: %s", len(self._done))

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            LOGGER.warning("Не прочитал %s: %s", self.path, exc)
            return
        keys = raw.get("keys") if isinstance(raw, dict) else None
        now = time.time()
        if isinstance(keys, dict):
            self._done = {
                str(key): float(value)
                for key, value in keys.items()
                if key and now - float(value) < self.ttl_sec
            }
