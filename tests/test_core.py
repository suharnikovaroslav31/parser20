"""Проверки цены лота и фильтров без сети."""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from typing import Optional
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
        activity_score=20,
        stars_rating_level=1,
        stars_rating_stars=10,
        stars_fetched=True,
        gifts_fetched=True,
        stargifts_count=None,
        lang_code="ru",
    )
    base.update(kwargs)
    return AccountMetrics(**base)


def _gift(slug: str = "PlushPepe-1", price: float = 2.0, *, unsaved: bool = False, collection_floor: Optional[float] = None) -> UniqueGift:
    ask = price
    floor = collection_floor if collection_floor is not None else (price * 3.0 if price else None)
    return UniqueGift(
        slug=slug,
        title="Plush Pepe",
        number=1,
        on_resale=True,
        telegram_floor_ton=floor,
        fair_value_ton=floor,
        market_floor_ton=ask,
        market_source="telegram_resale",
        unsaved=unsaved,
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
            max_account_age_days=None,
            filter_seller_age=False,
            min_activity_score=0,
            max_activity_score=55,
            max_regular_gifts=0,
            require_noob_profile=True,
            require_russian=True,
        )
        self.flt = ProfileFilter(self.live)

    def test_match_rating1_two_nfts_young(self) -> None:
        extra = _gift("B-2", 3.0)
        extra.on_resale = False
        gifts = [_gift("A-1"), extra]
        snap = _snapshot(gifts=gifts, metrics=_metrics(), price=2.0)
        snap.source = "recent_gift_peer"
        decision = self.flt.evaluate(snap)
        self.assertTrue(decision.matched, decision.reasons)

    def test_skip_two_resale_flipper(self) -> None:
        gifts = [_gift("A-1"), _gift("B-2", 3.0)]
        snap = _snapshot(gifts=gifts, metrics=_metrics(), price=2.0)
        decision = self.flt.evaluate(snap)
        self.assertFalse(decision.matched)

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

    def test_old_account_still_matches(self) -> None:
        snap = _snapshot(gifts=[_gift()], metrics=_metrics(account_age_days=400), price=2.0)
        decision = self.flt.evaluate(snap)
        self.assertTrue(decision.matched, decision.reasons)

    def test_skip_profile_not_opened(self) -> None:
        snap = _snapshot(gifts=[_gift()], metrics=_metrics(stars_fetched=False, stars_rating_level=1), price=2.0)
        decision = self.flt.evaluate(snap)
        self.assertFalse(decision.matched)

    def test_skip_gifts_not_read(self) -> None:
        snap = _snapshot(gifts=[_gift()], metrics=_metrics(gifts_fetched=False), price=2.0)
        decision = self.flt.evaluate(snap)
        self.assertFalse(decision.matched)

    def test_skip_verified(self) -> None:
        snap = _snapshot(gifts=[_gift()], metrics=_metrics(is_verified=True), price=2.0)
        decision = self.flt.evaluate(snap)
        self.assertFalse(decision.matched)

    def test_premium_still_matches(self) -> None:
        snap = _snapshot(gifts=[_gift()], metrics=_metrics(is_premium=True), price=2.0)
        decision = self.flt.evaluate(snap)
        self.assertTrue(decision.matched, decision.reasons)

    def test_skip_reseller_bio(self) -> None:
        snap = _snapshot(gifts=[_gift()], metrics=_metrics(bio="скупка nft / mrkt"), price=2.0)
        decision = self.flt.evaluate(snap)
        self.assertFalse(decision.matched)
        self.assertTrue(any("био" in reason for reason in decision.reasons))

    def test_skip_personal_channel(self) -> None:
        snap = _snapshot(gifts=[_gift()], metrics=_metrics(personal_channel_id=123), price=2.0)
        decision = self.flt.evaluate(snap)
        self.assertFalse(decision.matched)

    def test_skip_trader_username(self) -> None:
        snap = _snapshot(gifts=[_gift()], metrics=_metrics(username="nftfloor"), price=2.0)
        decision = self.flt.evaluate(snap)
        self.assertFalse(decision.matched)

    def test_skip_hidden_stargifts_count(self) -> None:
        snap = _snapshot(gifts=[_gift()], metrics=_metrics(stargifts_count=20), price=2.0)
        decision = self.flt.evaluate(snap)
        self.assertFalse(decision.matched)
        self.assertTrue(any("прячут" in reason for reason in decision.reasons))

    def test_few_extra_gifts_still_match(self) -> None:
        snap = _snapshot(gifts=[_gift()], metrics=_metrics(stargifts_count=4), price=2.0)
        decision = self.flt.evaluate(snap)
        self.assertTrue(decision.matched, decision.reasons)

    def test_skip_hidden_nft(self) -> None:
        listed = _gift("A-1", 2.0)
        hidden = _gift("B-2", 3.0, unsaved=True)
        snap = _snapshot(gifts=[listed, hidden], metrics=_metrics(), price=2.0)
        snap.cheap_gifts = [listed]
        decision = self.flt.evaluate(snap)
        self.assertFalse(decision.matched)
        self.assertTrue(any("скрыт" in reason for reason in decision.reasons))

    def test_listed_hidden_from_profile_skips(self) -> None:
        listed = _gift("A-1", 2.0, unsaved=True)
        snap = _snapshot(gifts=[listed], metrics=_metrics(), price=2.0)
        snap.cheap_gifts = [listed]
        decision = self.flt.evaluate(snap)
        self.assertFalse(decision.matched)
        self.assertTrue(any("скрыт" in reason for reason in decision.reasons))

    def test_skip_expensive_second_nft(self) -> None:
        listed = _gift("A-1", 2.0)
        rich = _gift("B-2", 40.0)
        snap = _snapshot(gifts=[listed, rich], metrics=_metrics(), price=2.0)
        snap.cheap_gifts = [listed]
        decision = self.flt.evaluate(snap)
        self.assertFalse(decision.matched)

    def test_empty_noob_still_matches(self) -> None:
        snap = _snapshot(
            gifts=[_gift()],
            metrics=_metrics(username=None, has_photo=False, bio=""),
            price=2.0,
        )
        snap.source = "recent_gift_peer"
        decision = self.flt.evaluate(snap)
        self.assertTrue(decision.matched, decision.reasons)

    def test_skip_english_profile(self) -> None:
        snap = _snapshot(
            gifts=[_gift()],
            metrics=_metrics(lang_code="en", first_name="John", last_name="Smith"),
            price=2.0,
        )
        decision = self.flt.evaluate(snap)
        self.assertFalse(decision.matched)
        self.assertTrue(any("русск" in reason for reason in decision.reasons))

    def test_cyrillic_name_matches_without_lang(self) -> None:
        from core.filters import name_has_cyrillic

        self.assertTrue(name_has_cyrillic("Сергей", "Иванов"))
        self.assertFalse(name_has_cyrillic("Dima", ""))
        snap = _snapshot(
            gifts=[_gift()],
            metrics=_metrics(lang_code=None, first_name="Сергей", last_name="Иванов"),
            price=2.0,
        )
        decision = self.flt.evaluate(snap)
        self.assertTrue(decision.matched, decision.reasons)

    def test_latin_name_without_lang_still_matches(self) -> None:
        snap = _snapshot(
            gifts=[_gift()],
            metrics=_metrics(lang_code=None, first_name="Dima", last_name=""),
            price=2.0,
        )
        snap.source = "recent_gift_peer"
        decision = self.flt.evaluate(snap)
        self.assertTrue(decision.matched, decision.reasons)

    def test_skip_chinese_script(self) -> None:
        snap = _snapshot(
            gifts=[_gift()],
            metrics=_metrics(lang_code=None, first_name="小明"),
            price=2.0,
        )
        decision = self.flt.evaluate(snap)
        self.assertFalse(decision.matched)
        self.assertTrue(any("русск" in reason for reason in decision.reasons))

    def test_skip_old_market_shell(self) -> None:
        snap = _snapshot(
            gifts=[_gift()],
            metrics=_metrics(username=None, has_photo=False, bio="", account_age_days=400),
            price=2.0,
        )
        snap.source = "tg_market"
        decision = self.flt.evaluate(snap)
        self.assertFalse(decision.matched)
        self.assertTrue(any("пустой" in reason for reason in decision.reasons))

    def test_old_mamont_from_people_still_matches(self) -> None:
        snap = _snapshot(
            gifts=[_gift()],
            metrics=_metrics(account_age_days=400, username=None, has_photo=True, bio=""),
            price=2.0,
        )
        snap.source = "recent_gift_peer"
        decision = self.flt.evaluate(snap)
        self.assertTrue(decision.matched, decision.reasons)

    def test_skip_burner_name_on_market(self) -> None:
        snap = _snapshot(
            gifts=[_gift()],
            metrics=_metrics(
                username=None,
                has_photo=False,
                bio="",
                first_name="Ywnnwkan",
                last_name="Absoanwbw",
                account_age_days=40,
            ),
            price=2.0,
        )
        snap.source = "tg_market"
        decision = self.flt.evaluate(snap)
        self.assertFalse(decision.matched)
        self.assertTrue(any("альт" in reason for reason in decision.reasons))

    def test_young_empty_market_shell_skips(self) -> None:
        snap = _snapshot(
            gifts=[_gift()],
            metrics=_metrics(username=None, has_photo=False, bio="", first_name="Маша", account_age_days=20),
            price=2.0,
        )
        snap.source = "tg_market"
        decision = self.flt.evaluate(snap)
        self.assertFalse(decision.matched)
        self.assertTrue(any("пустой" in reason for reason in decision.reasons))

    def test_skip_listing_at_current_market_floor(self) -> None:
        gift = _gift(price=5.0, collection_floor=9.0)
        gift.telegram_floor_ton = 5.0
        gift.fair_value_ton = None
        snap = _snapshot(gifts=[gift], metrics=_metrics(), price=5.0)
        decision = self.flt.evaluate(snap)
        self.assertFalse(decision.matched)
        self.assertTrue(any("рынка" in reason for reason in decision.reasons))

    def test_skip_market_latin_name(self) -> None:
        snap = _snapshot(
            gifts=[_gift()],
            metrics=_metrics(lang_code="ru", first_name="Dima", last_name=""),
            price=2.0,
        )
        snap.source = "tg_market"
        decision = self.flt.evaluate(snap)
        self.assertFalse(decision.matched)
        self.assertTrue(any("кириллиц" in reason for reason in decision.reasons))

    def test_skip_listing_near_last_sale(self) -> None:
        snap = _snapshot(
            gifts=[_gift(price=7.5, collection_floor=8.0)],
            metrics=_metrics(),
            price=7.5,
        )
        decision = self.flt.evaluate(snap)
        self.assertFalse(decision.matched)
        self.assertTrue(any("оценк" in reason or "перекупа" in reason for reason in decision.reasons))

    def test_small_undercut_is_flipper(self) -> None:
        gift = _gift(price=4.7, collection_floor=5.0)
        gift.fair_value_ton = None
        snap = _snapshot(gifts=[gift], metrics=_metrics(), price=4.7)
        decision = self.flt.evaluate(snap)
        self.assertFalse(decision.matched)
        self.assertTrue(any("рынка" in reason or "оценк" in reason for reason in decision.reasons))

    def test_received_unsaved_gift_matches(self) -> None:
        gift = _gift(price=2.0, unsaved=True)
        gift.on_resale = False
        gift.market_floor_ton = None
        snap = _snapshot(gifts=[gift], metrics=_metrics(), price=2.0)
        snap.source = "recent_gift_peer"
        decision = self.flt.evaluate(snap)
        self.assertTrue(decision.matched, decision.reasons)

    def test_no_fair_value_does_not_skip(self) -> None:
        gift = _gift(price=2.0, collection_floor=7.0)
        gift.fair_value_ton = None
        snap = _snapshot(gifts=[gift], metrics=_metrics(), price=2.0)
        decision = self.flt.evaluate(snap)
        self.assertTrue(decision.matched, decision.reasons)

    def test_unlisted_profile_nft_still_matches(self) -> None:
        gift = _gift(price=2.0, collection_floor=2.0)
        gift.on_resale = False
        gift.market_floor_ton = None
        snap = _snapshot(gifts=[gift], metrics=_metrics(), price=2.0)
        snap.source = "recent_gift_peer"
        decision = self.flt.evaluate(snap)
        self.assertTrue(decision.matched, decision.reasons)


class FilterSchemaTests(unittest.TestCase):
    def test_schema2_restores_original_filters(self) -> None:
        import json
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "filters.json"
            path.write_text(
                json.dumps(
                    {
                        "floor_max_ton": 15.0,
                        "max_unique_gifts": 5,
                        "stars_rating_min": 0,
                        "stars_rating_max": 2,
                        "require_stars_rating": False,
                        "max_account_age_days": 365,
                        "alert_cooldown_hours": 2,
                        "schema_version": 2,
                    }
                ),
                encoding="utf-8",
            )
            live = LiveFilters(path=path)
            live.load()
            self.assertTrue(live.require_stars_rating)
            self.assertEqual(live.max_unique_gifts, 2)
            self.assertEqual(live.stars_rating_max, 1)
            self.assertFalse(live.filter_seller_age)
            self.assertIsNone(live.max_account_age_days)
            self.assertEqual(live.floor_max_ton, 10.0)
            self.assertGreaterEqual(live.schema_version, 9)
            self.assertTrue(live.require_noob_profile)
            self.assertTrue(live.require_russian)
            self.assertEqual(live.max_activity_score, 55)
            self.assertIsNone(live.require_premium)


class SlugTests(unittest.TestCase):
    def test_slug_from_parts(self) -> None:
        from core.market import marketplace_slug

        self.assertEqual(marketplace_slug({"slug": "PlushPepe-1"}), "PlushPepe-1")
        self.assertEqual(
            marketplace_slug({"name": "Plush Pepe", "gift_num": 12}),
            "PlushPepe-12",
        )
        self.assertEqual(marketplace_slug({"name": "Desk Calendar #7"}), "DeskCalendar-7")


class ProfileNftCountTests(unittest.TestCase):
    def test_keeps_profile_without_listed_extra(self) -> None:
        listed = _gift("C-3")
        profile = [_gift("A-1"), _gift("B-2")]
        merged = profile_unique_gifts(profile, listed, gifts_fetched=True)
        self.assertEqual([gift.slug for gift in merged], ["A-1", "B-2"])

    def test_empty_profile_uses_listed(self) -> None:
        listed = _gift("A-1")
        merged = profile_unique_gifts([], listed, gifts_fetched=True)
        self.assertEqual([gift.slug for gift in merged], ["A-1"])

    def test_empty_profile_with_hidden_count_is_not_one_nft(self) -> None:
        listed = _gift("A-1")
        merged = profile_unique_gifts([], listed, gifts_fetched=True, stargifts_count=20)
        self.assertEqual(merged, [])

    def test_unread_profile_counts_nothing(self) -> None:
        listed = _gift("A-1")
        merged = profile_unique_gifts([], listed, gifts_fetched=False)
        self.assertEqual(merged, [])


class TrackerVersionTests(unittest.TestCase):
    def test_tracker_version_bumped(self) -> None:
        self.assertGreaterEqual(TRACKER_VERSION, 8)
        self.assertTrue(callable(ListingTracker))

    def test_expired_lot_is_processed_again(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "seen.json"
            tracker = ListingTracker(path)
            tracker.mark("tg:PlushPepe-1")
            self.assertFalse(tracker.should_process("tg:PlushPepe-1"))
            tracker._done["tg:PlushPepe-1"] = 0
            self.assertTrue(tracker.should_process("tg:PlushPepe-1"))


class NanotonTests(unittest.TestCase):
    def test_nanoton_constant(self) -> None:
        self.assertEqual(NANOTON, 1_000_000_000)


class SearchDirectionTests(unittest.TestCase):
    def test_telegram_scans_new_not_floor(self) -> None:
        from core.market import CHEAP_PAGES, COLLECTIONS_PER_PASS, FLOOR_SAMPLE, MAX_NOOB_COLLECTIONS, NEW_PAGES

        self.assertEqual(CHEAP_PAGES, 0)
        self.assertGreaterEqual(NEW_PAGES, 1)
        self.assertLessEqual(NEW_PAGES, 2)
        self.assertGreaterEqual(FLOOR_SAMPLE, 10)
        self.assertLess(COLLECTIONS_PER_PASS, MAX_NOOB_COLLECTIONS)
        self.assertGreaterEqual(COLLECTIONS_PER_PASS, 12)

    def test_people_source_skips_old_dialogs(self) -> None:
        from core.market import LIVE_PEOPLE_SOURCES

        self.assertIn("live_gift_received", LIVE_PEOPLE_SOURCES)
        self.assertIn("recent_gift_peer", LIVE_PEOPLE_SOURCES)
        self.assertNotIn("dialog", LIVE_PEOPLE_SOURCES)
        self.assertNotIn("contact", LIVE_PEOPLE_SOURCES)

    def test_fresh_noob_requires_live_resale(self) -> None:
        from core.market import GiftMarketScanner

        listed = SimpleNamespace(on_resale=True)
        owned = SimpleNamespace(on_resale=False)
        self.assertFalse(
            GiftMarketScanner._is_fresh_noob(SimpleNamespace(source="dialog", unique_gifts=[listed]))
        )
        self.assertFalse(
            GiftMarketScanner._is_fresh_noob(
                SimpleNamespace(source="live_gift_received", unique_gifts=[owned])
            )
        )
        self.assertTrue(
            GiftMarketScanner._is_fresh_noob(
                SimpleNamespace(source="live_gift_received", unique_gifts=[listed])
            )
        )

    def test_empty_market_name_is_not_flipper(self) -> None:
        from core.market import GiftMarketScanner

        blank = SimpleNamespace(first_name="", last_name="")
        latin = SimpleNamespace(first_name="John", last_name="Smith")
        rus = SimpleNamespace(first_name="Иван", last_name="")
        self.assertFalse(GiftMarketScanner._market_seller_is_flipper(blank, source="tg_market"))
        self.assertTrue(GiftMarketScanner._market_seller_is_flipper(latin, source="tg_market"))
        self.assertFalse(GiftMarketScanner._market_seller_is_flipper(rus, source="tg_market"))

    def test_gift_action_prefers_recipient_peer(self) -> None:
        from core.parser import ProfileScanner

        action = SimpleNamespace(peer=SimpleNamespace(user_id=777), to_id=None)
        self.assertEqual(ProfileScanner._action_recipient_id(action), 777)


class SessionCleanTests(unittest.TestCase):
    def test_strips_quotes_and_whitespace(self) -> None:
        from config import _clean_session_string

        self.assertEqual(_clean_session_string(' "abc+def==" \n'), "abc+def==")
        self.assertEqual(_clean_session_string("none"), "")


class ChatIdTests(unittest.TestCase):
    def test_adds_supergroup_prefix(self) -> None:
        from config import normalize_telegram_chat_id

        self.assertEqual(normalize_telegram_chat_id(-5425946278), -1005425946278)
        self.assertEqual(normalize_telegram_chat_id(-1005425946278), -1005425946278)

    def test_tries_both_group_id_formats(self) -> None:
        from config import log_group_id_candidates

        self.assertEqual(log_group_id_candidates(-5425946278), [-5425946278, -1005425946278])
        self.assertEqual(log_group_id_candidates(-1005425946278), [-1005425946278, -5425946278])


class BuildTests(unittest.TestCase):
    def test_build_id(self) -> None:
        from core.runtime import BUILD

        self.assertRegex(BUILD, r"^20\d{6}-\d+$")


class ClaimCardTests(unittest.TestCase):
    def test_claimed_notice_has_no_custom_emoji(self) -> None:
        from bot.claims import ClaimLot, claimed_notice

        lot = ClaimLot(
            token="abc",
            title="Plush Pepe",
            slug="PlushPepe-1",
            number=1,
            price_ton=4.5,
            source="tg_market",
            seller_id=1,
            seller_name="Ivan",
            seller_username="ivan",
            nft_link="https://t.me/nft/PlushPepe-1",
            getgems_link="",
            rating=1,
            created=0,
        )
        who = SimpleNamespace(full_name="Антон", username="anton", id=2)
        text = claimed_notice(who, lot)
        self.assertNotIn("tg-emoji", text)
        self.assertIn("Лот занят", text)
        self.assertIn("Plush Pepe", text)

    def test_claim_store_survives_reload(self) -> None:
        from bot.claims import ClaimLot, ClaimStore

        folder = Path(tempfile.mkdtemp())
        path = folder / "claims.json"
        store = ClaimStore(path)
        lot = ClaimLot(
            token="deadbeef",
            title="Pepe",
            slug="Pepe-1",
            number=1,
            price_ton=1.0,
            source="tg_market",
            seller_id=9,
            seller_name="A",
            seller_username=None,
            nft_link="",
            getgems_link="",
            rating=1,
            created=time.time(),
        )
        store.put(lot)
        taken = store.take("deadbeef")
        self.assertIsNotNone(taken)
        again = ClaimStore(path)
        self.assertTrue(again.was_claimed("deadbeef"))
        self.assertIsNone(again.get("deadbeef"))


if __name__ == "__main__":
    unittest.main()
