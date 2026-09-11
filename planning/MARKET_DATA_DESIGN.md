# Market Data Backend — Implementation Design

_A single, implementation-ready design for the whole `backend/market/` package: the unified provider interface, the process-global cache and SSE broadcast hub, the GBM simulator, and the Massive (Polygon.io) REST provider. This document consolidates and completes `MARKET_INTERFACE.md`, `MARKET_SIMULATOR.md`, and `MASSIVE_API.md` into buildable code — read those three for background/rationale; read this one to write the code._

---

## 1. Scope & contract

From `planning/PLAN.md` §6–§8 and Appendix A (D12, D13):

- One abstract interface, two interchangeable implementations, selected once at startup from `MASSIVE_API_KEY` (empty/absent → simulator).
- A single process-global in-memory price cache; one background poll loop writes to it; the SSE hub reads it on its own ~500 ms cadence and fans out `{ticker, price, previous_price, timestamp, direction}` JSON events to every connected client.
- `POST /api/watchlist` validates new tickers through the active provider; unknown → `400 unknown_ticker`.
- A ticker removed from the watchlist but still held in a position must keep streaming — "known" tickers are `watchlist ∪ positions ∪ cache`, not just the watchlist.
- `GET /api/health` reports `{source, last_update_age_ms}` and goes `503` past 10s of staleness.
- No historical backfill of any kind (Appendix A, D2) — the provider only ever answers "what's the price **right now**".

---

## 2. Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│  FastAPI app (backend/app.py)                                        │
│                                                                        │
│   GET /api/stream/prices ──subscribe──┐                              │
│   GET /api/portfolio ─────read────┐    │                              │
│   POST /api/watchlist ──validate─┐│    │                              │
│                                  ││    │                              │
│                          ┌───────▼▼────▼─────────┐                    │
│                          │   PriceStreamHub      │  500ms broadcast   │
│                          │  (direction, fan-out) │  loop, own cadence  │
│                          └───────────┬───────────┘                    │
│                                      │ reads                          │
│                          ┌───────────▼───────────┐                    │
│                          │      PriceCache       │  process-global,   │
│                          │  {ticker: CacheEntry} │  threading.Lock    │
│                          └───────────▲───────────┘                    │
│                                      │ writes                         │
│                          ┌───────────┴───────────┐                    │
│                          │      MarketPoller     │  drives provider   │
│                          └───────────┬───────────┘  on its own poll   │
│                                      │ calls .get_prices()   interval │
│                          ┌───────────▼───────────┐                    │
│                          │   MarketDataProvider  │  ABC                │
│                          │  SimulatedProvider  |  MassiveProvider     │
│                          └───────────────────────┘                    │
└──────────────────────────────────────────────────────────────────────┘
```

Three independent cadences, deliberately decoupled:

| Loop | Interval | Why decoupled |
|---|---|---|
| Provider poll (`MarketPoller`) | 0.5s (simulator) / 15s (Massive free tier) | Upstream rate limits vary wildly; the simulator has none |
| SSE broadcast (`PriceStreamHub`) | fixed 0.5s | Browser experience should feel identical regardless of source; unchanged prices still re-emit as `direction: "flat"` |
| SSE keepalive | 15s idle timeout | Keeps intermediary proxies from closing an idle connection |

Because the hub always re-emits every known ticker every 500 ms — even when Massive hasn't refreshed — the UI is source-agnostic. Only `/api/health`'s `source` field and the poll interval differ.

---

## 3. Module layout

```
backend/market/
├── __init__.py        # re-exports create_provider, PriceCache, PriceStreamHub, MarketPoller
├── types.py            # PriceTick, CacheEntry, PriceEvent, Direction
├── base.py              # MarketDataProvider ABC
├── cache.py             # PriceCache
├── hub.py                # PriceStreamHub
├── poller.py             # MarketPoller
├── factory.py            # create_provider() — env-driven selection
├── simulator.py          # SimulatedProvider (GBM)
└── massive.py            # MassiveProvider (httpx client for api.massive.com)
```

---

## 4. Core types

```python
# backend/market/types.py
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

Direction = Literal["up", "down", "flat"]


def to_iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class PriceTick:
    """One observation of a ticker's price from a market data source."""
    ticker: str
    price: float
    timestamp: datetime          # timezone-aware UTC


@dataclass(slots=True)
class CacheEntry:
    price: float
    previous_price: float        # value the hub last EMITTED (not last ingested)
    updated_at: datetime         # when the cache last received a write
    source_time: datetime        # timestamp reported by the source itself


@dataclass(frozen=True, slots=True)
class PriceEvent:
    """Exactly the JSON shape defined in PLAN.md §6."""
    ticker: str
    price: float
    previous_price: float
    timestamp: str               # ISO-8601 UTC, 'Z' suffix
    direction: Direction

    def to_sse(self) -> str:
        import json
        return f"data: {json.dumps(self.__dict__)}\n\n"
```

`previous_price` lives in two places for two different reasons: `CacheEntry.previous_price` is "what the hub last **sent**", used to compute `direction` on the *next* tick; `PriceEvent.previous_price` is that same value serialized into the event the browser receives. The provider never touches either — it only ever reports the current price.

---

## 5. The unified provider interface

```python
# backend/market/base.py
from __future__ import annotations

import abc

from .types import PriceTick


class MarketDataProvider(abc.ABC):
    """Abstract market data source. SimulatedProvider and MassiveProvider both
    implement this and nothing else in the codebase is allowed to know which
    one is active (except to report `.source` in /api/health)."""

    #: "simulator" | "massive" — reported verbatim by /api/health.
    source: str

    #: Seconds between MarketPoller cycles for this provider.
    poll_interval: float

    @abc.abstractmethod
    async def get_prices(self, tickers: list[str]) -> dict[str, PriceTick]:
        """Return the latest price for each resolvable ticker in `tickers`.

        Contract:
        - Keys of the result are a subset of (uppercased) `tickers`.
        - A ticker absent from the result means "no update this cycle" —
          the caller (MarketPoller) simply keeps the last cached value.
        - Never raises for a bad/unknown symbol in the list; skip it.
        - An upstream-wide failure (network error, 5xx, rate limit) must be
          caught internally and logged; return whatever partial data is
          available, or {} — never propagate the exception.
        """

    @abc.abstractmethod
    async def validate_ticker(self, ticker: str) -> bool:
        """True if `ticker` is a resolvable symbol. Backs POST /api/watchlist."""

    async def start(self) -> None:
        """Optional warm-up hook (seed simulator state / prime an HTTP client).
        Called once by MarketPoller.start(). Default no-op."""

    async def aclose(self) -> None:
        """Optional teardown (close an HTTP client, cancel an internal task).
        Called once by MarketPoller.stop(). Default no-op."""
```

Both concrete providers are exercised by the **same** parametrized conformance test (see §10), so this file is the actual spec, not just documentation of intent.

---

## 6. The price cache

```python
# backend/market/cache.py
from __future__ import annotations

import threading
from datetime import datetime, timezone

from .types import CacheEntry, PriceTick


class PriceCache:
    """Process-global latest-price store. Plain dict + lock — no async, no I/O.
    Safe to read/write from sync or async code on any event loop."""

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
        """Write a fresh observation from the provider. `previous_price` is
        left untouched here — only the hub's mark_emitted() advances it, so
        `direction` reflects the last value a *client saw*, not the last
        value the provider reported."""
        with self._lock:
            prev = self._entries.get(tick.ticker)
            self._entries[tick.ticker] = CacheEntry(
                price=tick.price,
                previous_price=prev.previous_price if prev else tick.price,
                updated_at=datetime.now(timezone.utc),
                source_time=tick.timestamp,
            )

    def seed_missing(self, ticker: str, price: float) -> None:
        """Register a ticker that must stream even though the provider
        hasn't reported it yet this cycle (e.g. a held position, or a
        brand-new watchlist add before the first poll completes)."""
        with self._lock:
            self._entries.setdefault(
                ticker,
                CacheEntry(price, price, datetime.now(timezone.utc), datetime.now(timezone.utc)),
            )

    def mark_emitted(self, ticker: str, emitted_price: float) -> None:
        """Called by PriceStreamHub right after it sends a tick for `ticker`,
        so the *next* diff is computed against the value the client saw."""
        with self._lock:
            e = self._entries.get(ticker)
            if e:
                e.previous_price = emitted_price

    def snapshot(self) -> dict[str, CacheEntry]:
        """A defensive copy — callers iterate this without holding the lock."""
        with self._lock:
            return {
                k: CacheEntry(v.price, v.previous_price, v.updated_at, v.source_time)
                for k, v in self._entries.items()
            }

    def latest_price(self, ticker: str) -> float | None:
        with self._lock:
            e = self._entries.get(ticker)
            return e.price if e else None

    def newest_update_age_ms(self) -> float | None:
        """Age of the freshest entry, in ms. None if the cache is empty
        (startup, before the first poll). Feeds /api/health."""
        with self._lock:
            if not self._entries:
                return None
            newest = max(e.updated_at for e in self._entries.values())
        return (datetime.now(timezone.utc) - newest).total_seconds() * 1000
```

---

## 7. The SSE broadcast hub

```python
# backend/market/hub.py
from __future__ import annotations

import asyncio
from datetime import timezone

from .cache import PriceCache
from .types import Direction, PriceEvent, to_iso_z

BROADCAST_INTERVAL = 0.5   # seconds — fixed SSE cadence, independent of provider poll rate
FLAT_EPSILON = 1e-9
SUBSCRIBER_QUEUE_MAX = 1000


def _direction(prev: float, cur: float) -> Direction:
    if cur > prev + FLAT_EPSILON:
        return "up"
    if cur < prev - FLAT_EPSILON:
        return "down"
    return "flat"


class PriceStreamHub:
    """The single loop that turns cache state into the wire format and fans
    it out to every open SSE connection. Owns 'what was last emitted' so
    `direction` is correct even when the upstream provider polls far slower
    than the 500ms broadcast cadence (Massive free tier: 15s)."""

    def __init__(self, cache: PriceCache) -> None:
        self._cache = cache
        self._subscribers: set[asyncio.Queue[PriceEvent]] = set()
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="price-stream-hub")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def subscribe(self) -> asyncio.Queue[PriceEvent]:
        q: asyncio.Queue[PriceEvent] = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_MAX)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[PriceEvent]) -> None:
        self._subscribers.discard(q)

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(BROADCAST_INTERVAL)
            events = self._build_events()
            if not events:
                continue
            self._fanout(events)

    def _build_events(self) -> list[PriceEvent]:
        events: list[PriceEvent] = []
        for ticker, entry in self._cache.snapshot().items():
            direction = _direction(entry.previous_price, entry.price)
            events.append(PriceEvent(
                ticker=ticker,
                price=round(entry.price, 4),
                previous_price=round(entry.previous_price, 4),
                timestamp=to_iso_z(entry.source_time),
                direction=direction,
            ))
            self._cache.mark_emitted(ticker, entry.price)
        return events

    def _fanout(self, events: list[PriceEvent]) -> None:
        dead: list[asyncio.Queue] = []
        for q in self._subscribers:
            for ev in events:
                try:
                    q.put_nowait(ev)
                except asyncio.QueueFull:
                    dead.append(q)      # slow consumer — drop it, EventSource reconnects
                    break
        for q in dead:
            self._subscribers.discard(q)
```

Every known ticker is re-emitted on every tick — including unchanged ones, as `direction: "flat"` — so first paint after subscribing is correct within one broadcast interval, and a client never has to wait to "see two ticks" to know a price is flat versus stale.

---

## 8. The poll loop (provider → cache)

```python
# backend/market/poller.py
from __future__ import annotations

import asyncio
import logging
from typing import Callable

from .base import MarketDataProvider
from .cache import PriceCache

log = logging.getLogger("market.poller")


class MarketPoller:
    """Drives provider.get_prices() on the provider's own interval and
    writes results into the cache. This is the only code that calls the
    provider — the SSE hub and REST routes only ever touch the cache."""

    def __init__(
        self,
        provider: MarketDataProvider,
        cache: PriceCache,
        tickers_supplier: Callable[[], list[str]],   # () -> watchlist ∪ held positions
    ) -> None:
        self._provider = provider
        self._cache = cache
        self._tickers_supplier = tickers_supplier
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        await self._provider.start()
        await self._poll_once()          # prime synchronously so tick #1 of the SSE hub isn't empty
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
            except Exception:
                log.exception("poll cycle failed; keeping last cached prices")

    async def _poll_once(self) -> None:
        # Union: current watchlist ∪ everything already cached (held positions,
        # tickers dropped from the watchlist but still owned). Never shrinks
        # the set the provider is asked about just because a ticker was
        # unwatched — PLAN §8 "held tickers stay known regardless of
        # watchlist membership".
        tickers = sorted(set(self._tickers_supplier()) | self._cache.known_tickers())
        if not tickers:
            return
        prices = await self._provider.get_prices(tickers)
        for tick in prices.values():
            self._cache.ingest(tick)
```

A failed cycle (network blip, Massive 429/5xx, simulator bug) is caught, logged, and swallowed — the cache keeps serving its last values and `/api/health` surfaces the growing staleness rather than the process crashing or the SSE stream going silent.

---

## 9. Provider selection

```python
# backend/market/factory.py
from __future__ import annotations

import os

from .base import MarketDataProvider
from .massive import MassiveProvider
from .simulator import SimulatedProvider

# Poll cadence by situation (PLAN §6 / MASSIVE_API.md §11). Massive's tier is
# not machine-detectable from the key alone, so the free-tier interval is the
# safe default; override in code/env if a paid tier is confirmed.
MASSIVE_POLL_SECONDS = 15.0
SIMULATOR_POLL_SECONDS = 0.5


def create_provider() -> MarketDataProvider:
    key = os.environ.get("MASSIVE_API_KEY", "").strip()
    if key:
        return MassiveProvider(api_key=key, poll_interval=MASSIVE_POLL_SECONDS)
    return SimulatedProvider(poll_interval=SIMULATOR_POLL_SECONDS)
```

Selection happens exactly once, in the FastAPI lifespan (§13). There is no runtime switching and no code outside this function branches on `MASSIVE_API_KEY`.

---

## 10. The simulator — `SimulatedProvider`

Implements independent per-ticker geometric Brownian motion:

```
S(t + dt) = S(t) · exp( (μ − ½σ²)·dt  +  σ·√dt·Z ),   Z ~ N(0, 1)
```

`dt` is expressed in **trading years** so an annualized `σ` produces sane per-tick moves:

```
SECONDS_PER_TRADING_YEAR = 252 days × 6.5h × 3600s = 5_896_800
dt = 0.5 / 5_896_800 ≈ 8.48e-8
```

At `σ = 0.30`: per-tick log-return stdev `σ·√dt ≈ 2.76e-5` (~0.0028%/tick, ~1%/1.3h session) — visible movement without absurd swings. The simulator runs continuously with no market-hours gating; "trading year" is purely a tick-size scaling choice.

### 10.1 Seed data

```python
# backend/market/simulator.py (part 1 — seed tables)
from __future__ import annotations

import asyncio
import hashlib
import math
import random
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .base import MarketDataProvider
from .types import PriceTick

# ── tunables ──────────────────────────────────────────────────────────────
UPDATE_INTERVAL_SECONDS = 0.5
SECONDS_PER_TRADING_YEAR = 252 * 6.5 * 3600          # 5_896_800
DT_YEARS = UPDATE_INTERVAL_SECONDS / SECONDS_PER_TRADING_YEAR
PRICE_FLOOR = 0.01
PRICE_FLOOR_SEED, PRICE_CEIL_SEED = 15.0, 600.0
TICKER_RE = re.compile(r"^[A-Z][A-Z.\-]{0,9}$")

DEFAULT_DRIFT = 0.0
DEFAULT_VOL = 0.30

# The 10 PLAN §7 defaults, curated for plausibility (exact values don't matter).
SEED_PRICES: dict[str, float] = {
    "AAPL": 190.0,
    "GOOGL": 175.0,
    "MSFT": 415.0,
    "AMZN": 185.0,
    "TSLA": 250.0,
    "NVDA": 120.0,
    "META": 500.0,
    "JPM": 200.0,
    "V": 280.0,
    "NFLX": 650.0,
}

# Optional per-ticker (drift, volatility) overrides; absent tickers use the
# DEFAULT_* constants above. Drift stays 0.0 so P&L is driven by variance,
# not a baked-in trend (PLAN §6 mandates drift 0 for novel tickers; kept
# uniform here for predictable demos).
SEED_PARAMS: dict[str, tuple[float, float]] = {
    "TSLA": (0.0, 0.55),
    "NVDA": (0.0, 0.50),
    "NFLX": (0.0, 0.40),
}


def deterministic_seed_price(symbol: str) -> float:
    """Stable across processes and Python runs — hashlib, never the salted
    built-in hash(). Any ticker not in SEED_PRICES gets this."""
    digest = hashlib.sha256(symbol.upper().encode()).digest()
    frac = int.from_bytes(digest[:8], "big") / 2**64     # uniform in [0, 1)
    return round(PRICE_FLOOR_SEED + frac * (PRICE_CEIL_SEED - PRICE_FLOOR_SEED), 2)


def seed_for(symbol: str) -> tuple[float, float, float]:
    """Return (price, drift, volatility) for any symbol, curated or novel."""
    sym = symbol.upper()
    if sym in SEED_PRICES:
        drift, vol = SEED_PARAMS.get(sym, (DEFAULT_DRIFT, DEFAULT_VOL))
        return SEED_PRICES[sym], drift, vol
    return deterministic_seed_price(sym), DEFAULT_DRIFT, DEFAULT_VOL


def rng_for(symbol: str, master_seed: int | None) -> random.Random:
    """Each ticker owns an independent RNG so series are statistically
    independent and adding/removing a ticker never perturbs another's
    sequence. master_seed=None (production default) uses OS entropy;
    a fixed int makes the whole simulation reproducible for tests."""
    if master_seed is None:
        return random.Random()
    return random.Random(f"{master_seed}:{symbol.upper()}")
```

### 10.2 Per-ticker state and the GBM step

```python
# backend/market/simulator.py (part 2 — ticker state)

@dataclass(slots=True)
class TickerState:
    symbol: str
    price: float
    drift: float
    volatility: float
    rng: random.Random
    prev_price: float = field(init=False)
    updated_at: datetime = field(init=False)

    def __post_init__(self) -> None:
        self.prev_price = self.price
        self.updated_at = datetime.now(timezone.utc)

    def step(self, dt: float = DT_YEARS) -> None:
        z = self.rng.gauss(0.0, 1.0)
        growth = (self.drift - 0.5 * self.volatility ** 2) * dt
        shock = self.volatility * math.sqrt(dt) * z
        self.prev_price = self.price
        self.price = max(self.price * math.exp(growth + shock), PRICE_FLOOR)
        self.updated_at = datetime.now(timezone.utc)
```

### 10.3 The provider

```python
# backend/market/simulator.py (part 3 — SimulatedProvider)

class SimulatedProvider(MarketDataProvider):
    source = "simulator"

    def __init__(self, poll_interval: float = UPDATE_INTERVAL_SECONDS,
                 seed: int | None = None) -> None:
        self.poll_interval = poll_interval
        self._seed = seed
        self._states: dict[str, TickerState] = {}
        self._task: asyncio.Task | None = None

    # -- MarketDataProvider interface ---------------------------------------
    async def start(self) -> None:
        for sym in SEED_PRICES:
            self._ensure(sym)
        self._task = asyncio.create_task(self._run(), name="simulator")

    async def aclose(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def get_prices(self, tickers: list[str]) -> dict[str, PriceTick]:
        out: dict[str, PriceTick] = {}
        for raw in tickers:
            sym = raw.upper()
            if not TICKER_RE.match(sym):
                continue                       # malformed symbol: skip, never raise
            st = self._ensure(sym)             # lazily materialize newly-watched tickers
            out[sym] = PriceTick(sym, round(st.price, 4), st.updated_at)
        return out

    async def validate_ticker(self, ticker: str) -> bool:
        # Every well-formed symbol is synthesizable — the simulator never
        # rejects a syntactically valid ticker.
        return bool(TICKER_RE.match(ticker.upper()))

    # -- internals -----------------------------------------------------------
    def _ensure(self, symbol: str) -> TickerState:
        st = self._states.get(symbol)
        if st is None:
            price, drift, vol = seed_for(symbol)
            st = TickerState(symbol=symbol, price=price, drift=drift,
                              volatility=vol, rng=rng_for(symbol, self._seed))
            self._states[symbol] = st
        return st

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self.poll_interval)
            for sym in list(self._states):     # snapshot keys: concurrent _ensure() is safe
                self._states[sym].step()
```

`get_prices()` reads whatever `TickerState.price` currently is; the internal `_run()` loop is what actually advances GBM on its own 500 ms cadence, independent of `MarketPoller` calling `get_prices()`. This means sparklines keep accumulating realistic motion even if a poll cycle is briefly delayed, and — because the loop runs regardless of subscriber count — the simulator behaves identically whether zero or many browser tabs are connected.

> **Simpler alternative** (documented in `MARKET_SIMULATOR.md` §6): drop the internal `_run()` task and call `st.step()` inside `get_prices()` itself, coupling stepping 1:1 to polling. Easier to reason about in unit tests (one GBM step per assertion), but prices stop moving if nothing polls. Start with the two-loop version above; only simplify if the internal task proves troublesome in tests (see §14 for how to make it deterministic instead).

### 10.4 Stretch: correlation & event shocks (off by default — PLAN D12)

Not required for a passing build. Kept as isolated, flag-gated additions so the core GBM path stays simple to test:

```python
# Correlated moves: give a ticker group a shared market factor each step.
#   Z_i = sqrt(rho)*Z_market + sqrt(1-rho)*Z_i_idiosyncratic
# One extra Z_market draw per group per step; rho=0 (default) recovers
# independent GBM exactly.

# Event shocks: small per-tick probability of a one-off jump.
P_EVENT = 0.0   # e.g. 0.001 → ~1 event per ticker per ~8 minutes at 0.5s ticks

def maybe_shock(st: TickerState) -> None:
    if st.rng.random() < P_EVENT:
        jump = st.rng.uniform(0.02, 0.05) * st.rng.choice((-1, 1))
        st.price = max(st.price * (1 + jump), PRICE_FLOOR)
```

---

## 11. The Massive provider — `MassiveProvider`

Backed by Massive's (formerly Polygon.io) **Full Market Snapshot** endpoint — one HTTP request covers the entire watchlist, which is what keeps this inside the free tier's 5 req/min budget. See `MASSIVE_API.md` for the full endpoint reference; this section is the concrete `MarketDataProvider` implementation.

### 11.1 HTTP client & auth

```python
# backend/market/massive.py (part 1 — client setup)
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import httpx

from .base import MarketDataProvider
from .types import PriceTick

log = logging.getLogger("market.massive")

BASE_URL = "https://api.massive.com"
SNAPSHOT_PATH = "/v2/snapshot/locale/us/markets/stocks/tickers"
SINGLE_SNAPSHOT_PATH = "/v2/snapshot/locale/us/markets/stocks/tickers/{ticker}"

REQUEST_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
MAX_BACKOFF_SECONDS = 60.0
```

Authentication uses the `Authorization: Bearer` header (never the `apiKey` query parameter, so the key never lands in logs or proxy access logs):

```python
# backend/market/massive.py (part 2 — price extraction)

def extract_price(ticker_obj: dict) -> float | None:
    """Freshest-first fallback chain (MASSIVE_API.md §3):
    live trade -> minute bar close -> today's cumulative close -> prev close.
    Returns None if nothing usable is present (e.g. during the ~03:30-04:00
    EST daily snapshot reset window with no prevDay data either)."""
    for section, field in (("lastTrade", "p"), ("min", "c"), ("day", "c"), ("prevDay", "c")):
        value = ticker_obj.get(section, {}).get(field)
        if value:                      # skips both None and 0.0
            return float(value)
    return None


def ns_to_dt(ns: int) -> datetime:
    return datetime.fromtimestamp(ns / 1_000_000_000, tz=timezone.utc)
```

### 11.2 The provider class

```python
# backend/market/massive.py (part 3 — MassiveProvider)

class MassiveProvider(MarketDataProvider):
    source = "massive"

    def __init__(self, api_key: str, poll_interval: float,
                 base_url: str = BASE_URL) -> None:
        self.poll_interval = poll_interval
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=REQUEST_TIMEOUT,
        )
        self._backoff_until: float = 0.0       # event-loop monotonic time

    # -- MarketDataProvider interface ---------------------------------------
    async def start(self) -> None:
        pass   # httpx.AsyncClient is already usable; nothing to warm up

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get_prices(self, tickers: list[str]) -> dict[str, PriceTick]:
        if not tickers:
            return {}
        loop = asyncio.get_running_loop()
        if loop.time() < self._backoff_until:
            return {}                          # still cooling down from a 429/5xx

        try:
            resp = await self._client.get(
                SNAPSHOT_PATH,
                params={"tickers": ",".join(sorted(set(t.upper() for t in tickers)))},
            )
        except httpx.HTTPError:
            log.exception("massive snapshot request failed")
            return {}

        if resp.status_code == 429:
            self._enter_backoff(loop, resp.headers.get("Retry-After"))
            return {}
        if resp.status_code >= 500:
            self._enter_backoff(loop, None)
            return {}
        if resp.status_code in (401, 403):
            # Bad key or endpoint not in plan — log once per occurrence and
            # surface via /api/health (source stays "massive", staleness grows).
            # We do NOT fall back to the simulator mid-process: provider
            # selection happens once at startup (§9).
            log.error("massive snapshot auth error: %s", resp.status_code)
            return {}
        resp.raise_for_status()

        body = resp.json()
        now = datetime.now(timezone.utc)
        out: dict[str, PriceTick] = {}
        for t in body.get("tickers", []):
            price = extract_price(t)
            if price is None:
                continue                       # no usable field this cycle — keep last cached
            symbol = t["ticker"]
            updated_ns = t.get("updated")
            ts = ns_to_dt(updated_ns) if updated_ns else now
            out[symbol] = PriceTick(symbol, price, ts)
        return out

    async def validate_ticker(self, ticker: str) -> bool:
        symbol = ticker.upper()
        try:
            resp = await self._client.get(SINGLE_SNAPSHOT_PATH.format(ticker=symbol))
        except httpx.HTTPError:
            log.exception("massive validate_ticker request failed for %s", symbol)
            return False                       # fail closed: reject on transport error

        if resp.status_code == 404:
            return False
        if resp.status_code != 200:
            log.warning("massive validate_ticker unexpected status %s for %s",
                        resp.status_code, symbol)
            return False

        body = resp.json()
        return extract_price(body.get("ticker", {})) is not None

    # -- internals -------------------------------------------------------
    def _enter_backoff(self, loop: asyncio.AbstractEventLoop, retry_after: str | None) -> None:
        try:
            delay = float(retry_after) if retry_after else self.poll_interval
        except ValueError:
            delay = self.poll_interval
        delay = min(delay, MAX_BACKOFF_SECONDS)
        self._backoff_until = loop.time() + delay
        log.warning("massive backing off for %.1fs", delay)
```

Design notes:

- **One request per poll cycle, for the whole watchlist** — `get_prices()` always issues a single `tickers=A,B,C,...` snapshot call, never one request per ticker. At the free tier's 15s poll interval that's 4 req/min, comfortably inside the 5 req/min budget (`MASSIVE_API.md` §2).
- **Never raises out of the provider.** Every failure path (`HTTPError`, `429`, `5xx`, `401/403`) is caught and returns `{}` (or partial data), matching the ABC contract in §5. `MarketPoller._poll_once` treats an empty result as "no update this cycle" and the cache keeps serving the last known price — exactly the PLAN §6 requirement that "a ticker that later stops resolving is kept but streams its last known price".
- **Backoff on `429`** respects `Retry-After` when Massive sends it, otherwise falls back to one poll interval, capped at 60s — deliberately conservative since this runs unattended in a classroom demo.
- **`validate_ticker` fails closed.** A transport error or unexpected status rejects the ticker (`400 unknown_ticker`) rather than silently admitting one the system can't actually price. This matches PLAN §6's "Unknown symbol" rule.
- **No provider fallback mid-process.** If the Massive key is bad, the app does *not* silently switch to the simulator — that would be a surprising and hard-to-debug behavior change under a professor's nose. It keeps polling Massive (which keeps failing and returning `{}`), the cache goes stale, and `/api/health` reports it. Provider choice is a boot-time decision (§9), fixed by design.

### 11.3 Timestamp handling

Massive reports different units in different places (`MASSIVE_API.md` §8): snapshot fields are **nanoseconds**, aggregates/grouped/prev are **milliseconds**. This provider only ever reads the snapshot endpoint, so only `ns_to_dt` (§11.1) is needed; it is applied at the edge and the rest of the codebase only ever sees timezone-aware UTC `datetime`s via `PriceTick.timestamp`.

---

## 12. Provider interface conformance — a shared test

Because both providers implement one ABC, interface-level behavior is tested **once** and run against both via `pytest.mark.parametrize`, rather than duplicated:

```python
# backend/tests/market/test_provider_conformance.py
import pytest

from market.simulator import SimulatedProvider
from market.massive import MassiveProvider


@pytest.fixture(params=["simulator", "massive"])
def provider(request, httpx_mock):     # httpx_mock only exercised for the massive case
    if request.param == "simulator":
        return SimulatedProvider(seed=42)
    return MassiveProvider(api_key="test-key")


@pytest.mark.asyncio
async def test_get_prices_skips_malformed_symbols(provider):
    prices = await provider.get_prices(["", "123", "aa pl"])
    assert prices == {}


@pytest.mark.asyncio
async def test_get_prices_never_raises_on_partial_failure(provider):
    # each provider's fixture pre-arranges one bad ticker / one bad response;
    # the call must return without raising regardless.
    await provider.get_prices(["AAPL", "NOTAREALTICKER"])
```

---

## 13. Wiring into FastAPI

```python
# backend/app.py (excerpt)
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

    # Held positions must stream even if dropped from the watchlist, and
    # must appear at *some* price before the first poll completes.
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

### 13.1 SSE endpoint

```python
# backend/routes/stream.py
import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from market.types import PriceEvent, to_iso_z
from market.hub import _direction

router = APIRouter()


def _event_from_entry(ticker: str, entry) -> str:
    ev = PriceEvent(
        ticker=ticker,
        price=round(entry.price, 4),
        previous_price=round(entry.previous_price, 4),
        timestamp=to_iso_z(entry.source_time),
        direction=_direction(entry.previous_price, entry.price),
    )
    return ev.to_sse()


@router.get("/api/stream/prices")
async def stream_prices(request: Request):
    hub = request.app.state.hub
    queue = hub.subscribe()

    async def gen():
        try:
            # Flush the current cache immediately so a fresh client isn't
            # blank for up to 500ms waiting on the next broadcast tick.
            for ticker, entry in request.app.state.cache.snapshot().items():
                yield _event_from_entry(ticker, entry)
            while True:
                if await request.is_disconnected():
                    break
                try:
                    ev = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield ev.to_sse()
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"        # comment frame — keeps proxies from closing idle conns
        finally:
            hub.unsubscribe(queue)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )
```

### 13.2 Watchlist ticker validation

```python
# backend/routes/watchlist.py (excerpt)
@router.post("/api/watchlist")
async def add_ticker(body: AddTicker, request: Request):
    symbol = body.ticker.strip().upper()
    if not TICKER_RE.match(symbol):
        raise HTTPException(400, {"error": {"code": "invalid_ticker", "message": "..."}})
    if watchlist_repo.count() >= 30:
        raise HTTPException(400, {"error": {"code": "watchlist_full", "message": "..."}})
    if watchlist_repo.has(symbol):
        return watchlist_repo.get(symbol)              # 200 no-op (PLAN §8)

    provider = request.app.state.provider
    if not await provider.validate_ticker(symbol):
        raise HTTPException(400, {"error": {"code": "unknown_ticker", "message": f"{symbol} not found"}})

    row = watchlist_repo.add(symbol)
    first = await provider.get_prices([symbol])
    seed_price = first[symbol].price if symbol in first else 0.0
    request.app.state.cache.seed_missing(symbol, seed_price)
    return row
```

### 13.3 Health

```python
# backend/routes/health.py
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter()


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
            "market_feed": {
                "source": provider.source,
                "last_update_age_ms": round(age) if age is not None else -1,
            },
        },
    )
```

---

## 14. Test plan (PLAN §12)

| Area | Test | Notes |
|---|---|---|
| Interface conformance | `get_prices` skips malformed/unknown symbols without raising; returns only resolvable tickers | parametrized across both providers (§12) |
| `PriceCache` | `ingest` preserves `previous_price` across writes; `mark_emitted` advances it; `seed_missing` is idempotent | pure, no I/O |
| `PriceCache` | `newest_update_age_ms()` is `None` when empty, grows monotonically otherwise | drives `/api/health` |
| `PriceStreamHub` | `direction` is `up`/`down`/`flat` vs the last *emitted* value, not the last ingested one | mock `PriceCache.snapshot()` |
| `PriceStreamHub` | an unchanged price still re-emits with `direction: "flat"` every tick | loop-level test |
| `MarketPoller` | primes the cache synchronously in `start()` before the periodic task begins | |
| `MarketPoller` | `_poll_once` unions watchlist ∪ cache keys — a ticker dropped from the watchlist but still cached stays polled | |
| `MarketPoller` | a provider exception in one cycle is logged and swallowed; next cycle still runs | |
| `factory.create_provider` | returns `MassiveProvider` iff `MASSIVE_API_KEY` is non-empty after `.strip()` | monkeypatch `os.environ` |
| Simulator | GBM step direction follows the sign of a mocked `rng.gauss` draw | see snippet below |
| Simulator | sample stdev of log-returns over N steps with a fixed seed ≈ `σ·√dt` within tolerance | statistical test |
| Simulator | two tickers, same params, different symbols ⇒ different sequences under one master seed | independence |
| Simulator | 100k steps at high σ never produces `price <= 0`; floor holds | |
| Simulator | `SimulatedProvider` after `start()` reports each curated ticker at its exact `SEED_PRICES` value before any `step()` | |
| Simulator | `deterministic_seed_price("PYPL")` equals a hard-coded golden value; stable across runs/processes | pins the hash choice |
| Simulator | `seed_for("PYPL")` → `(price in [15,600], drift=0.0, vol=0.30)` | |
| Simulator | `get_prices(["PYPL"])` on a fresh provider returns a tick and lazily registers `TickerState` | |
| Massive | snapshot response fixture → correct `PriceTick`s via `extract_price`'s fallback chain (`lastTrade.p` → `min.c` → `day.c` → `prevDay.c`) | fixture JSON per `MASSIVE_API.md` §3 |
| Massive | a `0` or missing value at each fallback level correctly falls through to the next | |
| Massive | `429` triggers backoff honoring `Retry-After`; falls back to `poll_interval` when the header is absent/unparseable | |
| Massive | `5xx` and transport errors return `{}` without raising | |
| Massive | `validate_ticker` returns `False` on `404` and on any transport error (fail closed) | |
| Massive | `get_prices([])` short-circuits to `{}` without an HTTP call | |

Example — pinning the GBM sign (from `MARKET_SIMULATOR.md` §8):

```python
def test_step_sign(monkeypatch):
    st = TickerState("AAPL", 100.0, drift=0.0, volatility=0.3, rng=random.Random(0))
    monkeypatch.setattr(st.rng, "gauss", lambda mu, sigma: 5.0)
    st.step()
    assert st.price > 100.0
    monkeypatch.setattr(st.rng, "gauss", lambda mu, sigma: -5.0)
    st.step()
    assert st.price < st.prev_price
```

Example — Massive snapshot parsing with `pytest-httpx`:

```python
@pytest.mark.asyncio
async def test_get_prices_prefers_last_trade(httpx_mock):
    httpx_mock.add_response(
        url="https://api.massive.com/v2/snapshot/locale/us/markets/stocks/tickers?tickers=AAPL",
        json={"status": "OK", "count": 1, "tickers": [{
            "ticker": "AAPL",
            "lastTrade": {"p": 190.72, "t": 1739210399900000000},
            "day": {"c": 189.5},
            "prevDay": {"c": 187.1},
        }]},
    )
    provider = MassiveProvider(api_key="test-key")
    prices = await provider.get_prices(["AAPL"])
    assert prices["AAPL"].price == 190.72
```

---

## 15. Tunable constants — summary

| Constant | Location | Default | Effect |
|---|---|---|---|
| `BROADCAST_INTERVAL` | `hub.py` | `0.5` | SSE broadcast cadence — fixed regardless of provider |
| `SUBSCRIBER_QUEUE_MAX` | `hub.py` | `1000` | Per-connection backpressure before a slow client is dropped |
| `MASSIVE_POLL_SECONDS` | `factory.py` | `15.0` | Free-tier-safe poll interval for Massive |
| `SIMULATOR_POLL_SECONDS` | `factory.py` | `0.5` | Simulator poll interval (matches its own internal step cadence) |
| `UPDATE_INTERVAL_SECONDS` | `simulator.py` | `0.5` | GBM step cadence |
| `SECONDS_PER_TRADING_YEAR` | `simulator.py` | `5_896_800` | Scales `dt`; larger ⇒ smaller per-tick moves |
| `DEFAULT_VOL` / `DEFAULT_DRIFT` | `simulator.py` | `0.30` / `0.0` | Baseline GBM parameters |
| `PRICE_FLOOR` | `simulator.py` | `0.01` | Hard lower bound on simulated price |
| `PRICE_FLOOR_SEED` / `PRICE_CEIL_SEED` | `simulator.py` | `15 / 600` | Range for hash-seeded novel tickers |
| `P_EVENT` (stretch) | `simulator.py` | `0.0` | Per-tick event-shock probability |
| `REQUEST_TIMEOUT` | `massive.py` | `10s / 5s connect` | httpx client timeout |
| `MAX_BACKOFF_SECONDS` | `massive.py` | `60.0` | Cap on `429`/`5xx` backoff |

---

## 16. Relationship to the source documents

| This document's section | Elaborates on |
|---|---|
| §2–§9, §13 | `MARKET_INTERFACE.md` — architecture, cache, hub, poller, factory, FastAPI wiring |
| §10 | `MARKET_SIMULATOR.md` — GBM model, seed tables, `SimulatedProvider` |
| §11 | `MASSIVE_API.md` — turns the endpoint reference into the concrete `MassiveProvider` class (backoff, error handling, timestamp normalization — not previously written as one class) |
| §12, §14 | `MARKET_INTERFACE.md` §11 / `MARKET_SIMULATOR.md` §8 — consolidated, plus the new Massive-specific cases |

Read the three source documents for design rationale and alternatives considered; this document is the one to implement against.
