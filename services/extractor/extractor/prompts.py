"""Prompts and utilities for deal extraction."""

import re

CANONICAL_CATEGORIES = [
    "Dining & Restaurants",
    "Shopping & Retail",
    "Travel & Lodging",
    "Health & Wellness",
    "Groceries & Supermarkets",
    "Online Shopping",
    "Fuel",
    "Other",
]

VALID_CARD_TYPES = [
    "Credit",
    "Debit",
    "Platinum",
    "Gold",
    "Silver",
    "Rewards",
    "Cashback",
    "Signature",
    "Premium",
    "Classic",
]

EXTRACTION_PROMPT = """Extract all credit card promotion deals from the following page content.

Page Title: {page_title}
Page URL: {url}
Content:
{content}

For each deal found, extract the following information:
- bank_name: Name of the bank (required)
- card_name: Name of the credit card (required)
- card_types: List of card types (must be from: {card_types})
- promotion_title: Title/name of the promotion (required)
- description: Detailed description of the promotion (required)
- category: Category of the deal (must be from: {categories})
- discount_percentage: Discount as a percentage (e.g., 10 for 10%)
- discount_amount: Fixed discount amount in LKR
- max_discount_lkr: Maximum discount limit in LKR
- merchant_name: Name of merchant/store if applicable
- merchant_category: Category of merchant if applicable
- valid_from: Start date, strictly "YYYY-MM-DD", or null
- valid_until: End date, strictly "YYYY-MM-DD", or null
- terms_and_conditions: Terms and conditions text

DATE RULES (important — dates are stored as real dates, not text):
- Always output dates as "YYYY-MM-DD". Never write descriptive text such as
  "Not specified", "Ongoing", "Till stocks last" or "30th September" in a date
  field. If the page does not state a date, output null.
- Convert what the page does say: "Till 30th September 2025" -> "2025-09-30",
  "01.09.2025 - 30.09.2025" -> valid_from "2025-09-01", valid_until "2025-09-30".
- Day-first is the Sri Lankan convention: "05/09/2025" is 5 September 2025.
- If only a year-less date is given, infer the year from the page context; if
  that is genuinely ambiguous, output null rather than guessing.
- A single-day promotion ("valid on 25th September 2025") sets BOTH valid_from
  and valid_until to that date.

SKIP non-promotions. Do not return a deal for:
- Notices that there is nothing on offer ("No offers available at the moment",
  "Watch this space", "Coming soon").
- Navigation or listing stubs whose only description would be "View offer
  details", "Click here", "Read more" or similar.
- Generic card product pages that describe a card rather than a promotion.
If the page contains no real promotions, return an empty array.

Return a JSON array of deals. Each deal must have bank_name, card_name, card_types,
promotion_title, description, and category. Other fields can be null if not mentioned.

Example format:
[
  {{
    "bank_name": "Sample Bank",
    "card_name": "Sample Card",
    "card_types": ["Credit", "Gold"],
    "promotion_title": "Sample Promotion",
    "description": "Sample description",
    "category": "Dining & Restaurants",
    "discount_percentage": 10,
    "discount_amount": null,
    "max_discount_lkr": 5000,
    "merchant_name": "Sample Restaurant",
    "merchant_category": "Dining",
    "valid_from": "2026-01-01",
    "valid_until": "2026-12-31",
    "terms_and_conditions": "Sample terms"
  }}
]

Return ONLY the JSON array, no additional text."""


def prepare_content(text: str, max_chars: int) -> str:
    """Clean and prepare content for LLM processing.

    Args:
        text: Raw page content
        max_chars: Maximum characters to keep

    Returns:
        Cleaned and truncated content
    """
    # Normalize whitespace
    cleaned = re.sub(r"\s+", " ", text)

    # Truncate to max length
    if len(cleaned) > max_chars:
        cleaned = cleaned[:max_chars]

    return cleaned.strip()
