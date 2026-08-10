"""Merge a fresh deals export into offerspot's data.json without churning IDs.

`export_deals_json.py` numbers offers by counting rows as it walks the table, so
the IDs it produces shift whenever a row is added or removed upstream. Those IDs
are fine for a first import and useless for a refresh: feeding a new export
straight into the site would renumber almost every offer, breaking
`/offer/<id>` URLs and every saved card in the wallet.

So the merge ignores incoming IDs entirely. It matches each incoming offer to an
existing one by content, keeps the existing ID when it finds a match, and only
mints a new ID for genuinely new offers — continuing each bank's counter from
the highest one already in use.

Usage:
    python3 scripts/export_deals_json.py > /tmp/deals_export.json
    python3 scripts/merge_into_offerspot.py /tmp/deals_export.json \
        --data ../offerspot/src/app/api/data.json

Add --dry-run to see the summary without writing.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Tuple

ID_PATTERN = re.compile(r"^po-([a-z]+)-(\d+)$")

# Fields the bank can legitimately change between crawls. Everything else is
# either part of the identity key or derived from it.
MUTABLE_FIELDS = ("bank", "card_types", "category", "offer_details", "validity", "terms")

Offer = Dict[str, Any]
Key = Tuple[str, str, str, str]


def normalize(value: Any) -> str:
    """Collapse whitespace and case so trivial re-wording is not a new offer."""
    return " ".join(str(value or "").split()).lower()


def identity(offer: Offer) -> Key:
    """Content key for matching an incoming offer to an existing one.

    source_url alone is far too coarse — Commercial Bank serves 100+ offers off
    a single promotions page. Title plus merchant is nearly enough; the leading
    slice of the description separates the handful of same-merchant offers that
    share a title (three distinct Daraz deals, for one).
    """
    merchant = offer.get("merchant") or {}
    return (
        normalize(offer.get("source_url")),
        normalize(offer.get("title")),
        normalize(merchant.get("name")),
        normalize(offer.get("description"))[:80],
    )


def bank_code(offer_id: str) -> str:
    match = ID_PATTERN.match(offer_id)
    return match.group(1) if match else "unk"


def load(path: Path) -> List[Offer]:
    with path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, list):
        raise SystemExit(f"{path} does not contain a JSON array of offers")
    return data


def merge(existing: List[Offer], incoming: List[Offer]) -> Tuple[List[Offer], Counter]:
    stats: Counter = Counter()

    by_key: Dict[Key, Offer] = {}
    for offer in existing:
        by_key.setdefault(identity(offer), offer)

    # Continue each bank's numbering from the highest ID already issued, so new
    # offers append rather than colliding with retired ones.
    counters: Dict[str, int] = defaultdict(int)
    for offer in existing:
        match = ID_PATTERN.match(offer.get("id", ""))
        if match:
            code = match.group(1)
            counters[code] = max(counters[code], int(match.group(2)))

    merged = list(existing)
    seen: set = set()

    for offer in incoming:
        key = identity(offer)
        if key in seen:
            stats["duplicate_in_export"] += 1
            continue
        seen.add(key)

        current = by_key.get(key)
        if current is None:
            code = bank_code(offer.get("id", ""))
            counters[code] += 1
            fresh = dict(offer)
            fresh["id"] = f"po-{code}-{counters[code]:04d}"
            merged.append(fresh)
            by_key[key] = fresh
            stats["added"] += 1
            continue

        changed = False
        for field in MUTABLE_FIELDS:
            if field in offer and offer[field] != current.get(field):
                current[field] = offer[field]
                changed = True
        # Backfill a logo the earlier crawl missed, but never blank one out.
        logo = (offer.get("merchant") or {}).get("logo_url")
        if logo and not (current.get("merchant") or {}).get("logo_url"):
            current.setdefault("merchant", {})["logo_url"] = logo
            changed = True
        stats["updated" if changed else "unchanged"] += 1

    # Offers the crawl no longer sees are kept: a bank pulling a promotion off
    # its site does not mean the promotion has ended, and the site already hides
    # anything past its end date.
    stats["absent_from_export"] = sum(1 for offer in existing if identity(offer) not in seen)

    return merged, stats


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("export", type=Path, help="JSON produced by export_deals_json.py")
    parser.add_argument(
        "--data",
        type=Path,
        default=Path(__file__).resolve().parents[2] / "offerspot" / "src" / "app" / "api" / "data.json",
        help="offerspot data.json to merge into (default: ../offerspot/src/app/api/data.json)",
    )
    parser.add_argument("--dry-run", action="store_true", help="report without writing")
    args = parser.parse_args()

    existing = load(args.data)
    incoming = load(args.export)

    merged, stats = merge(existing, incoming)

    ids = [offer["id"] for offer in merged]
    if len(set(ids)) != len(ids):
        raise SystemExit("refusing to write: merge produced duplicate IDs")

    print(f"existing: {len(existing)}  export: {len(incoming)}  merged: {len(merged)}", file=sys.stderr)
    for label in ("added", "updated", "unchanged", "duplicate_in_export", "absent_from_export"):
        print(f"  {label}: {stats[label]}", file=sys.stderr)

    if args.dry_run:
        print("dry run — nothing written", file=sys.stderr)
        return

    with args.data.open("w", encoding="utf-8") as handle:
        json.dump(merged, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    print(f"wrote {args.data}", file=sys.stderr)


if __name__ == "__main__":
    main()
