# Market Simulator — Design & Code Structure

_The default price feed when `MASSIVE_API_KEY` is unset. Implements the `MarketDataProvider` interface from `MARKET_INTERFACE.md`. No network, no external dependencies, runs as an in-process asyncio task._

---

## 1. What it must do (from PLAN §6)

- Generate prices via **independent per-ticker geometric Brownian motion (GBM)** with configurable drift and volatility.
- Update at **~500 ms** intervals.
- Seed the 10 default tickers from a **curated table** of realistic values.
- Any other ticker added later gets a **deterministic seed price derived from its symbol** (stable hash → a plausible $15–$600), drift `0`, ~30% annualized volatility.
- Be cheap to unit-test: GBM math is checkable, the novel-ticker seed is deterministic.
- **Stretch only:** cross-ticker correlation and random "event" shocks. Core is independent per-ticker GBM.

---

## 2. The GBM model

Discrete-time GBM update over a timestep `dt`:

```
S(t + dt) = S(t) · exp( (μ − ½σ²)·dt  +  σ·√dt·Z ),   Z ~ N(0, 1)
```

| Symbol | Meaning | Units | Default |
|---|---|---|---|
| `S` | price | dollars | per-ticker seed |
| `μ` (`drift`) | expected log-return | **annualized** | `0.0` (see §3) |
| `σ` (`volatility`) | volatility of log-return | **annualized** | `0.30` |
| `dt` | timestep | **years** | `interval / SECONDS_PER_TRADING_YEAR` |
| `Z` | standard normal draw | — | fresh per ticker per step |

### Choosing `dt`

Volatility is quoted annualized, so `dt` must be in years. Use a **trading-time** year so a 500 ms tick during "market hours" produces visible-but-not-crazy movement:

```
SECONDS_PER_TRADING_YEAR = 252 days × 6.5 hours × 3600  = 5_896_800
dt = UPDATE_INTERVAL_SECONDS / SECONDS_PER_TRADING_YEAR         # 0.5 / 5_896_800 ≈ 8.48e-8
```

Sanity check with σ = 0.30: per-step standard deviation of the log-return is
`σ·√dt = 0.30 · √8.48e-8 ≈ 2.76e-5` → about **0.0028% per tick**, ≈ 0.13% per minute,
≈ **1% over a ~1.3-hour session** — lively sparklines without absurd swings. Tunable via one constant.

> The simulator runs continuously (no market-hours gating) — the "trading year" is just a scaling choice for tick size. `EVENTS`/correlation are the only stretch pieces; wall-clock gating is intentionally omitted.

### Numerical guards

- GBM is analytically positive, but clamp to a floor anyway: `price = max(price, 0.01)`.
- Round the *stored* price to 4 dp to avoid float drift accumulating meaninglessly; keep full precision internally if preferred, round on read.

---

## 3. Drift

Default `μ = 0.0` for every ticker (PLAN mandates `0` for novel tickers; the curated table uses `0.0` too for predictable demos). A small optional per-ticker drift (e.g. `±0.05`) can be set in the seed table for flavor without violating the spec — kept at `0.0` by default so P&L is driven by variance, not a baked-in trend.

---

## 4. Seed prices

### 4.1 Curated table (the 10 defaults)

Values chosen to be plausible as of early 2026; exact figures don't matter, only that they're realistic and stable.

```python
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

# Optional per-ticker overrides; anything absent uses DEFAULT_DRIFT / DEFAULT_VOL.
SEED_PARAMS: dict[str, tuple[float, float]] = {
    # ticker: (annualized drift, annualized volatility)
    "TSLA": (0.0, 0.55),
    "NVDA": (0.0, 0.50),
    "NFLX": (0.0, 0.40),
}

DEFAULT_DRIFT = 0.0
DEFAULT_VOL = 0.30
```

### 4.2 Deterministic seed for any other ticker

A symbol not in the table (added via the watchlist or by the AI) gets a stable, plausible price from a hash of its symbol. Must be **deterministic across processes and Python runs** — so `hashlib`, not the salted built-in `hash()`.

```python
import hashlib

PRICE_FLOOR, PRICE_CEIL = 15.0, 600.0

def deterministic_seed_price(symbol: str) -> float:
    digest = hashlib.sha256(symbol.upper().encode()).digest()
    # first 8 bytes → unsigned int → uniform in [0, 1)
    frac = int.from_bytes(digest[:8], "big") / 2**64
    return round(PRICE_FLOOR + frac * (PRICE_CEIL - PRICE_FLOOR), 2)

def seed_for(symbol: str) -> tuple[float, float, float]:
    """Return (price, drift, volatility) for any symbol."""
    sym = symbol.upper()
    if sym in SEED_PRICES:
        drift, vol = SEED_PARAMS.get(sym, (DEFAULT_DRIFT, DEFAULT_VOL))
        return SEED_PRICES[sym], drift, vol
    return deterministic_seed_price(sym), 0.0, DEFAULT_VOL
```

`deterministic_seed_price("PYPL")` returns the same value on every machine forever — this is the property the unit test pins.

---

## 5. Determinism / RNG

- Each ticker owns its own `random.Random` (or `numpy.random.Generator`) instance so tickers are **statistically independent** and adding/removing one doesn't perturb another's sequence.
- A top-level `seed: int | None` on the provider makes the whole simulation reproducible for tests: each ticker's RNG is seeded from `hash((seed, symbol))`. `seed=None` (default, production) uses entropy.
- `LLM_MOCK` does **not** touch the simulator — E2E tests assert on behavior, not values (PLAN §12, D14). The `seed` knob exists purely for backend unit tests of GBM math.

```python
import random

def _rng_for(symbol: str, master_seed: int | None) -> random.Random:
    if master_seed is None:
        return random.Random()
    return random.Random(f"{master_seed}:{symbol.upper()}")
```

---

## 6. Code structure

```
backend/market/simulator.py
```

```python
# backend/market/simulator.py
from __future__ import annotations

import asyncio
import math
import random
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .base import MarketDataProvider
from .types import PriceTick

# ── tunables ───────────────────────────────────────────────────────────
UPDATE_INTERVAL_SECONDS = 0.5
SECONDS_PER_TRADING_YEAR = 252 * 6.5 * 3600          # 5_896_800
DT_YEARS = UPDATE_INTERVAL_SECONDS / SECONDS_PER_TRADING_YEAR
PRICE_FLOOR = 0.01
TICKER_RE = re.compile(r"^[A-Z][A-Z.\-]{0,9}$")

DEFAULT_DRIFT = 0.0
DEFAULT_VOL = 0.30
SEED_PRICES = { ... }        # §4.1
SEED_PARAMS = { ... }        # §4.1


# ── per-ticker state ───────────────────────────────────────────────────
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
        growth = (self.drift - 0.5 * self.volatility**2) * dt
        shock = self.volatility * math.sqrt(dt) * z
        self.prev_price = self.price
        self.price = max(self.price * math.exp(growth + shock), PRICE_FLOOR)
        self.updated_at = datetime.now(timezone.utc)


# ── provider ───────────────────────────────────────────────────────────
class SimulatedProvider(MarketDataProvider):
    source = "simulator"

    def __init__(self, poll_interval: float = UPDATE_INTERVAL_SECONDS,
                 seed: int | None = None) -> None:
        self.poll_interval = poll_interval
        self._seed = seed
        self._states: dict[str, TickerState] = {}
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None

    # -- interface ------------------------------------------------------
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
                continue
            st = self._ensure(sym)              # lazily materialize newly-watched symbols
            out[sym] = PriceTick(sym, round(st.price, 4), st.updated_at)
        return out

    async def validate_ticker(self, ticker: str) -> bool:
        # Every well-formed symbol is synthesizable.
        return bool(TICKER_RE.match(ticker.upper()))

    # -- internals ---------------------------------------------------
    def _ensure(self, symbol: str) -> TickerState:
        st = self._states.get(symbol)
        if st is None:
            price, drift, vol = seed_for(symbol)        # §4.2
            st = TickerState(
                symbol=symbol, price=price, drift=drift, volatility=vol,
                rng=_rng_for(symbol, self._seed),        # §5
            )
            self._states[symbol] = st
        return st

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self.poll_interval)
            # snapshot keys so a concurrent _ensure() doesn't mutate during iteration
            for sym in list(self._states):
                self._states[sym].step()
```

### How it plugs in

`MarketPoller` (see `MARKET_INTERFACE.md` §7) calls `get_prices(watchlist ∪ held ∪ cached)` every `poll_interval`. `SimulatedProvider` already advanced those tickers in its own `_run()` loop; `get_prices` just reads the current `TickerState.price`. The two loops run at the same 500 ms cadence, so each poll returns one fresh GBM step per ticker.

> Alternative (simpler, one loop): drop `SimulatedProvider._run()` entirely and call `st.step()` inside `get_prices()` right before reading. This couples the step to the poll and is easier to reason about in tests. Keep the internal loop only if the simulator must keep advancing prices when no one is polling (e.g. to make sparklines "catch up" after a tab was backgrounded). **Recommendation: start with the single-loop version; add the internal task only if needed.**

---

## 7. Stretch features (not required to pass — PLAN D12)

Keep these behind flags, off by default, so the core stays independently testable.

### 7.1 Correlated moves

Give correlated tickers a shared market factor each step:

```
Z_i = √ρ · Z_market  +  √(1−ρ) · Z_i_idiosyncratic
```

with `ρ` per group (e.g. `{AAPL, MSFT, GOOGL, META, NVDA}` tech at `ρ = 0.6`). One extra `Z_market` draw per group per step; everything else unchanged.

### 7.2 Event shocks

Each step, with small probability `p_event` (e.g. `0.001` per ticker per tick ≈ one event per ticker per ~8 min), apply a one-off multiplicative jump of `±2–5%`:

```python
if st.rng.random() < P_EVENT:
    jump = st.rng.uniform(0.02, 0.05) * st.rng.choice((-1, 1))
    st.price = max(st.price * (1 + jump), PRICE_FLOOR)
```

### 7.3 Mean reversion (optional flavor)

An Ornstein–Uhlenbeck pull toward the seed price keeps long-running sessions from drifting to $0 or $10,000. Off by default; a `reversion_strength` knob adds `θ·(ln S₀ − ln S)·dt` to the log-return.

---

## 8. Test plan (PLAN §12)

| Test | Assertion |
|---|---|
| GBM step direction | with a mocked `rng.gauss` returning `+3` / `−3`, price moves up / down accordingly |
| GBM step magnitude | over N steps with `seed` fixed, sample stdev of log-returns ≈ `σ·√dt` within tolerance |
| independence | two tickers with the same params but different symbols produce different sequences under the same master seed |
| no negative prices | 100k steps with high σ never yields `price <= 0`; floor holds |
| curated seed | `SimulatedProvider` after `start()` reports `AAPL` within, say, ±0 of `SEED_PRICES["AAPL"]` before any step |
| deterministic novel seed | `deterministic_seed_price("PYPL")` equals a hard-coded golden value; stable across runs |
| novel ticker params | `seed_for("PYPL")` → drift `0.0`, vol `0.30`, price in `[15, 600]` |
| lazy materialization | `get_prices(["PYPL"])` on a fresh provider returns a tick and registers the state |
| interface conformance | shared parametrized test also run against `MassiveProvider` (see `MARKET_INTERFACE.md` §11) |
| malformed symbol | `get_prices(["aa pl", "123", ""])` returns `{}`, doesn't raise |

Example — pinning the GBM sign:

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

---

## 9. Summary of tunable constants

| Constant | Default | Effect |
|---|---|---|
| `UPDATE_INTERVAL_SECONDS` | `0.5` | Tick cadence |
| `SECONDS_PER_TRADING_YEAR` | `5_896_800` | Scales `dt`; larger ⇒ smaller per-tick moves |
| `DEFAULT_VOL` | `0.30` | Baseline annualized volatility |
| `DEFAULT_DRIFT` | `0.0` | Baseline trend (keep at 0) |
| `PRICE_FLOOR` | `0.01` | Hard lower bound |
| `PRICE_FLOOR / PRICE_CEIL` (seed) | `15 / 600` | Range for hash-seeded novel tickers |
| `P_EVENT` (stretch) | `0.0` | Per-tick event-shock probability |
| `ρ` (stretch) | `0.0` | Intra-group correlation |
