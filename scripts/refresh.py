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
import json
import logging
import re
import sys
import time
from collections import Counter, defaultdict, deque
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
for _pkg in ("shared", "services/crawler", "services/parser", "services/extractor", "scripts"):
    sys.path.insert(0, str(ROOT / _pkg))

from crawler.config import CrawlerConfig  # noqa: E402
from crawler.fetcher import FetchError, PageFetcher, create_http_client  # noqa: E402
from export_deals_json import BANK_CODES, normalize_bank, row_to_offerspot  # noqa: E402
from extractor.extract import extract_deals  # noqa: E402
from parser.html_parser import extract_links, extract_text, extract_title  # noqa: E402
from parser.link_filter import filter_urls  # noqa: E402
from parser.relevance import check_relevance, pre_filter  # noqa: E402
from shared.config import LLMConfig  # noqa: E402
from shared.llm_client import create_llm_client  # noqa: E402

logger = logging.getLogger("refresh")

DEFAULT_CACHE = ROOT / ".refresh-cache.json"
DEFAULT_EXPORT = ROOT / "deals_export.json"


class PageCache:
    """Per-URL content hash plus the deals last extracted from that page.

    Storing the deals and not just the hash is the point: an unchanged page can
    then contribute its offers again without a second trip to the LLM. Without
    that, a nightly run would re-extract all ~845 offers every time.
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

    def hit(self, url: str, content_hash: str) -> list[dict] | None:
        if not self.enabled:
            return None
        entry = self.entries.get(url)
        if entry and entry.get("hash") == content_hash:
            return entry.get("deals", [])
        return None

    def store(self, url: str, content_hash: str, deals: list[dict]) -> None:
        self.entries[url] = {"hash": content_hash, "deals": deals}

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
) -> tuple[list[dict], list[str]]:
    """Fetch one page; return (deals as CreditCardDeal dicts, links to follow)."""
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
        return [], []

    html = result.content
    try:
        title = extract_title(html)
        links = extract_links(html, url)
    except Exception as exc:  # noqa: BLE001 — malformed HTML must not stop the crawl
        logger.warning("parse failed %s: %s", url, exc)
        stats["parse_failed"] += 1
        return [], []

    patterns = [re.compile(p) for p in bank.get("url_patterns", [])]
    followable = filter_urls(links, domain, max_depth, depth, patterns or None)

    cached = cache.hit(url, result.content_hash)
    if cached is not None:
        stats["unchanged"] += 1
        return cached, followable

    stats["changed"] += 1
    text = extract_text(html)

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
        stats["irrelevant"] += 1
        cache.store(url, result.content_hash, [])
        return [], followable

    stats["llm_extract"] += 1
    try:
        deals = await extract_with_retry(llm, url, title, text, stats=stats)
    except ExtractionFailed as exc:
        # Deliberately not cached. Storing [] here would record "this page has
        # no offers" as fact, and the page would then be skipped on every
        # future run until the bank happens to edit it — which is how a
        # rate-limited run silently erased six banks.
        logger.warning("giving up on %s: %s", url, exc)
        stats["extract_failed"] += 1
        stats["consecutive_failures"] += 1
        # Failures that keep coming are not bad pages, they are a dead
        # provider — a spent rate-limit window looks exactly like this. Stop
        # the run so the caller sees an error, rather than marching through
        # the remaining banks recording zero offers for each.
        if stats["consecutive_failures"] >= MAX_CONSECUTIVE_FAILURES:
            raise ProviderUnavailable(
                f"{stats['consecutive_failures']} extractions failed in a row; "
                f"last error: {exc}"
            )
        return [], followable

    stats["consecutive_failures"] = 0
    payload = [deal.model_dump(mode="json") for deal in deals]
    stats["deals_found"] += len(payload)
    cache.store(url, result.content_hash, payload)
    return payload, followable


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

    while frontier and len(seen) <= max_pages:
        url, depth = frontier.popleft()
        deals, links = await process_page(
            url, depth, bank,
            fetcher=fetcher, llm=llm, cache=cache, throttle=throttle,
            max_depth=max_depth, use_puppeteer=use_puppeteer, stats=stats,
        )
        collected.extend(deals)
        for link in links:
            if link not in seen and len(seen) < max_pages:
                seen.add(link)
                frontier.append((link, depth + 1))

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
            "llm_relevance", "llm_extract", "irrelevant", "deals_found",
            "puppeteer_unavailable",
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
