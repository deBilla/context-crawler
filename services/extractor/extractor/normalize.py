"""Normalisation of raw LLM output before Pydantic validation.

`CreditCardDeal.valid_from` / `valid_until` are typed `date | None`, so any
non-ISO value the LLM produces raises a ValidationError and `extract_deals`
drops the **entire deal** — losing the discount, merchant and terms along with
the unparseable date. Coercing here means a deal survives with a real date, or
at worst with a null date, instead of vanishing.

It also filters out rows that are page furniture rather than promotions: banks
routinely publish an "Offers" page carrying a "No offers available at the
moment" notice, and the LLM dutifully extracts it as a deal.
"""

from __future__ import annotations

import logging
import re
from datetime import date
from typing import Any

logger = logging.getLogger(__name__)

# Values the LLM emits to mean "the page didn't say". Matched on the whole
# string so a genuine date is never discarded.
NULL_TOKENS = re.compile(
    r"^\s*(?:"
    r"n/?a|none|null|-{1,2}|"
    r"not\s+(?:specified|provided|mentioned|available|stated|given)"
    r"(?:\s+in\s+the\s+(?:text|page|content))?|"
    r"unknown|unspecified|ongoing|open[-\s]?ended|until\s+further\s+notice|"
    r"tbd|tba|varies|see\s+terms"
    r")\s*\.?\s*$",
    re.IGNORECASE,
)

_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
    "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}
_MONTH_RE = "|".join(sorted(_MONTHS, key=len, reverse=True))

_ISO = re.compile(r"^\s*(\d{4})-(\d{1,2})-(\d{1,2})")
# "30th September 2025", "30 Sep 2025"
_DAY_FIRST = re.compile(
    rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({_MONTH_RE})\.?\,?\s+(\d{{4}})\b", re.IGNORECASE
)
# "September 30, 2025"
_MONTH_FIRST = re.compile(
    rf"\b({_MONTH_RE})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\,?\s+(\d{{4}})\b", re.IGNORECASE
)
# "30/09/2025" or "30.09.2025" — day-first, the Sri Lankan convention.
_NUMERIC = re.compile(r"\b(\d{1,2})[/.](\d{1,2})[/.](\d{4})\b")


def _build(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def coerce_date(value: Any) -> date | str | None:
    """Best-effort conversion of an LLM date value to a `date`.

    Returns a `date` when the value can be understood, `None` when it is a
    "not specified" token or unparseable, and passes through values that are
    already `date` objects. Never raises — callers rely on that.
    """
    if value is None or isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None

    text = value.strip()
    if not text or NULL_TOKENS.match(text):
        return None

    iso = _ISO.match(text)
    if iso:
        return _build(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))

    day_first = _DAY_FIRST.search(text)
    if day_first:
        return _build(
            int(day_first.group(3)), _MONTHS[day_first.group(2).lower()], int(day_first.group(1))
        )

    month_first = _MONTH_FIRST.search(text)
    if month_first:
        return _build(
            int(month_first.group(3)), _MONTHS[month_first.group(1).lower()], int(month_first.group(2))
        )

    numeric = _NUMERIC.search(text)
    if numeric:
        return _build(int(numeric.group(3)), int(numeric.group(2)), int(numeric.group(1)))

    logger.debug("Could not parse date value %r; storing null", text)
    return None


def normalize_text_field(value: Any) -> Any:
    """Turn "Not specified in the text" style filler into a real null."""
    if isinstance(value, str) and NULL_TOKENS.match(value.strip()):
        return None
    return value


# Titles/descriptions that mean "this page has no promotions right now".
_NO_OFFER_NOTICE = re.compile(
    r"\bno\s+offers?\s+(?:are\s+)?(?:currently\s+)?available\b"
    r"|\bthere\s+are\s+no\s+offers\b"
    r"|\bwatch\s+this\s+space\b"
    r"|\bcoming\s+soon\b",
    re.IGNORECASE,
)

# A description that is only a call to action carries no offer information.
_STUB_DESCRIPTION = re.compile(
    r"^\s*(?:view\s+(?:the\s+)?(?:offer\s+)?details?(?:\s+of\s+this\s+offer)?"
    r"|offer\s+details?|click\s+here|read\s+more|learn\s+more|see\s+more"
    r"|best\s+offer)\s*[.!]?\s*$",
    re.IGNORECASE,
)


def is_placeholder_deal(deal_data: dict[str, Any]) -> bool:
    """True when the extracted row is page furniture, not a promotion."""
    title = str(deal_data.get("promotion_title") or "")
    description = str(deal_data.get("description") or "")
    if _NO_OFFER_NOTICE.search(f"{title} {description}"):
        return True
    return bool(_STUB_DESCRIPTION.match(description))


def normalize_deal(deal_data: dict[str, Any]) -> dict[str, Any]:
    """Clean a raw LLM deal dict in place-ish, returning the cleaned copy."""
    cleaned = dict(deal_data)

    for field in ("valid_from", "valid_until"):
        if field in cleaned:
            cleaned[field] = coerce_date(cleaned[field])

    for field in ("terms_and_conditions", "merchant_name", "merchant_category", "card_name"):
        if field in cleaned:
            cleaned[field] = normalize_text_field(cleaned[field])

    # A single-day promotion often arrives with only one of the two dates set.
    if cleaned.get("valid_from") and not cleaned.get("valid_until"):
        text = " ".join(
            str(cleaned.get(key) or "")
            for key in ("promotion_title", "description", "terms_and_conditions")
        )
        if re.search(r"\bvalid\s+(?:only\s+)?on\b|\bone\s+day\s+only\b", text, re.IGNORECASE):
            cleaned["valid_until"] = cleaned["valid_from"]

    # An end date before the start date means the LLM mixed them up.
    start, end = cleaned.get("valid_from"), cleaned.get("valid_until")
    if isinstance(start, date) and isinstance(end, date) and end < start:
        logger.debug("Swapping reversed validity range %s..%s", start, end)
        cleaned["valid_from"], cleaned["valid_until"] = end, start

    return cleaned
