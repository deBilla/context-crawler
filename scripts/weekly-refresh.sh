#!/bin/bash
#
# Weekly: crawl the bank sites, merge into offerspot, publish.
#
#   ./scripts/weekly-refresh.sh              full run
#   ./scripts/weekly-refresh.sh --dry-run    crawl and merge, but do not push
#   ./scripts/weekly-refresh.sh --no-crawl   re-merge from the existing cache
#
# Credentials come from .env (git-ignored). Intended for cron on an always-on
# host; every failure path leaves the site exactly as it was.

set -uo pipefail
cd "$(dirname "$0")/.."
CRAWLER="$PWD"
SITE="${OFFERSPOT_DIR:-$HOME/offerspot}"
DATA="$SITE/src/app/api/data.json"
LOG_DIR="$CRAWLER/data"
LOG="$LOG_DIR/weekly.log"

# This job pushes to production with nobody watching, so it refuses to publish
# anything that looks like a collapse. A crawl that returns almost nothing —
# a bank restructuring its site, a network fault, an LLM outage — must leave
# the previous data in place rather than replace it with a shell of a site.
# Losing a week of freshness is recoverable; publishing an empty catalogue and
# having Google recrawl it is not.
MIN_OFFERS=800          # floor on total offers in data.json after the merge
MIN_LIVE=150            # floor on offers that have not expired
MAX_SHRINK_PCT=10       # refuse if the catalogue shrinks by more than this

mkdir -p "$LOG_DIR"
log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG"; }
die() { log "FAILED: $*"; exit 1; }

DO_CRAWL=1
DO_PUSH=1
for arg in "$@"; do
  case "$arg" in
    --dry-run)  DO_PUSH=0 ;;
    --no-crawl) DO_CRAWL=0 ;;
    *) die "unknown option: $arg" ;;
  esac
done

[ -f .env ] && set -a && . ./.env && set +a
export PATH="$HOME/.local/bin:$PATH"   # cron's PATH omits the claude CLI

log "=== run start ==="
[ -d "$SITE" ] || die "offerspot checkout not found at $SITE"

count_offers() { python3 -c "
import json,sys
print(len(json.load(open('$DATA'))))
" 2>/dev/null || echo 0; }

count_live() { python3 -c "
import json,datetime
today=datetime.date.today()
n=0
for o in json.load(open('$DATA')):
    e=(o.get('validity') or {}).get('end_date')
    if not e: continue
    try:
        if datetime.date.fromisoformat(e[:10])>=today: n+=1
    except ValueError: pass
print(n)
" 2>/dev/null || echo 0; }

BEFORE=$(count_offers)
log "before: $BEFORE offers, $(count_live) live"
[ "$BEFORE" -gt 0 ] || die "could not read $DATA"

# --- 1. sync both repos ----------------------------------------------------
git -C "$CRAWLER" pull -q --ff-only 2>&1 | tee -a "$LOG"
git -C "$SITE" pull -q --ff-only 2>&1 | tee -a "$LOG" || die "site pull failed — resolve by hand"

# A dirty checkout means a previous run left something behind; committing on
# top would mix it into this week's diff.
[ -z "$(git -C "$SITE" status --porcelain)" ] || die "site checkout is dirty, refusing to run"

# --- 2. crawl --------------------------------------------------------------
# A failed crawl is not a failed run: the merge below is a no-op if nothing new
# arrived, and the cache still holds everything from previous weeks.
if [ "$DO_CRAWL" = "1" ]; then
  log "crawling..."
  .venv/bin/python scripts/refresh.py --max-depth 1 --max-pages-per-bank 25 >> "$LOG" 2>&1 \
    && log "crawl ok" || log "crawl returned non-zero — merging whatever the cache holds"
fi

# --- 3. merge --------------------------------------------------------------
log "merging..."
.venv/bin/python scripts/refresh.py --offline --merge "$DATA" >> "$LOG" 2>&1 \
  || die "merge failed"

AFTER=$(count_offers)
LIVE=$(count_live)
log "after: $AFTER offers, $LIVE live"

# --- 4. sanity floors ------------------------------------------------------
[ "$AFTER" -ge "$MIN_OFFERS" ] || die "only $AFTER offers (floor $MIN_OFFERS) — not publishing"
[ "$LIVE" -ge "$MIN_LIVE" ] || die "only $LIVE live offers (floor $MIN_LIVE) — not publishing"

SHRINK=$(( (BEFORE - AFTER) * 100 / (BEFORE > 0 ? BEFORE : 1) ))
[ "$SHRINK" -le "$MAX_SHRINK_PCT" ] || die "catalogue shrank ${SHRINK}% (max ${MAX_SHRINK_PCT}%) — not publishing"

if ! python3 -c "import json; json.load(open('$DATA'))" 2>/dev/null; then
  die "$DATA is not valid JSON — not publishing"
fi

# --- 5. publish ------------------------------------------------------------
if [ -z "$(git -C "$SITE" status --porcelain)" ]; then
  log "no change to publish"
  log "=== run end ==="
  exit 0
fi

if [ "$DO_PUSH" = "0" ]; then
  log "dry run — leaving the change uncommitted"
  git -C "$SITE" --no-pager diff --stat | tee -a "$LOG"
  log "=== run end ==="
  exit 0
fi

git -C "$SITE" add src/app/api/data.json
git -C "$SITE" -c user.name="offerspot-crawler" -c user.email="crawler@cardpromotions.org" \
  commit -q -m "Refresh offers: $LIVE live of $AFTER total

Automated weekly crawl from the bank promotion pages.
Previous: $BEFORE total. See context-crawler/data/weekly.log on the host." \
  || die "commit failed"

git -C "$SITE" push -q github-offerspot main 2>&1 | tee -a "$LOG" || die "push failed"
log "published: $LIVE live of $AFTER total"
log "=== run end ==="
