"""
Фильтры: мамонт = не шарит за NFT.
Режем цену у флора, скрытую витрину и маркет-пустышки.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

from core.models import AccountMetrics, FilterDecision, ProfileSnapshot, UniqueGift
from core.runtime import LiveFilters

LOGGER = logging.getLogger("tg_gifts.filters")

_MARKET_SOURCES = {
    "tg_market",
    "telegram_resale",
    "mrkt",
    "tonnel",
    "portal",
    "getgems",
}
_COMMON_LATIN = {
    "alex", "max", "mike", "anna", "ivan", "john", "david", "daniel", "maria",
    "olga", "nick", "kevin", "chris", "james", "robert", "michael", "sarah",
    "kate", "lisa", "tom", "tim", "adam", "mark", "paul", "peter", "jack",
    "leo", "artem", "kirill", "andrey", "sergey", "dmitry", "alexander",
}
_CYRILLIC = re.compile(r"[А-Яа-яЁёІіЇїЄєҐґЎў]")
_FOREIGN_SCRIPT = re.compile(
    r"[\u4e00-\u9fff\u3400-\u4dbf\u3040-\u30ff\uac00-\ud7af\u0600-\u06ff\u0e00-\u0e7f\u0900-\u097f]"
)
_FOREIGN_LANG = {
    "en", "zh", "ar", "tr", "es", "pt", "de", "fr", "id", "hi", "th", "vi",
    "ko", "ja", "fa", "it", "pl", "nl", "ro", "ms", "fil", "he", "el", "sv",
    "cs", "hu", "fi", "no", "da", "sk", "az", "uz",
}
_TRADER_NICK = re.compile(
    r"(nft|нфт|gifts?|гифт|resale|ресейл|tonnel|portals?|mrkt|fragment|floor|flip|snipe|getgems|collect)",
    re.IGNORECASE,
)
_RESELLER_BIO = re.compile(
    r"("
    r"\bnft\b|нфт|"
    r"resale|ресейл|"
    r"tonnel|portals?|getgems|\bmrkt\b|fragment|"
    r"скупк[ауи]|купл[юи]\s*(нфт|nft|гифт)|продам\s*(нфт|nft|гифт)|"
    r"floor\s*price|маркетплейс|gifts?\s*shop|"
    r"unique\s*gifts?|star\s*gifts?|"
    r"t\.me/nft"
    r")",
    re.IGNORECASE,
)

_ID_ANCHORS: tuple[tuple[int, datetime], ...] = (
    (1, datetime(2013, 8, 14, tzinfo=timezone.utc)),
    (776_000, datetime(2013, 10, 1, tzinfo=timezone.utc)),
    (2_768_409, datetime(2013, 11, 1, tzinfo=timezone.utc)),
    (7_679_610, datetime(2014, 1, 1, tzinfo=timezone.utc)),
    (11_538_514, datetime(2014, 1, 31, tzinfo=timezone.utc)),
    (15_835_244, datetime(2014, 2, 21, tzinfo=timezone.utc)),
    (23_646_077, datetime(2014, 3, 31, tzinfo=timezone.utc)),
    (38_015_510, datetime(2014, 5, 15, tzinfo=timezone.utc)),
    (44_634_671, datetime(2014, 6, 9, tzinfo=timezone.utc)),
    (54_831_853, datetime(2014, 7, 16, tzinfo=timezone.utc)),
    (72_716_330, datetime(2014, 10, 2, tzinfo=timezone.utc)),
    (92_516_381, datetime(2014, 12, 31, tzinfo=timezone.utc)),
    (103_151_531, datetime(2015, 6, 4, tzinfo=timezone.utc)),
    (122_137_132, datetime(2016, 1, 1, tzinfo=timezone.utc)),
    (178_220_079, datetime(2016, 7, 1, tzinfo=timezone.utc)),
    (234_208_886, datetime(2017, 1, 1, tzinfo=timezone.utc)),
    (400_000_000, datetime(2017, 12, 1, tzinfo=timezone.utc)),
    (600_000_000, datetime(2018, 7, 1, tzinfo=timezone.utc)),
    (800_000_000, datetime(2019, 3, 1, tzinfo=timezone.utc)),
    (1_000_000_000, datetime(2019, 10, 1, tzinfo=timezone.utc)),
    (1_200_000_000, datetime(2020, 4, 1, tzinfo=timezone.utc)),
    (1_500_000_000, datetime(2021, 1, 1, tzinfo=timezone.utc)),
    (1_800_000_000, datetime(2021, 7, 1, tzinfo=timezone.utc)),
    (2_100_000_000, datetime(2022, 1, 1, tzinfo=timezone.utc)),
    (2_500_000_000, datetime(2022, 8, 1, tzinfo=timezone.utc)),
    (3_000_000_000, datetime(2023, 1, 1, tzinfo=timezone.utc)),
    (4_000_000_000, datetime(2023, 6, 1, tzinfo=timezone.utc)),
    (5_000_000_000, datetime(2023, 10, 1, tzinfo=timezone.utc)),
    (5_300_000_000, datetime(2024, 1, 1, tzinfo=timezone.utc)),
    (6_000_000_000, datetime(2024, 4, 1, tzinfo=timezone.utc)),
    (7_000_000_000, datetime(2024, 8, 1, tzinfo=timezone.utc)),
    (7_500_000_000, datetime(2024, 12, 1, tzinfo=timezone.utc)),
    (8_000_000_000, datetime(2025, 3, 1, tzinfo=timezone.utc)),
    (8_500_000_000, datetime(2025, 8, 1, tzinfo=timezone.utc)),
    (9_000_000_000, datetime(2026, 1, 1, tzinfo=timezone.utc)),
    (9_500_000_000, datetime(2026, 6, 1, tzinfo=timezone.utc)),
)


def estimate_registered_at(user_id: int) -> Optional[datetime]:
    if user_id <= 0:
        return None
    anchors = _ID_ANCHORS
    now = datetime.now(timezone.utc)
    if user_id <= anchors[0][0]:
        return anchors[0][1]
    if user_id >= anchors[-1][0]:
        return min(now, anchors[-1][1])
    left_id, left_dt = anchors[0]
    for right_id, right_dt in anchors[1:]:
        if left_id <= user_id <= right_id:
            span_ids = max(1, right_id - left_id)
            ratio = (user_id - left_id) / span_ids
            estimated = left_dt + (right_dt - left_dt) * ratio
            return estimated if estimated <= now else now
        left_id, left_dt = right_id, right_dt
    return None


def account_age_days(user_id: int) -> tuple[Optional[datetime], Optional[int]]:
    registered = estimate_registered_at(user_id)
    if registered is None:
        return None, None
    return registered, max(0, (datetime.now(timezone.utc) - registered).days)


def compute_activity_score(
    *,
    username: Optional[str],
    is_premium: bool,
    is_verified: bool,
    has_photo: bool,
    bio: str,
    personal_channel_id: Optional[int],
    common_chats_count: int,
    unique_gift_count: int,
    public_channel_count: int,
) -> int:
    score = 0
    if username:
        score += 20
    if is_premium:
        score += 20
    if is_verified:
        score += 15
    if has_photo:
        score += 10
    if bio.strip():
        score += 10
    if personal_channel_id:
        score += 10
    score += min(10, public_channel_count * 5)
    score += min(10, max(0, common_chats_count) * 2)
    score += min(15, unique_gift_count * 3)
    return max(0, min(100, score))


def profile_richness(metrics: AccountMetrics) -> int:
    """Насколько профиль «живой» без учёта NFT — у лоха должен быть низкий."""
    return compute_activity_score(
        username=metrics.username,
        is_premium=metrics.is_premium,
        is_verified=metrics.is_verified,
        has_photo=metrics.has_photo,
        bio=metrics.bio,
        personal_channel_id=metrics.personal_channel_id,
        common_chats_count=metrics.common_chats_count,
        unique_gift_count=0,
        public_channel_count=metrics.public_channel_count,
    )


FLOOR_HUG_RATIO = 0.55
MARKET_FLOOR_BAND = 0.08


def listing_hugs_floor(ask: Optional[float], fair_value: Optional[float], *, ratio: float = FLOOR_HUG_RATIO) -> bool:
    """Цена ≥ 55% последней продажи — знает рынок, не лох."""
    if ask is None or fair_value is None or fair_value <= 0 or ask <= 0:
        return False
    return ask >= fair_value * ratio


def listing_at_market_floor(ask: Optional[float], market_floor: Optional[float], *, band: float = MARKET_FLOOR_BAND) -> bool:
    """Листинг у текущего флора Telegram — это перекуп, не лох."""
    if ask is None or market_floor is None or market_floor <= 0 or ask <= 0:
        return False
    return abs(ask - market_floor) / market_floor <= band


def name_has_cyrillic(first_name: str = "", last_name: str = "", bio: str = "") -> bool:
    return bool(_CYRILLIC.search(f"{first_name or ''} {last_name or ''} {bio or ''}"))


def has_cyrillic_name(metrics: AccountMetrics) -> bool:
    return name_has_cyrillic(metrics.first_name, metrics.last_name, metrics.bio)


def looks_like_reseller_bio(bio: str) -> bool:
    return bool(_RESELLER_BIO.search(bio or ""))


def looks_like_trader_username(username: Optional[str]) -> bool:
    return bool(_TRADER_NICK.search(username or ""))


def looks_russian(metrics: AccountMetrics) -> bool:
    """Режем явных иностранцев. lang_code у чужих часто пустой — из-за этого лохи пропадали."""
    lang = (metrics.lang_code or "").strip().lower().replace("_", "-")
    base = lang.split("-", 1)[0]
    if base in {"ru", "uk", "be", "kk"}:
        return True
    text = f"{metrics.first_name or ''} {metrics.last_name or ''} {metrics.bio or ''}"
    if _CYRILLIC.search(text):
        return True
    if base in _FOREIGN_LANG:
        return False
    if _FOREIGN_SCRIPT.search(text):
        return False
    return True


def looks_like_shell_profile(metrics: AccountMetrics) -> bool:
    return (not metrics.username) and (not (metrics.bio or "").strip()) and (not metrics.has_photo)


def looks_like_burner_name(first_name: str, last_name: str = "") -> bool:
    """Два рандомных латинских слова без кириллицы — типичный купленный альт."""
    joined = f"{first_name or ''} {last_name or ''}".strip()
    if not joined or re.search(r"[А-Яа-яЁё]", joined):
        return False
    tokens = re.findall(r"[A-Za-z]+", joined)
    if any(token.lower() in _COMMON_LATIN for token in tokens):
        return False
    letters = [ch.lower() for ch in joined if ch.isalpha()]
    if len(letters) < 10:
        return False
    vowels = sum(1 for ch in letters if ch in "aeiouy")
    if vowels / len(letters) <= 0.32:
        return True
    if len(tokens) >= 2 and all(len(token) >= 6 for token in tokens[:2]):
        return bool(re.search(r"[bcdfghjklmnpqrstvwxz]{4}", joined, re.IGNORECASE))
    return False


class ProfileFilter:
    def __init__(self, live: LiveFilters) -> None:
        self.live = live
        self.skip_counts: dict[str, int] = defaultdict(int)
        self.checked = 0
        self.matched = 0

    def reset_stats(self) -> None:
        self.skip_counts = defaultdict(int)
        self.checked = 0
        self.matched = 0

    def dump_stats(self) -> str:
        parts = [f"проверено {self.checked}", f"MATCH {self.matched}"]
        for key, value in sorted(self.skip_counts.items(), key=lambda item: -item[1]):
            parts.append(f"{key}={value}")
        return ", ".join(parts)

    def evaluate(self, snapshot: ProfileSnapshot) -> FilterDecision:
        live = self.live
        self.checked += 1
        reasons: list[str] = []
        metrics = snapshot.metrics
        people = snapshot.source not in _MARKET_SOURCES
        visible = [gift for gift in snapshot.unique_gifts if not gift.unsaved]
        if people and not visible:
            visible = list(snapshot.unique_gifts)
        unique_count = len(visible)
        cheapest = self._cheapest(visible) or self._cheapest(snapshot.unique_gifts)
        price = snapshot.min_floor_ton

        if not metrics.stars_fetched:
            reasons.append("профиль не открыт — рейтинг не прочитан")
        if not metrics.gifts_fetched:
            reasons.append("NFT профиля не прочитаны")
        if metrics.is_verified:
            reasons.append("verified — не новичок")
        if live.require_russian and not looks_russian(metrics):
            reasons.append("не русский профиль")

        if unique_count < live.min_unique_gifts:
            reasons.append(f"NFT в профиле {unique_count} < {live.min_unique_gifts}")
        if unique_count > live.max_unique_gifts:
            reasons.append(f"NFT в профиле {unique_count} > {live.max_unique_gifts}")

        if price is None:
            reasons.append("нет цены лота")
        else:
            if price < live.floor_min_ton:
                reasons.append(f"лот {price:g} < мин {live.floor_min_ton:g} TON")
            if price >= live.floor_max_ton:
                reasons.append(f"лот {price:g} ≥ макс {live.floor_max_ton:g} TON")

        level = metrics.stars_rating_level
        if live.require_stars_rating and level is None:
            reasons.append("Stars-рейтинг скрыт / не прочитан")
        elif level is not None:
            if level < live.stars_rating_min or level > live.stars_rating_max:
                reasons.append(
                    f"рейтинг ур.{level} вне {live.stars_rating_min}–{live.stars_rating_max}"
                )

        if live.filter_seller_age and live.max_account_age_days:
            if metrics.account_age_days is None:
                reasons.append("возраст аккаунта неизвестен")
            elif metrics.account_age_days > live.max_account_age_days:
                reasons.append(f"возраст {metrics.account_age_days}д > {live.max_account_age_days}д")

        if live.require_premium is not None and metrics.is_premium != live.require_premium:
            wanted = "Premium" if live.require_premium else "без Premium"
            reasons.append(f"Premium: нужно {wanted}")

        if metrics.activity_score < live.min_activity_score:
            reasons.append(f"активность {metrics.activity_score} < {live.min_activity_score}")

        if live.require_noob_profile:
            reasons.extend(self._noob_reasons(snapshot, live))

        matched = not reasons
        if matched:
            self.matched += 1
            richness = profile_richness(metrics)
            reasons.append(
                f"лох: рейтинг ур.{level}, {unique_count} NFT, лот {price:g} TON, "
                f"профиль {richness}/100, акк ~{metrics.account_age_days}д"
            )
            LOGGER.info("MATCH user=%s %s", metrics.user_id, reasons[-1])
        else:
            bucket = reasons[0].split(":")[0][:40] if reasons else "skip"
            self.skip_counts[bucket] += 1
            n = self.skip_counts[bucket]
            if n <= 8 or n % 20 == 0 or self.checked % 15 == 0:
                LOGGER.info("SKIP user=%s %s", metrics.user_id, "; ".join(reasons))
            if self.checked % 20 == 0:
                LOGGER.info("воронка: %s", self.dump_stats())

        return FilterDecision(
            matched=matched,
            reasons=reasons,
            snapshot=snapshot,
            cheapest_gift=cheapest,
        )

    def _noob_reasons(self, snapshot: ProfileSnapshot, live: LiveFilters) -> list[str]:
        """Отсекает тех, кто шарит, даже если на витрине 1 дешёвый NFT."""
        metrics = snapshot.metrics
        found: list[str] = []
        if metrics.personal_channel_id:
            found.append("личный канал — витрина")
        if looks_like_trader_username(metrics.username):
            found.append("юзернейм как у перекупа")
        if looks_like_reseller_bio(metrics.bio):
            found.append("в био признаки перекупа")
        hidden_nfts = [gift for gift in snapshot.unique_gifts if gift.unsaved]
        if hidden_nfts and snapshot.source in _MARKET_SOURCES:
            found.append(f"скрытые NFT: {len(hidden_nfts)}")
        shown = len(snapshot.unique_gifts) + len(snapshot.regular_gifts)
        total_gifts = metrics.stargifts_count
        if total_gifts is not None and total_gifts >= 8 and total_gifts > shown + 3:
            found.append(f"гифтов {total_gifts} при витрине {shown} — прячут коллекцию")
        if snapshot.source in _MARKET_SOURCES:
            if looks_like_shell_profile(metrics):
                found.append("пустой акк на маркете — альт перекупа")
            if not has_cyrillic_name(metrics):
                found.append("на маркете без кириллицы — не мамонт")
            if looks_like_burner_name(metrics.first_name, metrics.last_name):
                found.append("рандомное имя на маркете — альт перекупа")
            visible_nfts = [gift for gift in snapshot.unique_gifts if not gift.unsaved]
            if len(visible_nfts) > 1:
                found.append("больше одного NFT на маркете — уже шарит")
            ask = _listing_ask(snapshot)
            floors = [gift.telegram_floor_ton for gift in (snapshot.cheap_gifts or snapshot.unique_gifts) if gift.telegram_floor_ton]
            market_floor = min(floors) if floors else None
            if listing_at_market_floor(ask, market_floor):
                found.append("цена рынка — шарит за NFT")
        listed = {gift.slug for gift in snapshot.cheap_gifts if gift.slug}
        richness = profile_richness(metrics)
        if live.max_activity_score > 0 and richness > live.max_activity_score:
            found.append(f"профиль слишком живой {richness} > {live.max_activity_score}")
        regular = len(snapshot.regular_gifts)
        if live.max_regular_gifts > 0 and regular > live.max_regular_gifts:
            found.append(f"обычных гифтов {regular} > {live.max_regular_gifts}")
        resale = [gift for gift in snapshot.unique_gifts if gift.on_resale]
        if len(resale) >= 2:
            found.append("несколько NFT на ресейле — флиппер")
        if listing_hugs_floor(_listing_ask(snapshot), _fair_value(snapshot)):
            found.append("цена у оценки — шарит за NFT")
        for gift in snapshot.unique_gifts:
            if gift.slug in listed:
                continue
            floor = gift.best_floor_ton
            if floor is not None and floor >= live.floor_max_ton:
                found.append("в профиле есть дорогой NFT — не один случайный лот")
                break
        return found

    @staticmethod
    def _cheapest(gifts: list[UniqueGift]) -> Optional[UniqueGift]:
        priced = [gift for gift in gifts if gift.best_floor_ton is not None]
        if not priced:
            return None
        return min(priced, key=lambda gift: gift.best_floor_ton or 0.0)


def _listing_ask(snapshot: ProfileSnapshot) -> Optional[float]:
    for gift in snapshot.cheap_gifts or snapshot.unique_gifts:
        if gift.on_resale and gift.market_floor_ton is not None:
            return gift.market_floor_ton
    return None


def _fair_value(snapshot: ProfileSnapshot) -> Optional[float]:
    gifts = snapshot.cheap_gifts or snapshot.unique_gifts
    values = [gift.fair_value_ton for gift in gifts if gift.fair_value_ton]
    return min(values) if values else None
