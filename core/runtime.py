"""
Живые фильтры: меняются из админ-бота без перезапуска.
Хранятся в data/filters.json.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from config import Settings

LOGGER = logging.getLogger("tg_gifts.runtime")
DEFAULT_PATH = Path("data/filters.json")
SCHEMA_VERSION = 10
BUILD = "20260920-7"

# Мамонт = русский, ур.1, 1–2 NFT, 0–10 TON.
ORIGINAL_FILTERS: dict[str, Any] = {
    "floor_min_ton": 0.0,
    "floor_max_ton": 10.0,
    "min_unique_gifts": 1,
    "max_unique_gifts": 2,
    "stars_rating_min": 1,
    "stars_rating_max": 1,
    "require_stars_rating": True,
    "max_account_age_days": None,
    "min_activity_score": 0,
    "max_activity_score": 55,
    "max_regular_gifts": 0,
    "require_premium": None,
    "require_noob_profile": True,
    "require_russian": True,
    "filter_seller_age": False,
    "alert_cooldown_hours": 24,
    "market_poll_sec": 15,
}


@dataclass
class LiveFilters:
    scanner_enabled: bool = True
    floor_min_ton: float = 0.0
    floor_max_ton: float = 10.0
    min_unique_gifts: int = 1
    max_unique_gifts: int = 2
    stars_rating_min: int = 1
    stars_rating_max: int = 1
    require_stars_rating: bool = True
    max_account_age_days: Optional[int] = None
    require_premium: Optional[bool] = None
    min_activity_score: int = 0
    max_activity_score: int = 55
    max_regular_gifts: int = 0
    filter_seller_age: bool = False
    require_noob_profile: bool = True
    require_russian: bool = True
    alert_cooldown_hours: int = 24
    market_poll_sec: int = 15
    community_url: str = "https://t.me/GGsel_deal"
    schema_version: int = SCHEMA_VERSION
    path: Path = field(default_factory=lambda: DEFAULT_PATH, repr=False)

    @classmethod
    def from_settings(cls, settings: Settings, path: Path = DEFAULT_PATH) -> "LiveFilters":
        live = cls(
            scanner_enabled=True,
            floor_min_ton=0.0,
            floor_max_ton=float(settings.floor_threshold_ton),
            min_unique_gifts=int(settings.min_unique_gifts),
            max_unique_gifts=min(2, int(settings.max_unique_gifts)),
            stars_rating_min=1,
            stars_rating_max=1,
            require_stars_rating=True,
            max_account_age_days=None,
            require_premium=None,
            min_activity_score=int(settings.min_activity_score),
            max_activity_score=55,
            max_regular_gifts=0,
            require_noob_profile=True,
            require_russian=True,
            filter_seller_age=False,
            alert_cooldown_hours=int(settings.alert_cooldown_hours),
            market_poll_sec=int(settings.market_poll_sec),
            community_url=getattr(settings, "community_chat_url", None) or "https://t.me/GGsel_deal",
            schema_version=SCHEMA_VERSION,
            path=path,
        )
        if path.exists():
            live.load()
        if int(live.market_poll_sec or 0) >= 40:
            live.market_poll_sec = 15
        live.community_url = getattr(settings, "community_chat_url", None) or "https://t.me/GGsel_deal"
        live.save()
        return live

    @property
    def alert_cooldown_sec(self) -> int:
        return max(60, int(self.alert_cooldown_hours) * 3600)

    def to_public_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("path", None)
        return data

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.to_public_dict(), ensure_ascii=False, indent=2), encoding="utf-8")

    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            LOGGER.warning("Не прочитал %s: %s", self.path, exc)
            return
        if not isinstance(raw, dict):
            return
        for key, value in raw.items():
            if key == "path" or not hasattr(self, key):
                continue
            setattr(self, key, value)
        saved = int(raw.get("schema_version") or 0)
        if saved < SCHEMA_VERSION:
            for key, value in ORIGINAL_FILTERS.items():
                setattr(self, key, value)
            self.schema_version = SCHEMA_VERSION
            self.save()
            LOGGER.info("Фильтры: русский мамонт, рейтинг 1, ≤2 NFT")

    def update(self, **kwargs: Any) -> None:
        for key, value in kwargs.items():
            if not hasattr(self, key) or key == "path":
                raise KeyError(key)
            setattr(self, key, value)
        self.save()

    def summary_lines(self) -> list[str]:
        premium = {True: "только Premium", False: "без Premium", None: "любой"}[self.require_premium]
        age = "выкл" if not self.filter_seller_age or not self.max_account_age_days else f"≤ {self.max_account_age_days}д"
        return [
            f"Сборка: {BUILD}",
            f"Сканер: {'ON' if self.scanner_enabled else 'OFF'}",
            f"Цена лота: {self.floor_min_ton:g}–{self.floor_max_ton:g} TON",
            f"NFT в профиле: {self.min_unique_gifts}–{self.max_unique_gifts}",
            f"Stars-рейтинг: {self.stars_rating_min}–{self.stars_rating_max}"
            + (" (обязателен)" if self.require_stars_rating else ""),
            f"Возраст аккаунта: {age}",
            f"Premium: {premium}",
            f"Лох-профиль: {'да' if self.require_noob_profile else 'нет'}",
            f"Только русские: {'да' if self.require_russian else 'нет'}",
            f"Мин. активность: {self.min_activity_score}",
            f"Макс. живость профиля: {'выкл' if self.max_activity_score <= 0 else self.max_activity_score}",
            f"Обычных гифтов: {'любое' if self.max_regular_gifts <= 0 else f'≤ {self.max_regular_gifts}'}",
            f"Пауза между кругами: {self.market_poll_sec}с",
            f"Антидубль: {self.alert_cooldown_hours}ч",
        ]
