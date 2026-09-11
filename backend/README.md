# Simulated Trading — Backend

FastAPI (Python/`uv`) backend for Simulated Trading. See `../planning/PLAN.md` for the
full project spec.

## Market data package (`market/`)

Implements `planning/MARKET_DATA_DESIGN.md`: a unified `MarketDataProvider` interface with
two interchangeable implementations — an in-process GBM simulator (`market/simulator.py`,
default) and a Massive/Polygon.io REST client (`market/massive.py`, used when
`MASSIVE_API_KEY` is set) — plus the process-global price cache, SSE broadcast hub, and
poll loop that sit between them and the rest of the app (`market/cache.py`, `market/hub.py`,
`market/poller.py`). `market/factory.py` selects the provider at startup.

REST routes, the database layer, and LLM chat integration are not part of this package and
are tracked separately.

## Setup

```bash
cd backend
uv sync
```

## Tests

```bash
cd backend
uv run pytest -q
```
