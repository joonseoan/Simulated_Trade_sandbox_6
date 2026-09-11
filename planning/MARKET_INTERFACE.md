# Market Data Interface — Unified Design

_The abstraction that lets the rest of the backend consume live prices without knowing whether they come from the Massive API or the built-in simulator. Companion documents: `MASSIVE_API.md` (the real feed) and `MARKET_SIMULATOR.md` (the fake feed)._

---

## 1. Goals

1. **One interface, two implementations.** `MassiveProvider` and `SimulatedProvider` are interchangeable. Selection is made once, at startup, from `MASSIVE_API_KEY`.
2. **Everything downstream is source-agnostic.** The price cache, the SSE broadcaster, ticker validation, and `/api/health` never branch on the source (except to *report* it).
3. **Process-global cache.** A single background task writes prices; every SSE connection and every API read shares the same in-memory cache with zero coordination.
4. **Matches the PLAN contract exactly** — `get_prices(tickers) -> dict[ticker, PriceTick]`, a `start()` background loop, the SSE event shape in PLAN §6, `400 unknown_ticker` on bad watchlist adds, held tickers stay "known" regardless of watchlist membership.

---

## 2. Layers

```
┌──────────────────────────────────────────────────────────────────────┐
│  FastAPI app                                                          │
│                                                                      │
│   GET /api/stream/prices ──subscribe──┐                              │
│   GET /api/portfolio ─────read────┐    │                              │
│   POST /api/watchlist ──validate─┐│    │                              │
│                                  ││    │                              │
│                          ┌───────▼▼────▼─────────┐                    │
│                          │   PriceStreamHub      │  ~500ms broadcast  │
│                          │  (direction, fan-out) │  loop              │
│                          └───────────┬───────────┘                    │
│                                      │ reads                          │
│                          ┌───────────▼───────────┐                    │
│                          │      PriceCache       │  process-global    │
│                          │  {ticker: CacheEntry} │                    │
│                          └───────────▲───────────┘                    │
│                                      │ writes                         │
│                          ┌───────────┴───────────┐                    │
│                          │   MarketDataProvider  │  poll loop         │
│                          │  Massive  |  Simulator│  (interval varies) │
│                          └───────────────────────┘                    │
└──────────────────────────────────────────────────────────────────────┘
```

- **Provider** — knows how to get raw prices for a set of tickers. Owns its own poll cadence. Writes latest price + timestamp into the cache.
- **PriceCache** — dumb store. Latest price, previous *emitted* price, last-update time, per ticker. No async, no I/O.
- **PriceStreamHub** — the single ~500 ms loop that reads the cache, computes `direction` versus the last value it emitted, builds the JSON event, and fans it out to every subscriber queue. Owning "previous emitted price" here (not in the provider) is what makes `direction` correct regardless of how slowly the provider polls.
- **App** — SSE endpoint subscribes to the hub; REST endpoints read the cache directly.

---

## 3. Core types

```python
# backend/market/types.py
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, Protocol

Direction = Literal["up", "down", "flat"]


@dataclass(frozen=True, slots=True)
class PriceTick:
    """One observation of a ticker's price from a market data source."""
    ticker: str
    price: float
    timestamp: datetime          # timezone-aware UTC

    def iso_timestamp(self) -> str:
        return self.timestamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(slots=True)
class CacheEntry:
    price: float
    previous_price: float        # value the hub emitted on its previous tick
    updated_at: datetime
    source_time: datetime        # timestamp reported by the source itself


@dataclass(frozen=True, slots=True)
class PriceEvent:
    """What the SSE endpoint serializes and sends to the browser (PLAN §6)."""
    ticker: str
    price: float
    previous_price: float
    timestamp: str               # ISO-8601 UTC, 'Z' suffix
    direction: Direction

    def to_sse(self) -> str:
        import json
        return f"data: {json.dumps(self.__dict__)}\n\n"
```

---

## 4. The provider interface

```python
# backend/market/base.py
from __future__ import annotations

import abc

from .types import PriceTick


class MarketDataProvider(abc.ABC):
    """Abstract market data source. Implemented by SimulatedProvider and MassiveProvider."""

    #: Human-readable source name reported by /api/health ("simulator" | "massive").
    source: str

    #: Seconds between polls of the upstream source.
    poll_interval: float

    @abc.abstractmethod
    async def get_prices(self, tickers: list[str]) -> dict[str, PriceTick]:
        """Return the latest price for each requested ticker.

        - Returns only tickers it could resolve. A missing ticker means
          "no data this cycle" — callers keep the previous cached value.
        - Must not raise for a partially-bad ticker list; log and skip.
        - Idempotent and side-effect free apart from any internal upstream cache.
        """

    @abc.abstractmethod
    async def validate_ticker(self, ticker: str) -> bool:
        """True if `ticker` is a real, resolvable symbol. Used by POST /api/watchlist."""

    async def start(self) -> None:
        """Optional: warm up (seed simulator state / prime upstream). Default no-op."""

    async def aclose(self) -> None:
        """Optional: release resources (close the HTTP client). Default no-op."""
```

`get_prices` + `validate_ticker` are the only two methods callers need. `start`/`aclose` are lifecycle hooks driven by the FastAPI lifespan.

Both implementations are covered by the same test (PLAN §12): *"both implementations satisfy the abstract interface"* — a parametrized pytest that runs the identical assertions against each.

---

## 5. The price cache

```python
# backend/market/cache.py
from __future__ import annotations

import threading
from datetime import datetime, timezone

from .types import CacheEntry, PriceTick


class PriceCache:
    """Process-global latest-price store. Plain dict + lock; no async, no I/O."""

    def __init__(self) -> None:
        self._entries: dict[str, CacheEntry] = {}
        self._lock = threading.Lock()

    def known_tickers(self) -> set[str]:
        with self._lock:
            return set(self._entries)

    def has(self, ticker: str) -> bool:
        with self._lock:
            return ticker in self._entries

    def ingest(self, tick: PriceTick) -> None:
        """Write a fresh observation from the provider. Preserves previous_price."""
        with self._lock:
            prev = self._entries.get(tick.ticker)
            self._entries[tick.ticker] = CacheEntry(
                price=tick.price,
                previous_price=prev.previous_price if prev else tick.price,
                updated_at=datetime.now(timezone.utc),
                source_time=tick.timestamp,
            )

    def seed_missing(self, ticker: str, price: float) -> None:
        """Register a ticker we must keep streaming (e.g. a held position) even if
        the provider hasn't returned it yet."""
        with self._lock:
            self._entries.setdefault(
                ticker,
                CacheEntry(price, price, datetime.now(timezone.utc), datetime.now(timezone.utc)),
            )

    def mark_emitted(self, ticker: str, emitted_price: float) -> None:
        """Called by the hub after it sends a tick, so the next diff is vs this value."""
        with self._lock:
            e = self._entries.get(ticker)
            if e:
                e.previous_price = emitted_price

    def snapshot(self) -> dict[str, CacheEntry]:
        with self._lock:
            return {k: CacheEntry(v.price, v.previous_price, v.updated_at, v.source_time)
                    for k, v in self._entries.items()}

    def latest_price(self, ticker: str) -> float | None:
        with self._lock:
            e = self._entries.get(ticker)
            return e.price if e else None

    def newest_update_age_ms(self) -> float | None:
        """Age of the most recently updated entry — feeds /api/health."""
        with self._lock:
            if not self._entries:
                return None
            newest = max(e.updated_at for e in self._entries.values())
        return (datetime.now(timezone.utc) - newest).total_seconds() * 1000
```

A `threading.Lock` (not an async lock) keeps the cache usable from sync code paths and is uncontended in practice — critical sections are dict operations.

---

## 6. The stream hub

```python
# backend/market/hub.py
from __future__ import annotations

import asyncio
from datetime import timezone

from .cache import PriceCache
from .types import Direction, PriceEvent

BROADCAST_INTERVAL = 0.5   # seconds — SSE cadence, independent of provider poll rate
FLAT_EPSILON = 1e-9


def _direction(prev: float, cur: float) -> Direction:
    if cur > prev + FLAT_EPSILON:
        return "up"
    if cur < prev - FLAT_EPSILON:
        return "down"
    return "flat"


class PriceStreamHub:
    def __init__(self, cache: PriceCache) -> None:
        self._cache = cache
        self._subscribers: set[asyncio.Queue[PriceEvent]] = set()
        self._task: asyncio.Task | None = None

    # -- lifecycle -----------------------------------------------------------
    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="price-stream-hub")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    # -- subscription ------------------------------------------------------
    def subscribe(self) -> asyncio.Queue[PriceEvent]:
        q: asyncio.Queue[PriceEvent] = asyncio.Queue(maxsize=1000)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[PriceEvent]) -> None:
        self._subscribers.discard(q)

    # -- broadcast loop --------------------------------------------------
    async def _run(self) -> None:
        while True:
            await asyncio.sleep(BROADCAST_INTERVAL)
            events: list[PriceEvent] = []
            for ticker, entry in self._cache.snapshot().items():
                direction = _direction(entry.previous_price, entry.price)
                events.append(PriceEvent(
                    ticker=ticker,
                    price=round(entry.price, 4),
                    previous_price=round(entry.previous_price, 4),
                    timestamp=entry.source_time.astimezone(timezone.utc)
                              .isoformat().replace("+00:00", "Z"),
                    direction=direction,
                ))
                self._cache.mark_emitted(ticker, entry.price)

            if not events:
                continue
            dead: list[asyncio.Queue] = []
            for q in self._subscribers:
                for ev in events:
                    try:
                        q.put_nowait(ev)
                    except asyncio.QueueFull:
                        dead.append(q)
                        break
            for q in dead:
                self._subscribers.discard(q)
```

Notes:

- The hub re-emits **every** known ticker on every tick — even unchanged ones get `direction: "flat"` (PLAN §6). First paint is therefore correct after one tick.
- `previous_price` in the event is "the value emitted on the prior tick", which is exactly what `mark_emitted` records.
- A slow consumer whose queue fills is dropped; `EventSource` reconnects and re-primes from the next tick.

---

## 7. The poll loop (provider → cache)

```python
# backend/market/poller.py
from __future__ import annotations

import asyncio
import logging

from .base import MarketDataProvider
from .cache import PriceCache

log = logging.getLogger("market.poller")


class MarketPoller:
    """Drives provider.get_prices() on an interval and writes results into the cache."""

    def __init__(
        self,
        provider: MarketDataProvider,
        cache: PriceCache,
        tickers_supplier,          # () -> list[str]: current watchlist ∪ held positions
    ) -> None:
        self._provider = provider
        self._cache = cache
        self._tickers_supplier = tickers_supplier
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        await self._provider.start()
        # Prime the cache once synchronously so the first SSE tick has data.
        await self._poll_once()
        self._task = asyncio.create_task(self._run(), name="market-poller")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self._provider.aclose()

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._provider.poll_interval)
            try:
                await self._poll_once()
            except Exception:                       # never let the loop die
                log.exception("poll cycle failed; keeping last cached prices")

    async def _poll_once(self) -> None:
        tickers = sorted(set(self._tickers_supplier()) | self._cache.known_tickers())
        if not tickers:
            return
        prices = await self._provider.get_prices(tickers)
        for tick in prices.values():
            self._cache.ingest(tick)
```

- `tickers_supplier` unions the live watchlist with anything already in the cache, so a ticker removed from the watchlist but still held keeps streaming (PLAN §8 "Watchlist edge cases").
- A failed cycle is logged and swallowed; the cache keeps serving stale prices and `/api/health` reports the staleness.

---

## 8. Provider selection

```python
# backend/market/factory.py
from __future__ import annotations

import os

from .base import MarketDataProvider
from .massive import MassiveProvider
from .simulator import SimulatedProvider

# Poll cadence by situation (PLAN §6). Massive tier is not machine-detectable,
# so we use the conservative free-tier interval unless overridden in code.
_MASSIVE_POLL_SECONDS = 15.0
_SIMULATOR_POLL_SECONDS = 0.5


def create_provider() -> MarketDataProvider:
    key = os.environ.get("MASSIVE_API_KEY", "").strip()
    if key:
        return MassiveProvider(api_key=key, poll_interval=_MASSIVE_POLL_SECONDS)
    return SimulatedProvider(poll_interval=_SIMULATOR_POLL_SECONDS)
```

Selection happens exactly once, in the FastAPI lifespan. There is no runtime switching.

---

## 9. Wiring into FastAPI

```python
# backend/app.py  (excerpt)
from contextlib import asynccontextmanager

from fastapi import FastAPI

from market.cache import PriceCache
from market.factory import create_provider
from market.hub import PriceStreamHub
from market.poller import MarketPoller


@asynccontextmanager
async def lifespan(app: FastAPI):
    cache = PriceCache()
    provider = create_provider()
    poller = MarketPoller(provider, cache, tickers_supplier=lambda: watchlist_repo.symbols())
    hub = PriceStreamHub(cache)

    # Held positions must stream even if not on the watchlist.
    for sym in positions_repo.symbols():
        cache.seed_missing(sym, seed_price_for(sym))

    await poller.start()
    await hub.start()

    app.state.cache = cache
    app.state.provider = provider
    app.state.hub = hub
    try:
        yield
    finally:
        await hub.stop()
        await poller.stop()


app = FastAPI(lifespan=lifespan)
```

### SSE endpoint

```python
# backend/routes/stream.py
import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

router = APIRouter()


@router.get("/api/stream/prices")
async def stream_prices(request: Request):
    hub = request.app.state.hub
    queue = hub.subscribe()

    async def gen():
        try:
            # Immediately flush the current cache so a fresh client isn't blank
            # for up to 500ms.
            for ticker, entry in request.app.state.cache.snapshot().items():
                yield _event_from_entry(ticker, entry)
            while True:
                if await request.is_disconnected():
                    break
                try:
                    ev = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield ev.to_sse()
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"          # comment frame keeps the pipe open
        finally:
            hub.unsubscribe(queue)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )
```

### Ticker validation on watchlist add

```python
# backend/routes/watchlist.py  (excerpt)
@router.post("/api/watchlist")
async def add_ticker(body: AddTicker, request: Request):
    symbol = body.ticker.strip().upper()
    if not TICKER_RE.match(symbol):
        raise HTTPException(400, {"error": {"code": "invalid_ticker", "message": "..."}})
    if watchlist_repo.count() >= 30:
        raise HTTPException(400, {"error": {"code": "watchlist_full", "message": "..."}})
    if watchlist_repo.has(symbol):
        return watchlist_repo.get(symbol)          # 200 no-op

    provider = request.app.state.provider
    if not await provider.validate_ticker(symbol):
        raise HTTPException(400, {"error": {"code": "unknown_ticker", "message": f"{symbol} not found"}})

    row = watchlist_repo.add(symbol)
    request.app.state.cache.seed_missing(symbol, await _first_price(provider, symbol))
    return row
```

`SimulatedProvider.validate_ticker` returns `True` for anything matching the ticker regex (every symbol is synthesizable). `MassiveProvider.validate_ticker` calls the single-ticker snapshot / prev-bar (see `MASSIVE_API.md` §4).

### Health

```python
@router.get("/api/health")
async def health(request: Request):
    cache = request.app.state.cache
    provider = request.app.state.provider
    age = cache.newest_update_age_ms()
    db_ok = check_db()
    feed_ok = age is not None and age < 10_000
    status = 200 if (db_ok and feed_ok) else 503
    return JSONResponse(
        status_code=status,
        content={
            "status": "ok" if status == 200 else "degraded",
            "db": "ok" if db_ok else "error",
            "market_feed": {"source": provider.source, "last_update_age_ms": round(age or -1)},
        },
    )
```

---

## 10. Module layout

```
backend/market/
├── __init__.py        # re-exports create_provider, PriceCache, PriceStreamHub
├── types.py           # PriceTick, CacheEntry, PriceEvent, Direction
├── base.py            # MarketDataProvider ABC
├── cache.py           # PriceCache
├── hub.py             # PriceStreamHub (500ms broadcast, direction, fan-out)
├── poller.py          # MarketPoller (provider -> cache loop)
├── factory.py         # create_provider() — env-driven selection
├── simulator.py       # SimulatedProvider  (see MARKET_SIMULATOR.md)
└── massive.py         # MassiveProvider    (see MASSIVE_API.md)
```

---

## 11. Test surface (PLAN §12)

| Test | Target |
|---|---|
| `get_prices` returns a `PriceTick` per resolvable ticker, skips unknowns | both providers (parametrized) |
| `start()` primes the cache; poller writes on each cycle | `MarketPoller` |
| `direction` is `up`/`down`/`flat` vs the previously emitted value | `PriceStreamHub._direction` + loop |
| unchanged price still re-emits with `direction: "flat"` | hub loop |
| held-but-unwatched ticker stays in `known_tickers()` and keeps streaming | `MarketPoller._poll_once` |
| provider exception in a poll cycle doesn't kill the loop; cache keeps serving | `MarketPoller._run` |
| `create_provider()` picks Massive iff `MASSIVE_API_KEY` non-empty | `factory` |
| `newest_update_age_ms()` drives `/api/health` 503 past 10 s | `PriceCache` + health route |
| Massive response parsing → `PriceTick` (fixture JSON) | `MassiveProvider` |
| Simulator GBM math, deterministic novel-ticker seed | `SimulatedProvider` (see `MARKET_SIMULATOR.md`) |

Because both providers implement one ABC, the interface-conformance tests are written once and run against each via `pytest.mark.parametrize`.
