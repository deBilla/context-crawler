"""Tests for LLM output normalisation.

These cover the shapes actually observed in production data.json, where 87% of
records had no usable end date and several rows were page furniture.
"""

from datetime import date

import pytest

from extractor.normalize import (
    coerce_date,
    is_placeholder_deal,
    normalize_deal,
    normalize_text_field,
)


class TestCoerceDate:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("2025-09-30", date(2025, 9, 30)),
            ("2025-9-3", date(2025, 9, 3)),
            ("30th September 2025", date(2025, 9, 30)),
            ("30 Sep 2025", date(2025, 9, 30)),
            ("1 April 2026", date(2026, 4, 1)),
            ("September 30, 2025", date(2025, 9, 30)),
            ("Sept 3 2025", date(2025, 9, 3)),
            # Day-first is the Sri Lankan convention.
            ("05/09/2025", date(2025, 9, 5)),
            ("30.09.2025", date(2025, 9, 30)),
            # Embedded in a sentence.
            ("Offer valid till 25th December 2025 only", date(2025, 12, 25)),
        ],
    )
    def test_parses_real_dates(self, raw, expected):
        assert coerce_date(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        [
            None,
            "",
            "   ",
            "N/A",
            "Not specified",
            "Not specified in the text",
            "Not provided in the text",
            "Unknown",
            "Ongoing",
            "Open-ended",
            "Until further notice",
            "TBD",
            "while stocks last",
        ],
    )
    def test_filler_becomes_null(self, raw):
        assert coerce_date(raw) is None

    def test_invalid_calendar_date_is_null(self):
        assert coerce_date("31st February 2025") is None
        assert coerce_date("2025-13-45") is None

    def test_passes_through_date_objects(self):
        assert coerce_date(date(2025, 1, 1)) == date(2025, 1, 1)

    def test_never_raises_on_odd_input(self):
        assert coerce_date(12345) is None
        assert coerce_date({"a": 1}) is None


class TestNormalizeTextField:
    def test_filler_becomes_null(self):
        assert normalize_text_field("Not specified in the text") is None

    def test_real_text_survives(self):
        assert normalize_text_field("Valid on weekdays") == "Valid on weekdays"

    def test_text_merely_containing_filler_survives(self):
        value = "Maximum discount not specified in the text but capped at LKR 5000"
        assert normalize_text_field(value) == value


class TestIsPlaceholderDeal:
    @pytest.mark.parametrize(
        "deal",
        [
            {
                "promotion_title": "No Offers Available",
                "description": "There are no offers available at the moment, please watch this space.",
            },
            {"promotion_title": "Malaysia Offer Details", "description": "View the details of this offer"},
            {"promotion_title": "Korea Visa Offers", "description": "View Offer Details"},
            {"promotion_title": "Something", "description": "Click here"},
            {"promotion_title": "Coming Soon", "description": "New offers coming soon"},
        ],
    )
    def test_detects_page_furniture(self, deal):
        assert is_placeholder_deal(deal) is True

    @pytest.mark.parametrize(
        "deal",
        [
            {
                "promotion_title": "Up to 20% Off Supermarket",
                "description": "Enjoy the best supermarket deals at Spar with ComBank Credit Cards",
            },
            # "details" appearing inside a real description must not trip it.
            {
                "promotion_title": "25% off dining",
                "description": "Get 25% off. View offer details on the bank website for the full terms.",
            },
        ],
    )
    def test_keeps_real_deals(self, deal):
        assert is_placeholder_deal(deal) is False


class TestNormalizeDeal:
    def test_coerces_both_dates(self):
        out = normalize_deal({"valid_from": "1st September 2025", "valid_until": "30/09/2025"})
        assert out["valid_from"] == date(2025, 9, 1)
        assert out["valid_until"] == date(2025, 9, 30)

    def test_single_day_offer_gets_an_end_date(self):
        """The po-cb-0029 shape: start date set, end date left null."""
        out = normalize_deal(
            {
                "valid_from": "2025-09-25",
                "valid_until": None,
                "promotion_title": "Up to 20% Off Supermarket",
                "terms_and_conditions": "Offer valid on 25th September 2025",
            }
        )
        assert out["valid_until"] == date(2025, 9, 25)

    def test_open_ended_offer_keeps_null_end_date(self):
        out = normalize_deal(
            {
                "valid_from": "2025-09-01",
                "valid_until": "Ongoing",
                "description": "0% easy payment plan",
            }
        )
        assert out["valid_from"] == date(2025, 9, 1)
        assert out["valid_until"] is None

    def test_reversed_range_is_swapped(self):
        out = normalize_deal({"valid_from": "2025-09-30", "valid_until": "2025-09-01"})
        assert out["valid_from"] == date(2025, 9, 1)
        assert out["valid_until"] == date(2025, 9, 30)

    def test_does_not_mutate_input(self):
        original = {"valid_from": "1 Sep 2025"}
        normalize_deal(original)
        assert original["valid_from"] == "1 Sep 2025"

    def test_missing_date_keys_are_left_absent(self):
        out = normalize_deal({"promotion_title": "x"})
        assert "valid_from" not in out
        assert "valid_until" not in out
