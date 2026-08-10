# CRD Promo Crawler

Sri Lankan credit card promotion crawler — a multi-service pipeline that crawls bank websites, extracts structured deal data via LLM, and serves it through a REST API with a web dashboard.

## Architecture

Async producer-consumer pipeline with 5 microservices communicating via Redis queues:

```
seed.py → [frontier queue] → crawler → [parsing queue] → parser → [extraction queue] → extractor → Postgres
                                ↓                           ↓                                         ↓
                              MinIO                    Puppeteer                                   API → Dashboard
```

## Services

| Service | Path | Purpose |
|---------|------|---------|
| crawler | `services/crawler/` | Fetches bank pages (HTTP + Puppeteer for JS sites), stores HTML in MinIO |
| parser | `services/parser/` | Parses HTML, extracts links, LLM relevance check, routes to extraction |
| extractor | `services/extractor/` | LLM-powered structured deal extraction, stores in Postgres |
| api | `services/api/` | FastAPI REST API + web dashboard at `/` |
| puppeteer | `services/puppeteer/` | Headless Chrome sidecar for JS-rendered pages |

## Shared Package

`shared/shared/` — Pydantic models, Redis queue abstraction, URL dedup, DB models, LLM client, MinIO utils.

## Running

```bash
# Start infrastructure + all services
docker compose up -d

# Seed the frontier queue (run from host — hits Docker Redis)
docker compose exec crawler python -c "..." # or use scripts/seed.py against Docker Redis

# Monitor
curl http://localhost:8000/deals/stats
open http://localhost:8000/  # Web dashboard
```

## Key Commands

```bash
# Rebuild after code changes
docker compose build crawler parser extractor api

# View logs
docker compose logs -f crawler parser extractor

# Run alembic migrations
cd migrations && alembic -c alembic.ini upgrade head

# Seed frontier (host Redis — note: Docker has separate Redis)
python3 scripts/seed.py --clear
```

## Configuration

- `.env` — Postgres, MinIO, LLM provider/model settings
- `config/banks.json` — Bank seed URLs, URL patterns, CSS selectors, crawl strategies
- Each service reads config via pydantic-settings from env vars

## LLM Configuration

Set via env vars:
- `LLM_PROVIDER`: `claude_code` | `ollama` | `openai` | `anthropic`
- `LLM_MODEL`: model name (e.g. `claude-sonnet-5`, `qwen2.5-coder:14b`)
- `LLM_BASE_URL`: provider endpoint (ollama/openai only)
- `LLM_API_KEY`: API key (openai/anthropic only)
- `CLAUDE_BIN`: path to the `claude` CLI (claude_code only, default `claude`)

`claude_code` runs prompts through the local `claude` CLI, billing against a
Pro/Max subscription instead of a metered key. Pick a provider by workload:

| Workload | Provider | Why |
|----------|----------|-----|
| Nightly incremental refresh | `claude_code` | Tens of changed pages; no API key, no per-token cost |
| Initial full backfill | `anthropic` | Thousands of pages — subscription rate limits and ~3.5s/call cold start make the CLI the wrong tool |
| Offline / no credentials | `ollama` | Free, but weak at structured extraction |

Two gotchas if you touch `llm_client.py`:
- Callers disagree on JSON shape — `relevance.py` wants an object, `extract.py`
  wants an array. Do not force one via assistant prefill or a `json_object`
  response format; `complete_json()` tolerates both plus fences and prose.
- The OpenAI client sends `response_format: {"type": "json_object"}`, which
  cannot return the array `extract.py` asks for. That path is untested.

## Testing

```bash
python3 -m pytest services/*/tests shared/tests -m "not integration and not e2e"
```

## Important Notes

- Docker Redis (`redis://redis:6379/0`) and host Redis (`redis://localhost:6379`) may be different instances if a local Redis is running. Seed must target the correct one.
- `asyncio_mode = auto` in pytest — no `@pytest.mark.asyncio` needed.
- Relative imports used in `shared/shared/` package (not absolute `shared.models`).
- Bank URL patterns in `config/banks.json` are enforced by the parser's link filter.
- Crawler has per-domain URL ceiling (500), content-hash dedup, URL length/depth limits for trap prevention.
