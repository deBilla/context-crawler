"""Deal extraction logic."""

import json
import logging
from typing import Any

from shared.llm_client import LLMClient
from shared.models import CreditCardDeal

from extractor.normalize import is_placeholder_deal, normalize_deal
from extractor.prompts import (
    CANONICAL_CATEGORIES,
    EXTRACTION_PROMPT,
    VALID_CARD_TYPES,
    prepare_content,
)

logger = logging.getLogger(__name__)


async def extract_deals(
    llm: LLMClient,
    url: str,
    title: str,
    content: str,
    max_chars: int = 8000,
    raise_on_error: bool = False,
) -> list[CreditCardDeal]:
    """Extract credit card deals from page content using LLM.

    Args:
        llm: LLMClient instance
        url: Source URL
        title: Page title
        content: Page content text
        max_chars: Maximum content characters to send to LLM
        raise_on_error: Propagate LLM and parse failures instead of returning
            an empty list. The queue-driven service leaves this off — a bad
            page should not kill the consumer — but any caller that caches or
            records the result must turn it on, because otherwise "the model
            was rate limited" is indistinguishable from "this page has no
            offers", and the failure gets stored as fact.

    Returns:
        List of validated CreditCardDeal objects
    """
    try:
        # Prepare content
        prepared_content = prepare_content(content, max_chars)

        if not prepared_content:
            logger.warning("Content is empty after preparation for URL: %s", url)
            return []

        # Format prompt
        prompt = EXTRACTION_PROMPT.format(
            page_title=title,
            url=url,
            content=prepared_content,
            card_types=", ".join(VALID_CARD_TYPES),
            categories=", ".join(CANONICAL_CATEGORIES),
        )

        # Call LLM
        logger.debug("Calling LLM to extract deals from %s", url)
        response = await llm.complete_json(prompt, max_tokens=4000)

        if not response:
            logger.warning("LLM returned empty response for URL: %s", url)
            return []

        # Parse response — normalize to list
        deals_data = response
        if isinstance(deals_data, dict):
            # LLM sometimes wraps deals in {"deals": [...]} or returns a single deal
            if "deals" in deals_data and isinstance(deals_data["deals"], list):
                deals_data = deals_data["deals"]
            else:
                deals_data = [deals_data]
        if not isinstance(deals_data, list):
            logger.warning(
                "LLM response is not a list for URL: %s, got type: %s",
                url,
                type(deals_data).__name__,
            )
            return []

        # Validate and collect deals
        valid_deals: list[CreditCardDeal] = []
        for i, deal_data in enumerate(deals_data):
            try:
                if not isinstance(deal_data, dict):
                    logger.warning(
                        "Deal %d is not a dict for URL: %s", i, url
                    )
                    continue

                # Normalize common LLM field-name mismatches
                aliases = {
                    "title": "promotion_title",
                    "name": "promotion_title",
                    "promo_title": "promotion_title",
                    "offer_title": "promotion_title",
                    "bank": "bank_name",
                    "card": "card_name",
                    "desc": "description",
                    "offer_description": "description",
                    "details": "description",
                    "type": "category",
                    "discount": "discount_percentage",
                    "merchant": "merchant_name",
                    "valid_start": "valid_from",
                    "start_date": "valid_from",
                    "valid_end": "valid_until",
                    "end_date": "valid_until",
                    "expiry_date": "valid_until",
                    "terms": "terms_and_conditions",
                    "conditions": "terms_and_conditions",
                }
                for old_key, new_key in aliases.items():
                    if old_key in deal_data and new_key not in deal_data:
                        deal_data[new_key] = deal_data.pop(old_key)

                # Drop "No offers available" notices and bare "View details"
                # stubs before they reach the database and, from there, the site.
                if is_placeholder_deal(deal_data):
                    logger.info(
                        "Skipping placeholder deal %d from %s: %r",
                        i,
                        url,
                        deal_data.get("promotion_title"),
                    )
                    continue

                # Coerce dates and filler strings before validation. Without
                # this a value like "30th September 2025" fails the `date` type
                # and the whole deal is discarded, losing the discount, merchant
                # and terms along with the date.
                deal_data = normalize_deal(deal_data)

                # Inject source_url — the LLM doesn't know it
                deal_data["source_url"] = url
                deal = CreditCardDeal(**deal_data)
                valid_deals.append(deal)
            except Exception as e:
                logger.warning(
                    "Failed to validate deal %d from URL %s: %s\nDeal data: %s",
                    i,
                    url,
                    str(e),
                    json.dumps(deal_data, default=str)[:500],
                )
                continue

        logger.info(
            "Extracted %d valid deals from %s", len(valid_deals), url
        )
        return valid_deals

    except json.JSONDecodeError as e:
        logger.error("Failed to parse LLM response as JSON for URL %s: %s",
                     url, str(e))
        if raise_on_error:
            raise
        return []
    except Exception as e:
        logger.error(
            "Failed to extract deals from URL %s: %s", url, str(e)
        )
        if raise_on_error:
            raise
        return []
