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
SECONDS_PER_TRADING_YEAR = 252 * 6.5 * 3600  # 5_896_800
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
    frac = int.from_bytes(digest[:8], "big") / 2**64  # uniform in [0, 1)
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


class SimulatedProvider(MarketDataProvider):
    source = "simulator"

    def __init__(self, poll_interval: float = UPDATE_INTERVAL_SECONDS, seed: int | None = None) -> None:
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
                continue  # malformed symbol: skip, never raise
            st = self._ensure(sym)  # lazily materialize newly-watched tickers
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
            st = TickerState(symbol=symbol, price=price, drift=drift, volatility=vol, rng=rng_for(symbol, self._seed))
            self._states[symbol] = st
        return st

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self.poll_interval)
            for sym in list(self._states):  # snapshot keys: concurrent _ensure() is safe
                self._states[sym].step()
