"""The Massive (formerly Polygon.io) REST provider.

Backed by the Full Market Snapshot endpoint — one HTTP request covers the
entire watchlist, which is what keeps this inside the free tier's 5 req/min
budget. See planning/MASSIVE_API.md for the full endpoint reference and
planning/MARKET_DATA_DESIGN.md §11 for the concrete design.
"""
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


def extract_price(ticker_obj: dict) -> float | None:
    """Freshest-first fallback chain (MASSIVE_API.md §3):
    live trade -> minute bar close -> today's cumulative close -> prev close.
    Returns None if nothing usable is present (e.g. during the ~03:30-04:00
    EST daily snapshot reset window with no prevDay data either).
    """
    for section, field_name in (("lastTrade", "p"), ("min", "c"), ("day", "c"), ("prevDay", "c")):
        value = ticker_obj.get(section, {}).get(field_name)
        if value:  # skips both None and 0.0
            return float(value)
    return None


def ns_to_dt(ns: int) -> datetime:
    return datetime.fromtimestamp(ns / 1_000_000_000, tz=timezone.utc)


class MassiveProvider(MarketDataProvider):
    source = "massive"

    def __init__(self, api_key: str, poll_interval: float = 15.0, base_url: str = BASE_URL) -> None:
        self.poll_interval = poll_interval
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=REQUEST_TIMEOUT,
        )
        self._backoff_until: float = 0.0  # event-loop monotonic time

    # -- MarketDataProvider interface ---------------------------------------
    async def start(self) -> None:
        pass  # httpx.AsyncClient is already usable; nothing to warm up

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get_prices(self, tickers: list[str]) -> dict[str, PriceTick]:
        if not tickers:
            return {}
        loop = asyncio.get_running_loop()
        if loop.time() < self._backoff_until:
            return {}  # still cooling down from a 429/5xx

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
            # selection happens once at startup.
            log.error("massive snapshot auth error: %s", resp.status_code)
            return {}
        resp.raise_for_status()

        body = resp.json()
        now = datetime.now(timezone.utc)
        out: dict[str, PriceTick] = {}
        for t in body.get("tickers", []):
            price = extract_price(t)
            if price is None:
                continue  # no usable field this cycle — keep last cached
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
            return False  # fail closed: reject on transport error

        if resp.status_code == 404:
            return False
        if resp.status_code != 200:
            log.warning("massive validate_ticker unexpected status %s for %s", resp.status_code, symbol)
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
