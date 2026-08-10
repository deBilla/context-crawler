"""Core data models shared across all services.

These Pydantic models define the contracts between services. Every piece of data that
crosses a service boundary (through a queue, database, or API) should use one of these
models — never raw dicts.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class CrawlStatus(StrEnum):
    """Lifecycle status of a URL in the pipeline."""
    QUEUED = "queued"
    FETCHING = "fetching"
    FETCHED = "fetched"
    PARSING = "parsing"
    PARSED = "parsed"
    RELEVANT = "relevant"
    IRRELEVANT = "irrelevant"
    EXTRACTING = "extracting"
    EXTRACTED = "extracted"
    FAILED = "failed"
    DEAD = "dead"


class LLMProvider(StrEnum):
    """Supported LLM providers."""
    OLLAMA = "ollama"
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    CLAUDE_CODE = "claude_code"


# ---------------------------------------------------------------------------
# Queue Message Models
# ---------------------------------------------------------------------------

class FrontierItem(BaseModel):
    """An item in the frontier queue — a URL waiting to be crawled."""
    url: str
    depth: int = 0
    priority: int = 0
    source_url: str | None = None
    domain: str = ""
    needs_puppeteer: bool = False


class ParsingQueueItem(BaseModel):
    """Handoff from crawler to parser service."""
    url: str
    minio_path: str
    depth: int = 0
    domain: str = ""


class ExtractionQueueItem(BaseModel):
    """Handoff from parser to extractor — only pages confirmed as relevant."""
    url: str
    minio_path: str
    text_content: str
    page_title: str = ""
    domain: str = ""


# ---------------------------------------------------------------------------
# Database Models (Pydantic representations)
# ---------------------------------------------------------------------------

class URLMetadata(BaseModel):
    """Tracks the lifecycle of every URL through the crawl pipeline."""
    id: int | None = None
    url: str
    domain: str
    file_path: str | None = None
    status: CrawlStatus = CrawlStatus.QUEUED
    status_code: int | None = None
    content_hash: str | None = None
    depth: int = 0
    retry_count: int = 0
    is_seed: bool = False
    error_message: str | None = None
    discovered_at: datetime = Field(default_factory=datetime.utcnow)
    fetched_at: datetime | None = None
    parsed_at: datetime | None = None


class CreditCardDeal(BaseModel):
    """A structured credit card promotion extracted by the LLM.

    Modeled after Sri Lankan bank card promotions — includes LKR-specific
    fields (max_discount_lkr) and canonical categories used across local banks.
    """
    id: int | None = None
    source_url: str
    bank_name: str
    card_name: str | None = None
    card_types: list[str] = Field(default_factory=list)
    promotion_title: str
    description: str
    category: str = "Other"
    discount_percentage: float | None = None
    discount_amount: float | None = None
    max_discount_lkr: float | None = None  # Max discount in Sri Lankan Rupees
    merchant_name: str | None = None
    merchant_category: str | None = None
    merchant_logo_url: str | None = None
    valid_from: date | None = None
    valid_until: date | None = None
    valid_days: list[str] | None = None  # e.g., ["Monday", "Friday"]
    terms_and_conditions: str | None = None
    raw_text: str = ""
    confidence_score: float = 0.0
    extracted_at: datetime = Field(default_factory=datetime.utcnow)


# ---------------------------------------------------------------------------
# Domain Constants (Sri Lankan bank card promotions)
# ---------------------------------------------------------------------------

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
    "Credit Card",
    "Debit Card",
    "Visa",
    "Mastercard",
    "Amex",
]


# ---------------------------------------------------------------------------
# Bank Configuration
# ---------------------------------------------------------------------------

class BankPageConfig(BaseModel):
    """A specific page to crawl for a bank."""
    url: str
    category: str = "General"


class BankConfig(BaseModel):
    """Configuration for crawling a specific bank's promotions."""
    name: str
    url: str
    seed_urls: list[str] = Field(default_factory=list)
    url_patterns: list[str] = Field(default_factory=list)
    exclude_patterns: list[str] = Field(default_factory=list)
    max_depth: int = 3
    max_urls: int = 500
    needs_puppeteer: bool = False
    card_selector: str | None = None
    crawl_strategy: str = "static"
    pages: list[BankPageConfig] = Field(default_factory=list)
    extra: dict[str, Any] = Field(default_factory=dict)
