"""Тесты охотника лохов-новичков."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from core.mammoth import (
    MAX_RICHNESS,
    RATING_LEVEL,
    SeenPeople,
    at_floor,
    is_burner,
    is_mammoth,
    listing_price_ton,
    looks_russian,
    profile_richness,
)
from core.models import AccountMetrics, UniqueGift
from core.runtime import BUILD


def _metrics(**kwargs) -> AccountMetrics:
    base = dict(
        user_id=8_800_000_000,
        username=None,
        first_name="Иван",
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
        activity_score=10,
        stars_rating_level=1,
        stars_rating_stars=10,
        stars_fetched=True,
        gifts_fetched=True,
        stargifts_count=1,
        lang_code="ru",
    )
    base.update(kwargs)
    return AccountMetrics(**base)


def _gift(slug: str = "LolPop-1", *, unsaved: bool = False, price: float = 6.5, on_resale: bool = True) -> UniqueGift:
    return UniqueGift(
        slug=slug,
        title="Lol Pop",
        number=1,
        on_resale=on_resale,
        market_floor_ton=price,
        unsaved=unsaved,
    )


class MammothJudgeTests(unittest.TestCase):
    def test_clean_mammoth_matches(self) -> None:
        v = is_mammoth(_metrics(), [_gift()], 6.5, floor=5.0)
        self.assertTrue(v.ok, v.reasons)

    def test_at_floor_skips(self) -> None:
        v = is_mammoth(_metrics(), [_gift(price=5.2)], 5.2, floor=5.0)
        self.assertFalse(v.ok)
        self.assertTrue(any("шарит" in r for r in v.reasons))

    def test_slight_off_floor_ok(self) -> None:
        v = is_mammoth(_metrics(), [_gift(price=5.8)], 5.8, floor=5.0)
        self.assertTrue(v.ok, v.reasons)

    def test_rich_profile_skips(self) -> None:
        rich = _metrics(username="ivanov", is_premium=True, bio="привет", has_photo=True, personal_channel_id=1)
        self.assertGreater(profile_richness(rich), MAX_RICHNESS)
        v = is_mammoth(rich, [_gift()], 6.5, floor=5.0)
        self.assertFalse(v.ok)

    def test_hidden_nft_skips(self) -> None:
        v = is_mammoth(_metrics(), [_gift(), _gift("X-2", unsaved=True)], 6.5, floor=5.0)
        self.assertFalse(v.ok)
        self.assertTrue(any("скрыт" in r for r in v.reasons))

    def test_rating_not_one_skips(self) -> None:
        v = is_mammoth(_metrics(stars_rating_level=2), [_gift()], 6.5, floor=5.0)
        self.assertFalse(v.ok)

    def test_too_many_nft_skips(self) -> None:
        gifts = [_gift("A-1"), _gift("B-2"), _gift("C-3"), _gift("D-4")]
        v = is_mammoth(_metrics(), gifts, 6.5, floor=5.0)
        self.assertFalse(v.ok)

    def test_three_nft_ok(self) -> None:
        gifts = [_gift("A-1"), _gift("B-2", on_resale=False), _gift("C-3", on_resale=False)]
        v = is_mammoth(_metrics(), gifts, 6.5, floor=5.0)
        self.assertTrue(v.ok, v.reasons)

    def test_two_on_resale_skips(self) -> None:
        v = is_mammoth(_metrics(), [_gift("A-1"), _gift("B-2")], 6.5, floor=5.0)
        self.assertFalse(v.ok)
        self.assertTrue(any("флиппер" in r for r in v.reasons))

    def test_price_over_cap_skips(self) -> None:
        from core.mammoth import FLOOR_MAX

        v = is_mammoth(_metrics(), [_gift()], FLOOR_MAX, floor=5.0)
        self.assertFalse(v.ok)

    def test_personal_channel_alone_ok(self) -> None:
        v = is_mammoth(_metrics(personal_channel_id=1), [_gift()], 6.5, floor=5.0)
        self.assertTrue(v.ok, v.reasons)

    def test_foreign_name_skips(self) -> None:
        v = is_mammoth(_metrics(first_name="小明", lang_code=None), [_gift()], 6.5, floor=5.0)
        self.assertFalse(v.ok)

    def test_latin_ru_still_ok(self) -> None:
        v = is_mammoth(_metrics(first_name="Dima", lang_code="ru"), [_gift()], 6.5, floor=5.0)
        self.assertTrue(v.ok, v.reasons)

    def test_shell_skips(self) -> None:
        v = is_mammoth(_metrics(has_photo=False, username=None, bio=""), [_gift()], 6.5, floor=5.0)
        self.assertFalse(v.ok)


class HelperTests(unittest.TestCase):
    def test_floor_band(self) -> None:
        self.assertTrue(at_floor(5.0, 5.0))
        self.assertTrue(at_floor(5.3, 5.0))
        self.assertFalse(at_floor(5.8, 5.0))

    def test_burner(self) -> None:
        self.assertTrue(is_burner("Ywnnwkan", "Absoanwbw"))
        self.assertFalse(is_burner("Dima", ""))

    def test_russian(self) -> None:
        self.assertTrue(looks_russian("Сергей", "", "", None))
        self.assertFalse(looks_russian("John", "Smith", "", "en"))

    def test_price_ton(self) -> None:
        ton = type("StarsTonAmount", (), {"amount": 2_500_000_000})()
        gift = SimpleNamespace(resell_amount=[ton])
        self.assertAlmostEqual(listing_price_ton(gift), 2.5)

    def test_build(self) -> None:
        self.assertEqual(BUILD, "20260920-24")
        self.assertEqual(RATING_LEVEL, 1)


class SeenPeopleTests(unittest.TestCase):
    def test_person_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "seen.json"
            seen = SeenPeople(path)
            self.assertFalse(seen.seen(42))
            seen.mark(42)
            self.assertTrue(seen.seen(42))
            seen.save()
            again = SeenPeople(path)
            self.assertTrue(again.seen(42))


if __name__ == "__main__":
    unittest.main()
