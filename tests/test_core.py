"""Проверки цены лота и фильтров без сети."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from core.filters import ProfileFilter
from core.listings import VERSION as TRACKER_VERSION, ListingTracker
from core.market import listing_price_ton, profile_unique_gifts
from core.models import AccountMetrics, ProfileSnapshot, UniqueGift
from core.runtime import LiveFilters
from core.ton_client import NANOTON


def _metrics(**kwargs) -> AccountMetrics:
    base = dict(
        user_id=8_800_000_000,
        username="seller",
        first_name="A",
        last_name="",
        is_premium=False,
        is_verified=False,
        has_photo=True,
        bio="",
        personal_channel_id=None,
        common_chats_count=0,
        approx_registered_at=None,
        account_age_days=30,
        public_channel_count=0,
        activity_score=20,
        stars_rating_level=1,
        stars_rating_stars=10,
    )
    base.update(kwargs)
    return AccountMetrics(**base)


def _gift(slug: str = "PlushPepe-1", price: float = 2.0) -> UniqueGift:
    return UniqueGift(
        slug=slug,
        title="Plush Pepe",
        number=1,
        on_resale=True,
        telegram_floor_ton=price,
        market_floor_ton=price,
        market_source="telegram_resale",
    )


def _snapshot(*, gifts: list[UniqueGift], metrics: AccountMetrics, price: float) -> ProfileSnapshot:
    return ProfileSnapshot(
        metrics=metrics,
        unique_gifts=gifts,
        regular_gifts=[],
        estimated_value_ton=price,
        estimated_value_usd=0,
        min_floor_ton=price,
        cheap_gifts=gifts[:1],
        ton_usd=5.0,
        processed_ms=1.0,
        source="tg_market",
        fingerprint_key=f"tg_market:{gifts[0].slug}:{price:.4f}",
    )


class ListingPriceTests(unittest.TestCase):
    def test_ton_amount(self) -> None:
        class StarsTonAmount:
            def __init__(self, amount: int) -> None:
                self.amount = amount

        gift = SimpleNamespace(resell_amount=[StarsTonAmount(2_500_000_000)], value_amount=None)
        price = listing_price_ton(gift, ton_usd=5.0, stars_usd=0.013)
        self.assertAlmostEqual(price, 2.5)

    def test_small_ton_already_normalized(self) -> None:
        class StarsTonAmount:
            def __init__(self, amount: float) -> None:
                self.amount = amount

        gift = SimpleNamespace(resell_amount=[StarsTonAmount(3.2)], value_amount=None)
        price = listing_price_ton(gift, ton_usd=5.0, stars_usd=0.013)
        self.assertAlmostEqual(price, 3.2)


class FilterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.live = LiveFilters(
            scanner_enabled=True,
            floor_min_ton=0.0,
            floor_max_ton=10.0,
            min_unique_gifts=1,
            max_unique_gifts=2,
            stars_rating_min=1,
            stars_rating_max=1,
            require_stars_rating=True,
            max_account_age_days=90,
            filter_seller_age=True,
            min_activity_score=0,
        )
        self.flt = ProfileFilter(self.live)

    def test_match_rating1_two_nfts_young(self) -> None:
        gifts = [_gift("A-1"), _gift("B-2", 3.0)]
        snap = _snapshot(gifts=gifts, metrics=_metrics(), price=2.0)
        decision = self.flt.evaluate(snap)
        self.assertTrue(decision.matched, decision.reasons)

    def test_skip_rating_missing(self) -> None:
        snap = _snapshot(gifts=[_gift()], metrics=_metrics(stars_rating_level=None), price=2.0)
        decision = self.flt.evaluate(snap)
        self.assertFalse(decision.matched)
        self.assertTrue(any("рейтинг" in reason.lower() or "Stars" in reason for reason in decision.reasons))

    def test_skip_too_many_nfts(self) -> None:
        gifts = [_gift("A-1"), _gift("B-2"), _gift("C-3")]
        snap = _snapshot(gifts=gifts, metrics=_metrics(), price=2.0)
        decision = self.flt.evaluate(snap)
        self.assertFalse(decision.matched)

    def test_skip_expensive(self) -> None:
        snap = _snapshot(gifts=[_gift(price=12.0)], metrics=_metrics(), price=12.0)
        decision = self.flt.evaluate(snap)
        self.assertFalse(decision.matched)

    def test_skip_old_account(self) -> None:
        snap = _snapshot(gifts=[_gift()], metrics=_metrics(account_age_days=400), price=2.0)
        decision = self.flt.evaluate(snap)
        self.assertFalse(decision.matched)


class LooseFilterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.live = LiveFilters(
            scanner_enabled=True,
            floor_min_ton=0.0,
            floor_max_ton=15.0,
            min_unique_gifts=1,
            max_unique_gifts=5,
            stars_rating_min=0,
            stars_rating_max=2,
            require_stars_rating=False,
            max_account_age_days=365,
            filter_seller_age=True,
            min_activity_score=0,
        )
        self.flt = ProfileFilter(self.live)

    def test_match_without_stars_rating(self) -> None:
        snap = _snapshot(gifts=[_gift()], metrics=_metrics(stars_rating_level=None), price=2.0)
        decision = self.flt.evaluate(snap)
        self.assertTrue(decision.matched, decision.reasons)

    def test_match_rating_two(self) -> None:
        snap = _snapshot(gifts=[_gift()], metrics=_metrics(stars_rating_level=2), price=4.0)
        decision = self.flt.evaluate(snap)
        self.assertTrue(decision.matched, decision.reasons)

    def test_match_five_nfts(self) -> None:
        gifts = [_gift(f"G-{i}") for i in range(5)]
        snap = _snapshot(gifts=gifts, metrics=_metrics(), price=2.0)
        decision = self.flt.evaluate(snap)
        self.assertTrue(decision.matched, decision.reasons)

    def test_skip_high_rating(self) -> None:
        snap = _snapshot(gifts=[_gift()], metrics=_metrics(stars_rating_level=5), price=2.0)
        decision = self.flt.evaluate(snap)
        self.assertFalse(decision.matched)

    def test_match_year_old_account(self) -> None:
        snap = _snapshot(gifts=[_gift()], metrics=_metrics(account_age_days=300), price=2.0)
        decision = self.flt.evaluate(snap)
        self.assertTrue(decision.matched, decision.reasons)


class FilterSchemaTests(unittest.TestCase):
    def test_old_filters_json_is_widened(self) -> None:
        import json
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "filters.json"
            path.write_text(
                json.dumps(
                    {
                        "floor_max_ton": 10.0,
                        "max_unique_gifts": 2,
                        "stars_rating_min": 1,
                        "stars_rating_max": 1,
                        "require_stars_rating": True,
                        "max_account_age_days": 90,
                        "alert_cooldown_hours": 24,
                        "schema_version": 1,
                    }
                ),
                encoding="utf-8",
            )
            live = LiveFilters(path=path)
            live.load()
            self.assertFalse(live.require_stars_rating)
            self.assertEqual(live.max_unique_gifts, 5)
            self.assertEqual(live.stars_rating_max, 2)
            self.assertEqual(live.max_account_age_days, 365)
            self.assertEqual(live.floor_max_ton, 15.0)
            self.assertGreaterEqual(live.schema_version, 2)


class ProfileNftCountTests(unittest.TestCase):
    def test_keeps_profile_without_listed_extra(self) -> None:
        listed = _gift("C-3")
        profile = [_gift("A-1"), _gift("B-2")]
        merged = profile_unique_gifts(profile, listed)
        self.assertEqual([gift.slug for gift in merged], ["A-1", "B-2"])

    def test_empty_profile_uses_listed(self) -> None:
        listed = _gift("A-1")
        merged = profile_unique_gifts([], listed)
        self.assertEqual([gift.slug for gift in merged], ["A-1"])


class TrackerVersionTests(unittest.TestCase):
    def test_tracker_version_bumped(self) -> None:
        self.assertGreaterEqual(TRACKER_VERSION, 5)
        self.assertTrue(callable(ListingTracker))


class NanotonTests(unittest.TestCase):
    def test_nanoton_constant(self) -> None:
        self.assertEqual(NANOTON, 1_000_000_000)


if __name__ == "__main__":
    unittest.main()
