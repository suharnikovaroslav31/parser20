"""
Фильтры: Stars-рейтинг уровня 1, NFT в профиле, цена лота, возраст, Premium.
Значения берутся из LiveFilters (админ-бот), не из замороженного .env.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from core.models import FilterDecision, ProfileSnapshot, UniqueGift
from core.runtime import LiveFilters

LOGGER = logging.getLogger("tg_gifts.filters")

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


class ProfileFilter:
    def __init__(self, live: LiveFilters) -> None:
        self.live = live

    def evaluate(self, snapshot: ProfileSnapshot) -> FilterDecision:
        live = self.live
        reasons: list[str] = []
        metrics = snapshot.metrics
        unique_count = len(snapshot.unique_gifts)
        cheapest = self._cheapest(snapshot.unique_gifts)
        price = snapshot.min_floor_ton

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

        matched = not reasons
        if matched:
            reasons.append(
                f"OK: рейтинг ур.{level}, {unique_count} NFT, лот {price:g} TON, акк ~{metrics.account_age_days}д"
            )
            LOGGER.info("MATCH user=%s %s", metrics.user_id, reasons[-1])
        else:
            LOGGER.debug("SKIP user=%s %s", metrics.user_id, reasons)

        return FilterDecision(
            matched=matched,
            reasons=reasons,
            snapshot=snapshot,
            cheapest_gift=cheapest,
        )

    @staticmethod
    def _cheapest(gifts: list[UniqueGift]) -> Optional[UniqueGift]:
        priced = [gift for gift in gifts if gift.best_floor_ton is not None]
        if not priced:
            return None
        return min(priced, key=lambda gift: gift.best_floor_ton or 0.0)
