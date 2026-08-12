"""Export credit card deals from Postgres to offerspot-compatible JSON.

Reads straight from the Docker Postgres — no arguments, no piping needed:

    python3 scripts/export_deals_json.py > /tmp/deals_export.json

The IDs it assigns are positional and therefore unstable between runs. Never
feed the output to the site directly; pass it through merge_into_offerspot.py,
which matches on content and keeps the IDs already published.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections import Counter
from pathlib import Path


# Normalize messy bank names from LLM extraction to canonical names
BANK_NAME_MAP = {
    "people's bank": "People's Bank",
    "peoples bank": "People's Bank",
    "people's bank of sri lanka": "People's Bank",
    "bank of ceylon": "Bank of Ceylon",
    "boc": "Bank of Ceylon",
    "commercial bank": "Commercial Bank",
    "comercial bank": "Commercial Bank",
    "commerical bank": "Commercial Bank",
    "commercial bank of ceylon": "Commercial Bank",
    "commerzbank lanka": "Commercial Bank",
    "comerica bank": "Commercial Bank",
    "dfcc bank": "DFCC Bank",
    "hsbc": "HSBC",
    "hsbc lk": "HSBC",
    "hsbc sri lanka": "HSBC",
    "sampath bank": "Sampath Bank",
}

# Corporate suffixes the LLM appends inconsistently — the same bank arrives as
# "DFCC Bank", "DFCC Bank PLC" and "DFCC Bank PLC.". Stripping these before the
# lookup beats enumerating every combination, which is how "Commercial Bank of
# Ceylon PLC" slipped through and became its own bank.
BANK_NAME_SUFFIXES = ("plc", "ltd", "limited", "pvt", "private", "sri lanka", "lanka")

# Bank short codes for ID generation
BANK_CODES = {
    "People's Bank": "pb",
    "Bank of Ceylon": "boc",
    "Commercial Bank": "cb",
    "DFCC Bank": "dfcc",
    "HSBC": "hsbc",
    "Sampath Bank": "sp",
}


def normalize_bank(raw: str) -> str:
    """Map messy LLM-extracted bank names to canonical form.

    An unmapped name is returned as-is, which means it lands on the site as a
    brand new bank with the "unk" ID prefix — so a miss here is visible rather
    than silent.
    """
    key = " ".join(raw.strip().lower().split()).strip(".,")
    if key in BANK_NAME_MAP:
        return BANK_NAME_MAP[key]

    # Peel corporate suffixes off the end, repeatedly: "dfcc bank plc." and
    # "commercial bank of ceylon plc" both reduce to a mapped key.
    changed = True
    while changed:
        changed = False
        for suffix in BANK_NAME_SUFFIXES:
            if key.endswith(" " + suffix):
                key = key[: -len(suffix) - 1].strip().strip(".,")
                changed = True
        if key in BANK_NAME_MAP:
            return BANK_NAME_MAP[key]

    return BANK_NAME_MAP.get(key, raw.strip())


def normalize_card_types(raw) -> list[str]:
    """Normalize card_types JSON to clean list."""
    if not raw:
        return ["Credit Card"]
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return [raw]
    result = []
    for ct in raw:
        ct = ct.strip()
        if not ct:
            continue
        # Normalize common variations
        ct_lower = ct.lower()
        if "credit" in ct_lower and "debit" not in ct_lower:
            result.append("Credit Card")
        elif "debit" in ct_lower:
            result.append("Debit Card")
        elif "platinum" in ct_lower:
            result.append("Platinum Card")
        elif "gold" in ct_lower:
            result.append("Gold Card")
        elif "visa" in ct_lower:
            result.append("Visa Card")
        elif "master" in ct_lower:
            result.append("Mastercard")
        else:
            result.append(ct)
    return list(dict.fromkeys(result)) or ["Credit Card"]  # Deduplicate, preserve order


def determine_offer_type(row: dict) -> dict:
    """Build offer_details from discount fields."""
    pct = row.get("discount_percentage")
    amt = row.get("discount_amount")
    max_lkr = row.get("max_discount_lkr")

    if pct and float(pct) > 0:
        return {
            "type": "percentage",
            "value": float(pct),
            "max_discount_lkr": float(max_lkr) if max_lkr else None,
        }
    elif amt and float(amt) > 0:
        return {
            "type": "fixed",
            "value": float(amt),
            "max_discount_lkr": float(max_lkr) if max_lkr else None,
        }
    else:
        return {
            "type": "percentage",
            "value": None,
            "max_discount_lkr": float(max_lkr) if max_lkr else None,
        }


def row_to_offerspot(row: dict, offer_id: str) -> dict:
    """Convert a Postgres row dict to offerspot JSON format."""
    bank = normalize_bank(row["bank_name"])
    card_types = normalize_card_types(row.get("card_types"))
    offer_details = determine_offer_type(row)

    valid_days = row.get("valid_days")
    if isinstance(valid_days, str):
        try:
            valid_days = json.loads(valid_days)
        except (json.JSONDecodeError, TypeError):
            valid_days = None

    return {
        "id": offer_id,
        "bank": bank,
        "card_types": card_types,
        "category": (row.get("category") or "Other").strip().strip("'\""),
        "merchant": {
            "name": row.get("merchant_name") or None,
            "logo_url": row.get("merchant_logo_url") or None,
        },
        "title": (row.get("promotion_title") or "").strip(),
        "description": (row.get("description") or "").strip(),
        "offer_details": offer_details,
        "validity": {
            "start_date": row.get("valid_from"),
            "end_date": row.get("valid_until"),
            "days": valid_days if valid_days else None,
        },
        "location": {
            "address": None,
            "latitude": None,
            "longitude": None,
        },
        "terms": (row.get("terms_and_conditions") or "").strip() or None,
        "source_url": row.get("source_url", ""),
    }


def fetch_deals_via_docker() -> list[dict]:
    """Fetch deals by running psql inside the Docker container."""
    query = (
        "COPY (SELECT row_to_json(t) FROM "
        "(SELECT * FROM credit_card_deals ORDER BY id) t) TO STDOUT"
    )
    result = subprocess.run(
        ["docker", "compose", "exec", "-T", "postgres",
         "psql", "-U", "crawler", "-d", "crawler", "-c", query],
        capture_output=True, text=True,
        # compose must run from the repo root to find docker-compose.yml.
        cwd=Path(__file__).resolve().parents[1],
    )
    if result.returncode != 0:
        print(f"Error: {result.stderr}", file=sys.stderr)
        sys.exit(1)

    rows = []
    for line in result.stdout.strip().split("\n"):
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def main():
    rows = fetch_deals_via_docker()
    print(f"Fetched {len(rows)} deals from Postgres", file=sys.stderr)

    # Track per-bank counters for ID generation
    bank_counters: Counter = Counter()
    offers = []
    skipped = 0

    for row in rows:
        bank = normalize_bank(row["bank_name"])
        title = (row.get("promotion_title") or "").strip()

        # Skip empty/garbage entries
        if not title or len(title) < 5:
            skipped += 1
            continue

        code = BANK_CODES.get(bank, "unk")
        bank_counters[code] += 1
        offer_id = f"po-{code}-{bank_counters[code]:04d}"

        offer = row_to_offerspot(row, offer_id)
        offers.append(offer)

    print(f"Converted {len(offers)} offers, skipped {skipped}", file=sys.stderr)

    # Bank breakdown
    bank_counts = Counter(o["bank"] for o in offers)
    for bank, count in bank_counts.most_common():
        print(f"  {bank}: {count}", file=sys.stderr)

    json.dump(offers, sys.stdout, indent=2, ensure_ascii=False)
    print(file=sys.stdout)  # trailing newline


if __name__ == "__main__":
    main()
