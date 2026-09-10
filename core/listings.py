"""Трекер уже разобранных лотов — без «прогрева», который глушил поиск."""

from __future__ import annotations

import json
import logging
from pathlib import Path

LOGGER = logging.getLogger("tg_gifts.listings")
DEFAULT_PATH = Path("data/seen_listings.json")
VERSION = 2


class ListingTracker:
    """
    Пропускаем только лоты, которые уже разбирали.
    Первый проход тоже разбирает — иначе бот «молчит».
    """

    def __init__(self, path: Path = DEFAULT_PATH) -> None:
        self.path = path
        self._prev: set[str] = set()
        self._curr: set[str] = set()
        self._load()

    @property
    def warming_up(self) -> bool:
        return False

    def observe(self, key: str) -> bool:
        """True — этот лот ещё не разбирали, нужно снять карточку."""
        if not key:
            return False
        if key in self._curr or key in self._prev:
            return False
        self._curr.add(key)
        return True

    def commit_scan(self) -> None:
        self._prev |= self._curr
        if len(self._prev) > 80_000:
            extra = len(self._prev) - 60_000
            for item in list(self._prev)[:extra]:
                self._prev.discard(item)
        self._curr = set()
        self._save()
        LOGGER.info("Трекер лотов: разобрано всего %s", len(self._prev))

    def discard_partial(self) -> None:
        self._prev |= self._curr
        self._curr = set()
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
            LOGGER.info("Старый трекер прогрева сброшен — начинаю реальный разбор лотов")
            return
        keys = raw.get("keys")
        if isinstance(keys, list):
            self._prev = {str(item) for item in keys if item}
            LOGGER.info("Трекер лотов: %s уже разобранных", len(self._prev))

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": VERSION, "keys": sorted(self._prev)}
        self.path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
