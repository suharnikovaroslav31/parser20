"""
Доменные модели TG-Gifts Analytics.

Dataclass'ы используются во всём пайплайне (сканер → фильтр → логгер).
ORM-модели — история профилей в SQLite или PostgreSQL.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import JSON, BigInteger, Boolean, DateTime, Float, Integer, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    """Базовый класс SQLAlchemy 2.0."""


class AnalyzedProfileRow(Base):
    """Снимок профиля, прошедшего (или не прошедшего) фильтры."""

    __tablename__ = "analyzed_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True, unique=True)
    username: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    display_name: Mapped[str] = mapped_column(String(256), default="")
    is_premium: Mapped[bool] = mapped_column(Boolean, default=False)
    unique_gift_count: Mapped[int] = mapped_column(Integer, default=0)
    total_gift_count: Mapped[int] = mapped_column(Integer, default=0)
    estimated_value_ton: Mapped[float] = mapped_column(Float, default=0.0)
    estimated_value_usd: Mapped[float] = mapped_column(Float, default=0.0)
    min_floor_ton: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    activity_score: Mapped[int] = mapped_column(Integer, default=0)
    approx_registered_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    matched: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    fingerprint: Mapped[str] = mapped_column(String(64), default="")
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )


class GiftItemRow(Base):
    """Отдельный коллекционный подарок, привязанный к профилю."""

    __tablename__ = "gift_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    slug: Mapped[str] = mapped_column(String(128), index=True)
    title: Mapped[str] = mapped_column(String(256), default="")
    number: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    gift_address: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    floor_ton: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    floor_source: Mapped[str] = mapped_column(String(32), default="")
    listed: Mapped[bool] = mapped_column(Boolean, default=False)
    raw: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ScanEventRow(Base):
    """Аудит прогона: сколько профилей обработано, сколько совпало."""

    __tablename__ = "scan_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(64))
    profiles_seen: Mapped[int] = mapped_column(Integer, default=0)
    profiles_matched: Mapped[int] = mapped_column(Integer, default=0)
    error_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


@dataclass(slots=True)
class GiftAttribute:
    trait: str
    value: str
    rarity_permille: Optional[int] = None


@dataclass(slots=True)
class UniqueGift:
    """Коллекционный (unique / NFT) подарок Telegram."""

    slug: str
    title: str
    number: Optional[int] = None
    gift_id: Optional[int] = None
    gift_address: Optional[str] = None
    owner_address: Optional[str] = None
    model: Optional[str] = None
    backdrop: Optional[str] = None
    symbol: Optional[str] = None
    attributes: list[GiftAttribute] = field(default_factory=list)
    availability_issued: Optional[int] = None
    availability_total: Optional[int] = None
    on_resale: bool = False
    telegram_floor_ton: Optional[float] = None
    fair_value_ton: Optional[float] = None
    market_floor_ton: Optional[float] = None
    market_source: str = ""
    listed_count: Optional[int] = None
    fragment_url: Optional[str] = None
    seller_id: Optional[int] = None
    seller_name: Optional[str] = None
    unsaved: bool = False

    @property
    def best_floor_ton(self) -> Optional[float]:
        candidates = [value for value in (self.market_floor_ton, self.telegram_floor_ton) if value is not None]
        return min(candidates) if candidates else None

    @property
    def nft_link(self) -> str:
        slug = re.sub(r"[^A-Za-z0-9_-]+", "", self.slug or "")
        return f"https://t.me/nft/{slug}" if slug else ""

    @property
    def getgems_link(self) -> str:
        addr = re.sub(r"[^A-Za-z0-9_-]+", "", self.gift_address or "")
        if addr:
            return f"https://getgems.io/nft/{addr}"
        return "https://getgems.io"


@dataclass(slots=True)
class RegularGift:
    """Обычный (не unique) Star Gift, отображаемый в профиле."""

    gift_id: int
    title: str
    stars: Optional[int] = None
    limited: bool = False
    sold_out: bool = False


@dataclass(slots=True)
class AccountMetrics:
    user_id: int
    username: Optional[str]
    first_name: str
    last_name: str
    is_premium: bool
    is_verified: bool
    has_photo: bool
    bio: str
    personal_channel_id: Optional[int]
    common_chats_count: int
    approx_registered_at: Optional[datetime]
    account_age_days: Optional[int]
    public_channel_count: int
    activity_score: int
    stars_rating_level: Optional[int] = None
    stars_rating_stars: Optional[int] = None
    stars_fetched: bool = False
    gifts_fetched: bool = False
    stargifts_count: Optional[int] = None
    lang_code: Optional[str] = None

    @property
    def display_name(self) -> str:
        full = f"{self.first_name} {self.last_name}".strip()
        return full or (f"@{self.username}" if self.username else str(self.user_id))

    @property
    def telegram_link(self) -> str:
        if self.username and re.fullmatch(r"[A-Za-z0-9_]{4,32}", self.username):
            return f"https://t.me/{self.username}"
        return f"tg://user?id={self.user_id}"


@dataclass(slots=True)
class ProfileSnapshot:
    """Полный снимок публичного профиля + оценка подарков."""

    metrics: AccountMetrics
    unique_gifts: list[UniqueGift]
    regular_gifts: list[RegularGift]
    estimated_value_ton: float
    estimated_value_usd: float
    min_floor_ton: Optional[float]
    cheap_gifts: list[UniqueGift]
    ton_usd: float
    processed_ms: float
    source: str
    fingerprint_key: str = ""
    listing_key: str = ""
    captured_at: datetime = field(default_factory=utcnow)

    @property
    def fingerprint(self) -> str:
        if self.fingerprint_key:
            return self.fingerprint_key
        slugs = ",".join(sorted(gift.slug for gift in self.unique_gifts))
        return f"{self.metrics.user_id}:{len(self.unique_gifts)}:{slugs}"


@dataclass(slots=True)
class FilterDecision:
    matched: bool
    reasons: list[str]
    snapshot: ProfileSnapshot
    cheapest_gift: Optional[UniqueGift]
