"""Трекер уже разобранных лотов."""

from __future__ import annotations

import json
import logging
from pathlib import Path

LOGGER = logging.getLogger("tg_gifts.listings")
DEFAULT_PATH = Path("data/seen_listings.json")
VERSION = 3


class ListingTracker:
    """Помечаем лот только после реального разбора. Незавершённый круг не сжигает очередь."""

    def __init__(self, path: Path = DEFAULT_PATH) -> None:
        self.path = path
        self._done: set[str] = set()
        self._load()

    def should_process(self, key: str) -> bool:
        return bool(key) and key not in self._done

    def mark(self, key: str) -> None:
        if key:
            self._done.add(key)

    def commit_scan(self) -> None:
        if len(self._done) > 80_000:
            extra = len(self._done) - 50_000
            for item in list(self._done)[:extra]:
                self._done.discard(item)
        self._save()
        LOGGER.info("Трекер лотов: разобрано %s", len(self._done))

    def discard_partial(self) -> None:
        self._save()

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
        if isinstance(keys, list):
            self._done = {str(item) for item in keys if item}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": VERSION, "keys": sorted(self._done)}
        self.path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
