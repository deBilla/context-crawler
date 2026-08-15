"""Refresh the offer data in one process — no Docker, no Redis, no Postgres.

The compose stack exists to spread a wide crawl across containers, and it is
the wrong shape for the recurring job this project actually needs: revisit six
bank sites, notice what changed, and hand the result to offerspot. That job has
no use for queues, a blob store or a dashboard, and the pipeline's daemons make
it awkward — an async producer-consumer graph never reports "finished", so
anything scheduling it has to poll queue depths and guess.

None of the modules that do the real work are entangled with that machinery,
though. fetcher, html_parser, link_filter, relevance and extract are all pure
over HTML and LLM calls, so this driver imports them directly and walks the
frontier as an in-memory list. Same fetching, same prompts, same normalisation
as the services; just a plain BFS instead of Redis round-trips.

    python3 scripts/refresh.py --merge ../offerspot/src/app/api/data.json

Repeat runs are cheap. Every page's content hash and the deals extracted from
it are cached, so an unchanged page costs one conditional fetch and no LLM
call at all — which is what makes the subscription-backed claude_code provider
viable for a nightly run.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import re
import sys
import time
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
for _pkg in ("shared", "services/crawler", "services/parser", "services/extractor", "scripts"):
    sys.path.insert(0, str(ROOT / _pkg))

from crawler.config import CrawlerConfig  # noqa: E402
from crawler.fetcher import FetchError, PageFetcher, create_http_client  # noqa: E402
from export_deals_json import BANK_CODES, normalize_bank, row_to_offerspot  # noqa: E402
from extractor.extract import extract_deals, validate_deals  # noqa: E402
from extractor.prompts import (  # noqa: E402
    CANONICAL_CATEGORIES,
    EXTRACTION_PROMPT,
    VALID_CARD_TYPES,
    prepare_content,
)
from parser.html_parser import extract_links, extract_text, extract_title  # noqa: E402
from parser.link_filter import filter_urls  # noqa: E402
from parser.relevance import check_relevance, pre_filter  # noqa: E402
from shared.config import LLMConfig  # noqa: E402
from shared.llm_client import create_llm_client  # noqa: E402

logger = logging.getLogger("refresh")

DEFAULT_CACHE = ROOT / ".refresh-cache.json"
DEFAULT_EXPORT = ROOT / "deals_export.json"


class PageCache:
    """Per-URL content fingerprint plus the deals last extracted from that page.

    Storing the deals and not just the fingerprint is the point: an unchanged
    page can then contribute its offers again without a second trip to the LLM.
    Without that, a nightly run would re-extract everything every time.

    The fingerprint is of the *extracted text*, not the raw HTML. Bank pages are
    not byte-stable between fetches — People's Bank and Commercial Bank both
    return a different HTML hash every request, differing by a byte or two of
    per-request markup — so hashing the response made every page look changed
    and the cache never hit. The text that survives boilerplate stripping is
    stable, and it is also exactly what the model is shown, so if it has not
    moved neither has the extraction.
    """

    def __init__(self, path: Path, enabled: bool = True) -> None:
        self.path = path
        self.enabled = enabled
        self.entries: dict[str, dict[str, Any]] = {}
        if enabled and path.exists():
            try:
                self.entries = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Ignoring unreadable cache %s: %s", path, exc)

    def hit(self, url: str, fingerprint: str) -> list[dict] | None:
        if not self.enabled:
            return None
        entry = self.entries.get(url)
        if entry and entry.get("hash") == fingerprint:
            return entry.get("deals", [])
        return None

    def store(self, url: str, fingerprint: str, deals: list[dict]) -> None:
        self.entries[url] = {"hash": fingerprint, "deals": deals}

    def save(self) -> None:
        if not self.enabled:
            return
        self.path.write_text(json.dumps(self.entries, ensure_ascii=False), encoding="utf-8")


class DomainThrottle:
    """In-process replacement for the Redis-backed DomainRateLimiter.

    One process means a dict of last-hit timestamps is enough; the point is
    only to keep from hammering a bank, which is per-domain not per-worker.
    """

    def __init__(self, delay: float) -> None:
        self.delay = delay
        self._last: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def acquire(self, domain: str) -> None:
        async with self._locks[domain]:
            wait = self.delay - (time.monotonic() - self._last.get(domain, 0.0))
            if wait > 0:
                await asyncio.sleep(wait)
            self._last[domain] = time.monotonic()


def text_fingerprint(text: str) -> str:
    """Hash of the page text the model will be shown."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Pending:
    """A page that passed relevance and still needs extracting."""

    url: str
    title: str
    text: str
    fingerprint: str


class ExtractionFailed(RuntimeError):
    """Extraction did not complete — as distinct from finding nothing."""


class ProviderUnavailable(RuntimeError):
    """Too many extractions failed in a row to believe the provider is up."""


# One dead page is normal; a run of them is a spent rate-limit window or an
# outage. Low enough to stop before a whole bank is written off as empty.
MAX_CONSECUTIVE_FAILURES = 5


# Enough attempts to ride out a blip, few enough that a genuine outage is not
# hammered. The subscription-backed provider is the reason this exists: its
# limits reset on a window, so failures arrive in long runs rather than singly.
EXTRACT_ATTEMPTS = 3
EXTRACT_BACKOFF_SECONDS = (5, 20)


# Pages sent to the model together. The fixed system-prompt preamble is paid
# once per call rather than once per page, which measured 40% fewer tokens at
# three; larger batches push the JSON reply toward the output ceiling, where a
# truncated array costs the whole group rather than one page.
EXTRACT_BATCH_SIZE = 3


async def extract_batch(llm, pages: list[tuple[str, str, str]], *, stats: Counter):
    """Extract several pages in one call. Returns {url: [CreditCardDeal]}.

    Pages are labelled by ordinal rather than by URL and mapped back here.
    Asking the model to echo a URL invites near-miss strings that silently
    attribute a deal to the wrong page; an integer either matches a page we
    sent or it does not.
    """
    if len(pages) == 1:
        url, title, text = pages[0]
        return {url: await extract_with_retry(llm, url, title, text, stats=stats)}

    blocks = []
    for index, (url, title, text) in enumerate(pages, start=1):
        prepared = prepare_content(text, 8000)
        blocks.append(f"=== PAGE {index} ===\nURL: {url}\nTITLE: {title}\nCONTENT:\n{prepared}")

    prompt = EXTRACTION_PROMPT.format(
        page_title="(multiple pages)",
        url="(multiple pages)",
        content="\n\n".join(blocks),
        card_types=", ".join(VALID_CARD_TYPES),
        categories=", ".join(CANONICAL_CATEGORIES),
    ) + (
        f"\n\nThe content above contains {len(pages)} pages delimited by === PAGE n ===."
        " Return ONE flat JSON array covering every page, and give each deal a"
        ' "page" field holding the integer of the page it came from.'
    )

    stats["llm_extract_batched"] += 1
    for attempt in range(EXTRACT_ATTEMPTS):
        try:
            response = await llm.complete_json(prompt, max_tokens=8000)
            break
        except Exception as exc:  # noqa: BLE001 — provider errors are untyped
            if attempt == EXTRACT_ATTEMPTS - 1:
                raise ExtractionFailed(str(exc) or "no error text from provider") from exc
            stats["extract_retried"] += 1
            await asyncio.sleep(EXTRACT_BACKOFF_SECONDS[attempt])

    if not isinstance(response, list):
        # A batch that comes back the wrong shape is not evidence about any
        # single page, so fall back to one call each rather than record zeros.
        logger.warning("batched extraction returned %s, falling back to per-page", type(response).__name__)
        stats["batch_fallback"] += 1
        return {
            url: await extract_with_retry(llm, url, title, text, stats=stats)
            for url, title, text in pages
        }

    grouped: dict[str, list] = {url: [] for url, _, _ in pages}
    for deal in response:
        if not isinstance(deal, dict):
            continue
        page = deal.pop("page", None)
        if not isinstance(page, int) or not 1 <= page <= len(pages):
            stats["batch_unattributed"] += 1
            continue
        grouped[pages[page - 1][0]].append(deal)

    return {url: validate_deals(raw, url) for url, raw in grouped.items()}


async def extract_with_retry(llm, url: str, title: str, text: str, *, stats: Counter):
    last: Exception | None = None
    for attempt in range(EXTRACT_ATTEMPTS):
        try:
            return await extract_deals(llm, url, title, text, raise_on_error=True)
        except Exception as exc:  # noqa: BLE001 — provider errors are untyped
            last = exc
            if attempt < EXTRACT_ATTEMPTS - 1:
                delay = EXTRACT_BACKOFF_SECONDS[attempt]
                stats["extract_retried"] += 1
                logger.warning(
                    "extract failed for %s (attempt %d/%d): %s — retrying in %ds",
                    url, attempt + 1, EXTRACT_ATTEMPTS, exc, delay,
                )
                await asyncio.sleep(delay)
    raise ExtractionFailed(str(last) or "no error text from provider")


async def puppeteer_available(fetcher: PageFetcher, url: str) -> bool:
    """Probe the sidecar so a missing container degrades instead of erroring."""
    try:
        await fetcher.fetch_with_puppeteer(url)
        return True
    except (FetchError, Exception):  # noqa: BLE001 — any failure means unusable
        return False


async def process_page(
    url: str,
    depth: int,
    bank: dict,
    *,
    fetcher: PageFetcher,
    llm,
    cache: PageCache,
    throttle: DomainThrottle,
    max_depth: int,
    use_puppeteer: bool,
    stats: Counter,
) -> tuple[list[dict], list[str], "Pending | None"]:
    """Fetch and screen one page.

    Returns (deals already known from cache, links to follow, work still owed
    to the extractor). Only one of the first and third is ever non-empty.
    """
    domain = urlparse(url).netloc
    await throttle.acquire(domain)

    try:
        if use_puppeteer:
            result = await fetcher.fetch_with_puppeteer(url)
        else:
            result = await fetcher.fetch(url)
    except FetchError as exc:
        logger.warning("fetch failed %s: %s", url, exc)
        stats["fetch_failed"] += 1
        return [], [], None

    html = result.content
    try:
        title = extract_title(html)
        links = extract_links(html, url)
    except Exception as exc:  # noqa: BLE001 — malformed HTML must not stop the crawl
        logger.warning("parse failed %s: %s", url, exc)
        stats["parse_failed"] += 1
        return [], [], None

    patterns = [re.compile(p) for p in bank.get("url_patterns", [])]
    followable = filter_urls(links, domain, max_depth, depth, patterns or None)

    text = extract_text(html)
    fingerprint = text_fingerprint(text)

    cached = cache.hit(url, fingerprint)
    if cached is not None:
        stats["unchanged"] += 1
        return cached, followable, None

    stats["changed"] += 1

    # Two-stage relevance gate, matching parser/main.py: the cheap URL/title
    # heuristic answers most pages, and only genuine ambiguity costs a call.
    verdict = pre_filter(url, title)
    if verdict == "likely":
        relevant = True
    elif verdict == "unlikely":
        relevant = False
    else:
        stats["llm_relevance"] += 1
        relevant, _ = await check_relevance(llm, url, title, text)
    stats[f"prefilter_{verdict}"] += 1

    if not relevant:
        # Safe to cache: this is a judgement about the page, not a failed call.
        stats["irrelevant"] += 1
        cache.store(url, fingerprint, [])
        return [], followable, None

    # Extraction is deferred so several pages can share one LLM call; the
    # caller batches whatever comes back pending.
    return [], followable, Pending(url=url, title=title, text=text, fingerprint=fingerprint)


async def crawl_bank(
    bank: dict,
    *,
    fetcher: PageFetcher,
    llm,
    cache: PageCache,
    throttle: DomainThrottle,
    config: CrawlerConfig,
    max_depth: int,
    max_pages: int,
    stats: Counter,
) -> list[dict]:
    """Breadth-first walk of one bank, bounded by its own page ceiling."""
    name = bank["name"]
    use_puppeteer = bool(bank.get("needs_puppeteer"))

    if use_puppeteer:
        probe = bank["seed_urls"][0]
        if not await puppeteer_available(fetcher, probe):
            logger.warning(
                "%s needs the Puppeteer sidecar and it is unreachable at %s — "
                "falling back to plain HTTP, so JS-rendered offers will be missed",
                name, config.puppeteer_url,
            )
            use_puppeteer = False
            stats["puppeteer_unavailable"] += 1

    seen: set[str] = set()
    frontier: deque[tuple[str, int]] = deque((url, 0) for url in bank["seed_urls"])
    seen.update(bank["seed_urls"])
    collected: list[dict] = []

    pending: list[Pending] = []

    async def drain(batch: list[Pending]) -> None:
        """Extract a batch and fold the results into `collected` and the cache."""
        if not batch:
            return
        stats["llm_extract"] += 1
        try:
            results = await extract_batch(
                llm, [(item.url, item.title, item.text) for item in batch], stats=stats
            )
        except ExtractionFailed as exc:
            # Nothing here is cached. Recording [] would state "these pages
            # have no offers" as fact, and they would then be skipped every
            # future run until the bank edits them — which is how a
            # rate-limited run silently erased six banks.
            logger.warning("giving up on %d page(s): %s", len(batch), exc)
            stats["extract_failed"] += len(batch)
            stats["consecutive_failures"] += 1
            # Failures that keep coming are a dead provider, not dull pages —
            # a spent rate-limit window looks exactly like this. Stop, so the
            # caller sees an error instead of zeros for every remaining bank.
            if stats["consecutive_failures"] >= MAX_CONSECUTIVE_FAILURES:
                raise ProviderUnavailable(
                    f"{stats['consecutive_failures']} extractions failed in a row; "
                    f"last error: {exc}"
                )
            return

        stats["consecutive_failures"] = 0
        for item in batch:
            payload = [deal.model_dump(mode="json") for deal in results.get(item.url, [])]
            stats["deals_found"] += len(payload)
            cache.store(item.url, item.fingerprint, payload)
            collected.extend(payload)

    while frontier and len(seen) <= max_pages:
        url, depth = frontier.popleft()
        deals, links, owed = await process_page(
            url, depth, bank,
            fetcher=fetcher, llm=llm, cache=cache, throttle=throttle,
            max_depth=max_depth, use_puppeteer=use_puppeteer, stats=stats,
        )
        collected.extend(deals)
        if owed is not None:
            pending.append(owed)
            # Batches never span banks: one bank's markup must not influence
            # how another's is read, and attribution stays within one site.
            if len(pending) >= EXTRACT_BATCH_SIZE:
                await drain(pending)
                pending = []
        for link in links:
            if link not in seen and len(seen) < max_pages:
                seen.add(link)
                frontier.append((link, depth + 1))

    await drain(pending)

    logger.info("%s: %d pages, %d deals", name, len(seen), len(collected))
    stats["pages"] += len(seen)
    return collected


def to_offerspot(deals: list[dict]) -> list[dict]:
    """Convert CreditCardDeal dicts to the site's shape, reusing the exporter.

    The IDs here are provisional. merge_into_offerspot.py ignores everything
    but the bank-code prefix and matches on content, so numbering restarts each
    run without disturbing anything already published.
    """
    counters: Counter = Counter()
    offers = []
    for deal in deals:
        title = (deal.get("promotion_title") or "").strip()
        if len(title) < 5:
            continue
        # Look the code up under the canonical name, the same one
        # row_to_offerspot will store — otherwise a raw "Commercial Bank of
        # Ceylon PLC" takes the "unk" prefix while its bank field reads
        # "Commercial Bank", and the two disagree on the same offer.
        code = BANK_CODES.get(normalize_bank(deal.get("bank_name", "")), "unk")
        counters[code] += 1
        offers.append(row_to_offerspot(deal, f"po-{code}-{counters[code]:04d}"))
    return offers


def deals_from_cache(cache: PageCache) -> list[dict]:
    """Every deal the cache is holding, without touching the network.

    Lets an export be rebuilt after a normalisation or mapping change without
    re-crawling and re-paying for extraction.
    """
    return [deal for entry in cache.entries.values() for deal in entry.get("deals", [])]


def finish(
    args: argparse.Namespace, all_deals: list[dict], stats: Counter, *, offline: bool = False
) -> int:
    """Convert, write, report and optionally merge. Shared by both paths."""
    offers = to_offerspot(all_deals)
    Path(args.out).write_text(
        json.dumps(offers, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    if not offline:
        logger.info("--- run summary ---")
        for label in (
            "pages", "unchanged", "changed", "fetch_failed", "parse_failed",
            "prefilter_likely", "prefilter_unlikely", "prefilter_uncertain",
            "llm_relevance", "llm_extract", "llm_extract_batched", "extract_retried",
            "extract_failed", "batch_fallback", "batch_unattributed", "irrelevant",
            "deals_found", "puppeteer_unavailable",
        ):
            logger.info("  %-22s %d", label, stats[label])
        logger.info("  %-22s %d", "llm calls total",
                    stats["llm_relevance"] + stats["llm_extract"])
    logger.info("wrote %d offers to %s", len(offers), args.out)

    if args.merge:
        from merge_into_offerspot import load, merge

        existing = load(Path(args.merge))
        merged, merge_stats = merge(existing, offers)
        ids = [o["id"] for o in merged]
        if len(set(ids)) != len(ids):
            logger.error("merge produced duplicate IDs — not writing")
            return 1
        Path(args.merge).write_text(
            json.dumps(merged, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        logger.info(
            "merged into %s: +%d new, %d updated, %d unchanged, %d untouched",
            args.merge, merge_stats["added"], merge_stats["updated"],
            merge_stats["unchanged"], merge_stats["absent_from_export"],
        )

    return 0


async def run(args: argparse.Namespace) -> int:
    banks_config = json.loads((ROOT / "config" / "banks.json").read_text(encoding="utf-8"))
    banks = banks_config["banks"]
    if args.banks:
        wanted = {b.strip().lower() for b in args.banks.split(",")}
        banks = [b for b in banks if b["name"].lower() in wanted]
        if not banks:
            logger.error("No banks matched %s", args.banks)
            return 1

    config = CrawlerConfig()
    stats: Counter = Counter()
    cache = PageCache(Path(args.cache), enabled=not args.no_cache)

    if args.forget_empty:
        needle = args.forget_empty.lower()
        stale = [u for u, v in cache.entries.items()
                 if needle in urlparse(u).netloc.lower() and not v.get("deals")]
        for u in stale:
            del cache.entries[u]
        cache.save()
        logger.info("forgot %d cached-empty pages matching %r", len(stale), args.forget_empty)
        return 0

    if args.offline:
        if not cache.entries:
            logger.error("--offline needs a populated cache; %s is empty or missing", args.cache)
            return 1
        all_deals = deals_from_cache(cache)
        logger.info("offline rebuild: %d pages, %d deals from cache",
                    len(cache.entries), len(all_deals))
        return finish(args, all_deals, stats, offline=True)

    llm_config = LLMConfig()
    logger.info("LLM provider: %s (%s)", llm_config.llm_provider, llm_config.llm_model)

    throttle = DomainThrottle(config.request_delay)

    client = create_http_client(config)
    fetcher = PageFetcher(client, config)
    llm = create_llm_client(llm_config)

    all_deals: list[dict] = []
    aborted: ProviderUnavailable | None = None
    try:
        for bank in banks:
            try:
                all_deals.extend(await crawl_bank(
                    bank,
                    fetcher=fetcher, llm=llm, cache=cache, throttle=throttle,
                    config=config, max_depth=args.max_depth,
                    max_pages=args.max_pages_per_bank, stats=stats,
                ))
            except ProviderUnavailable as exc:
                # Stop crawling, but keep what earlier banks produced and save
                # the cache — the point is to avoid recording the remaining
                # banks as empty, not to throw away a half-finished run.
                aborted = exc
                break
    finally:
        await client.aclose()
        await llm.close()
        cache.save()

    if aborted is not None:
        logger.error("ABORTED: %s", aborted)
        logger.error(
            "%d banks were not reached. Nothing was cached for the failures, so "
            "re-running once the provider recovers will retry them.",
            len(banks) - len({d.get("bank_name") for d in all_deals}),
        )

    exit_code = finish(args, all_deals, stats)
    # A partial run must not exit 0: weekly-refresh.sh keys its publish
    # decision off this, and the sanity floors alone would happily publish a
    # catalogue missing every bank we never reached.
    return 1 if aborted is not None else exit_code


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--banks", help="comma-separated bank names (default: all)")
    parser.add_argument("--max-depth", type=int, default=2,
                        help="link depth from each seed (default: 2)")
    parser.add_argument("--max-pages-per-bank", type=int, default=200,
                        help="hard page ceiling per bank (default: 200)")
    parser.add_argument("--out", default=str(DEFAULT_EXPORT),
                        help=f"where to write the export (default: {DEFAULT_EXPORT.name})")
    parser.add_argument("--cache", default=str(DEFAULT_CACHE),
                        help=f"page cache file (default: {DEFAULT_CACHE.name})")
    parser.add_argument("--no-cache", action="store_true",
                        help="ignore the cache and re-extract every page")
    parser.add_argument("--forget-empty", metavar="DOMAIN",
                        help="drop cached empty results for this domain so the next run "
                             "re-extracts them. Use after a run failed provider-side and "
                             "recorded pages as having no offers.")
    parser.add_argument("--offline", action="store_true",
                        help="rebuild the export from the cache alone — no network, no "
                             "LLM calls. Use after changing normalisation or bank mappings.")
    parser.add_argument("--merge", metavar="DATA_JSON",
                        help="also merge the export into this offerspot data.json")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    sys.exit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
