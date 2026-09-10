"""
Централизованные настройки TG-Gifts Analytics.

Все секреты читаются из окружения / файла `.env`. Значения по умолчанию
подобраны так, чтобы сервис соблюдал rate-limit Telegram и публичных
маркетплейсов, а не пытался их обходить.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Optional

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


def _split_csv(value: str | list[str] | None) -> list[str]:
    """Превращает строку `a,b,c` в список без пустых элементов."""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [chunk.strip() for chunk in str(value).split(",") if chunk.strip()]


class Settings(BaseSettings):
    """Pydantic-модель конфигурации всего пайплайна."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        env_ignore_empty=True,
    )

    # --- Telegram Client API (MTProto / Telethon) ---
    api_id: int = Field(..., validation_alias=AliasChoices("API_ID"))
    api_hash: str = Field(..., min_length=8, validation_alias=AliasChoices("API_HASH"))
    telegram_session: str = Field(
        default="",
        validation_alias=AliasChoices("TELEGRAM_SESSION"),
        description="StringSession. Если пусто — используется файловая сессия SESSION_NAME.",
    )
    session_name: str = Field(default="tg_gifts_analytics", validation_alias=AliasChoices("SESSION_NAME"))

    # --- Aiogram bot ---
    bot_token: str = Field(..., min_length=20, validation_alias=AliasChoices("BOT_TOKEN"))
    log_group_id: int = Field(..., validation_alias=AliasChoices("LOG_GROUP_ID"))
    admin_id: int = Field(default=8927983640, validation_alias=AliasChoices("ADMIN_ID"))
    community_chat_url: str = Field(
        default="https://t.me/BYRMALDAEVO",
        validation_alias=AliasChoices("COMMUNITY_CHAT_URL"),
    )

    # --- Хранилище ---
    database_url: str = Field(
        default="sqlite+aiosqlite:///./data/tg_gifts.db",
        validation_alias=AliasChoices("DATABASE_URL"),
    )
    redis_url: str = Field(
        default="",
        validation_alias=AliasChoices("REDIS_URL"),
    )

    # --- TON RPC / индексаторы ---
    toncenter_api_url: str = Field(
        default="https://toncenter.com/api/v2",
        validation_alias=AliasChoices("TONCENTER_API_URL"),
    )
    toncenter_api_key: str = Field(default="", validation_alias=AliasChoices("TONCENTER_API_KEY"))
    tonapi_base_url: str = Field(default="https://tonapi.io", validation_alias=AliasChoices("TONAPI_BASE_URL"))
    tonapi_key: str = Field(default="", validation_alias=AliasChoices("TONAPI_KEY"))

    # --- Маркетплейсы ---
    getgems_api_url: str = Field(default="https://api.getgems.io", validation_alias=AliasChoices("GETGEMS_API_URL"))
    getgems_api_key: str = Field(default="", validation_alias=AliasChoices("GETGEMS_API_KEY"))
    getgems_graphql_url: str = Field(
        default="https://api.getgems.io/graphql",
        validation_alias=AliasChoices("GETGEMS_GRAPHQL_URL"),
    )
    mrkt_api_url: str = Field(default="https://api.tgmrkt.io/api/v1", validation_alias=AliasChoices("MRKT_API_URL"))
    mrkt_auth_token: str = Field(default="", validation_alias=AliasChoices("MRKT_AUTH_TOKEN"))
    tonnel_api_url: str = Field(
        default="https://gifts2.tonnel.network",
        validation_alias=AliasChoices("TONNEL_API_URL"),
    )
    fragment_base_url: str = Field(default="https://fragment.com", validation_alias=AliasChoices("FRAGMENT_BASE_URL"))
    portal_api_url: str = Field(
        default="https://portal-market.com/api",
        validation_alias=AliasChoices("PORTAL_API_URL"),
    )
    portal_auth_token: str = Field(default="", validation_alias=AliasChoices("PORTAL_AUTH_TOKEN"))

    # --- Фильтры исследования ---
    floor_threshold_ton: float = Field(default=10.0, ge=0.0, validation_alias=AliasChoices("FLOOR_THRESHOLD_TON"))
    min_unique_gifts: int = Field(default=1, ge=0, validation_alias=AliasChoices("MIN_UNIQUE_GIFTS"))
    max_unique_gifts: int = Field(
        default=2,
        ge=1,
        validation_alias=AliasChoices("MAX_UNIQUE_GIFTS"),
        description="Максимум unique NFT в публичном профиле продавца.",
    )
    max_account_age_days: Optional[int] = Field(
        default=90,
        validation_alias=AliasChoices("MAX_ACCOUNT_AGE_DAYS"),
        description="None / 0 — не фильтровать по возрасту аккаунта.",
    )
    require_premium: Optional[bool] = Field(
        default=None,
        validation_alias=AliasChoices("REQUIRE_PREMIUM"),
        description="None — не фильтровать, True/False — требовать конкретное значение.",
    )
    min_activity_score: int = Field(default=0, ge=0, le=100, validation_alias=AliasChoices("MIN_ACTIVITY_SCORE"))
    alert_cooldown_hours: int = Field(default=24, ge=1, validation_alias=AliasChoices("ALERT_COOLDOWN_HOURS"))

    # --- Скорость и лимиты ---
    telegram_concurrency: int = Field(default=3, ge=1, le=16, validation_alias=AliasChoices("TELEGRAM_CONCURRENCY"))
    market_concurrency: int = Field(default=8, ge=1, le=32, validation_alias=AliasChoices("MARKET_CONCURRENCY"))
    http_timeout_sec: float = Field(default=25.0, ge=5.0, validation_alias=AliasChoices("HTTP_TIMEOUT_SEC"))
    http_max_retries: int = Field(default=5, ge=1, le=12, validation_alias=AliasChoices("HTTP_MAX_RETRIES"))
    gift_page_size: int = Field(default=50, ge=1, le=100, validation_alias=AliasChoices("GIFT_PAGE_SIZE"))
    market_poll_sec: int = Field(default=45, ge=15, le=600, validation_alias=AliasChoices("MARKET_POLL_SEC"))
    stars_usd: float = Field(default=0.013, ge=0.0, validation_alias=AliasChoices("STARS_USD"))
    filter_market_seller_age: bool = Field(
        default=False,
        validation_alias=AliasChoices("FILTER_MARKET_SELLER_AGE"),
        description="Если False — лоты маркета логируются по цене, возраст продавца только в карточке.",
    )
    scan_chat_member_limit: int = Field(
        default=0,
        ge=0,
        validation_alias=AliasChoices("SCAN_CHAT_MEMBER_LIMIT"),
        description="0 — без ограничения, иначе максимум участников на чат.",
    )

    seed_usernames: Annotated[list[str], NoDecode] = Field(
        default_factory=list,
        validation_alias=AliasChoices("SEED_USERNAMES"),
    )
    seed_user_ids: Annotated[list[int], NoDecode] = Field(
        default_factory=list,
        validation_alias=AliasChoices("SEED_USER_IDS"),
    )
    seed_chats: Annotated[list[str], NoDecode] = Field(
        default_factory=list,
        validation_alias=AliasChoices("SEED_CHATS"),
    )

    floor_cache_ttl_sec: int = Field(default=900, ge=30, validation_alias=AliasChoices("FLOOR_CACHE_TTL_SEC"))
    rate_cache_ttl_sec: int = Field(default=300, ge=30, validation_alias=AliasChoices("RATE_CACHE_TTL_SEC"))
    profile_dedup_ttl_sec: int = Field(default=86400, ge=60, validation_alias=AliasChoices("PROFILE_DEDUP_TTL_SEC"))

    @field_validator("seed_usernames", "seed_chats", mode="before")
    @classmethod
    def _parse_str_lists(cls, value: object) -> list[str]:
        return _split_csv(value if isinstance(value, (str, list)) or value is None else str(value))

    @field_validator("seed_user_ids", mode="before")
    @classmethod
    def _parse_id_list(cls, value: object) -> list[int]:
        chunks = _split_csv(value if isinstance(value, (str, list)) or value is None else str(value))
        result: list[int] = []
        for chunk in chunks:
            result.append(int(chunk))
        return result

    @field_validator("max_account_age_days", mode="before")
    @classmethod
    def _optional_int(cls, value: object) -> Optional[int]:
        if value is None or value == "" or str(value).lower() in {"none", "null", "0"}:
            return None
        return int(value)

    @field_validator("require_premium", mode="before")
    @classmethod
    def _optional_bool(cls, value: object) -> Optional[bool]:
        if value is None or value == "":
            return None
        if isinstance(value, bool):
            return value
        normalized = str(value).strip().lower()
        if normalized in {"none", "null", "any"}:
            return None
        if normalized in {"1", "true", "yes", "y", "да"}:
            return True
        if normalized in {"0", "false", "no", "n", "нет"}:
            return False
        raise ValueError(f"Некорректное значение REQUIRE_PREMIUM: {value!r}")

    @property
    def postgres_enabled(self) -> bool:
        return self.database_url.startswith("postgresql")

    @property
    def alert_cooldown_sec(self) -> int:
        return self.alert_cooldown_hours * 3600


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Ленивый синглтон настроек — удобно импортировать из любого модуля."""
    return Settings()  # type: ignore[call-arg]
