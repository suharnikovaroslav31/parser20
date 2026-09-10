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
SCHEMA_VERSION = 2

# Более широкий профиль «лёгкого» продавца: дешёвый лот, мало NFT, низкий/нет рейтинга, молодой акк.
VOLUME_DEFAULTS: dict[str, Any] = {
    "floor_min_ton": 0.0,
    "floor_max_ton": 15.0,
    "min_unique_gifts": 1,
    "max_unique_gifts": 5,
    "stars_rating_min": 0,
    "stars_rating_max": 2,
    "require_stars_rating": False,
    "max_account_age_days": 365,
    "min_activity_score": 0,
    "filter_seller_age": True,
    "alert_cooldown_hours": 2,
    "market_poll_sec": 20,
}


@dataclass
class LiveFilters:
    scanner_enabled: bool = True
    floor_min_ton: float = 0.0
    floor_max_ton: float = 15.0
    min_unique_gifts: int = 1
    max_unique_gifts: int = 5
    stars_rating_min: int = 0
    stars_rating_max: int = 2
    require_stars_rating: bool = False
    max_account_age_days: Optional[int] = 365
    require_premium: Optional[bool] = None
    min_activity_score: int = 0
    filter_seller_age: bool = True
    alert_cooldown_hours: int = 2
    market_poll_sec: int = 20
    community_url: str = "https://t.me/BYRMALDAEVO"
    schema_version: int = SCHEMA_VERSION
    path: Path = field(default_factory=lambda: DEFAULT_PATH, repr=False)

    @classmethod
    def from_settings(cls, settings: Settings, path: Path = DEFAULT_PATH) -> "LiveFilters":
        live = cls(
            scanner_enabled=True,
            floor_min_ton=0.0,
            floor_max_ton=max(15.0, float(settings.floor_threshold_ton)),
            min_unique_gifts=max(1, int(settings.min_unique_gifts)),
            max_unique_gifts=max(5, int(settings.max_unique_gifts)),
            stars_rating_min=0,
            stars_rating_max=2,
            require_stars_rating=False,
            max_account_age_days=365,
            require_premium=settings.require_premium,
            min_activity_score=int(settings.min_activity_score),
            filter_seller_age=True,
            alert_cooldown_hours=2,
            market_poll_sec=min(20, int(settings.market_poll_sec)),
            community_url=getattr(settings, "community_chat_url", None) or "https://t.me/BYRMALDAEVO",
            schema_version=SCHEMA_VERSION,
            path=path,
        )
        if path.exists():
            live.load()
        else:
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
            for key, value in VOLUME_DEFAULTS.items():
                setattr(self, key, value)
            self.schema_version = SCHEMA_VERSION
            self.save()
            LOGGER.info("Фильтры расширены под больший поток лотов (schema %s)", SCHEMA_VERSION)

    def update(self, **kwargs: Any) -> None:
        for key, value in kwargs.items():
            if not hasattr(self, key) or key == "path":
                raise KeyError(key)
            setattr(self, key, value)
        self.save()

    def summary_lines(self) -> list[str]:
        premium = {True: "только Premium", False: "без Premium", None: "любой"}[self.require_premium]
        age = "выкл" if not self.max_account_age_days else f"≤ {self.max_account_age_days}д"
        rating_note = " (обязателен)" if self.require_stars_rating else " (нет рейтинга = ок)"
        return [
            f"Сканер: {'ON' if self.scanner_enabled else 'OFF'}",
            f"Цена лота: {self.floor_min_ton:g}–{self.floor_max_ton:g} TON",
            f"NFT в профиле: {self.min_unique_gifts}–{self.max_unique_gifts}",
            f"Stars-рейтинг: {self.stars_rating_min}–{self.stars_rating_max}{rating_note}",
            f"Возраст аккаунта: {age}",
            f"Premium: {premium}",
            f"Мин. активность: {self.min_activity_score}",
            f"Пауза между кругами: {self.market_poll_sec}с",
            f"Антидубль: {self.alert_cooldown_hours}ч",
        ]
