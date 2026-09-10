"""Трекер лотов: обрабатываем только новые выставления, не весь рынок."""

from __future__ import annotations

import json
import logging
from pathlib import Path

LOGGER = logging.getLogger("tg_gifts.listings")
DEFAULT_PATH = Path("data/seen_listings.json")


class ListingTracker:
    """
    Первый проход только запоминает текущие лоты (без алертов).
    Дальше — только slug, которого не было в прошлом проходе.
    Повторная выставка того же NFT после снятия снова считается новой.
    """

    def __init__(self, path: Path = DEFAULT_PATH) -> None:
        self.path = path
        self._prev: set[str] = set()
        self._curr: set[str] = set()
        self._ready = False
        self._load()

    @property
    def warming_up(self) -> bool:
        return not self._ready

    def observe(self, key: str) -> bool:
        """True — лот новый, нужно разобрать. False — уже видели / прогрев."""
        if not key:
            return False
        self._curr.add(key)
        if not self._ready:
            return False
        if key in self._prev:
            return False
        self._prev.add(key)
        self._save()
        return True

    def commit_scan(self) -> None:
        self._prev = set(self._curr)
        self._curr = set()
        self._ready = True
        self._save()
        LOGGER.info("Трекер лотов: запомнил %s актуальных выставлений", len(self._prev))

    def discard_partial(self) -> None:
        self._curr = set()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            LOGGER.warning("Не прочитал %s: %s", self.path, exc)
            return
        keys = raw.get("keys") if isinstance(raw, dict) else None
        if isinstance(keys, list):
            self._prev = {str(item) for item in keys if item}
            self._ready = bool(self._prev) or bool(raw.get("ready"))
            LOGGER.info("Трекер лотов: %s известных выставлений", len(self._prev))

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"ready": True, "keys": sorted(self._prev)}
        self.path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
