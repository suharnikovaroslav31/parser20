"""Трекер уже разобранных лотов."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

LOGGER = logging.getLogger("tg_gifts.listings")
DEFAULT_PATH = Path("data/seen_listings.json")
VERSION = 7
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
