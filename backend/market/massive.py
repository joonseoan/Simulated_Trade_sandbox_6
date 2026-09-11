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
    EST daily snapshot reset window with no prevDay data either)."""
    for section, field in (("lastTrade", "p"), ("min", "c"), ("day", "c"), ("prevDay", "c")):
        value = (ticker_obj.get(section) or {}).get(field)
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
            # selection happens once at startup (§9).
            log.error("massive snapshot auth error: %s", resp.status_code)
            return {}
        if resp.status_code != 200:
            # Any other non-2xx (400/404/422/...): never raise out of
            # get_prices() — the ABC contract (base.py) forbids it, and
            # MarketPoller.start() awaits this synchronously and unguarded
            # during FastAPI's lifespan, so a raised exception here would
            # crash the app at boot.
            log.error("massive snapshot unexpected status: %s", resp.status_code)
            return {}

        try:
            body = resp.json()
        except ValueError:
            # Malformed JSON body on an otherwise-200 response. Same
            # never-raise contract as the status-code branches above: log
            # and return {} rather than let json.JSONDecodeError propagate
            # out through MarketPoller.start()'s unguarded priming call.
            log.exception("massive snapshot returned invalid JSON")
            return {}

        tickers_payload = body.get("tickers")
        if not isinstance(tickers_payload, list):
            log.error("massive snapshot payload missing/invalid 'tickers' list")
            return {}

        now = datetime.now(timezone.utc)
        out: dict[str, PriceTick] = {}
        for t in tickers_payload:
            if not isinstance(t, dict):
                continue  # malformed entry: skip rather than raise
            price = extract_price(t)
            if price is None:
                continue  # no usable field this cycle — keep last cached
            symbol = t.get("ticker")
            if not symbol:
                continue  # no symbol to key on: skip rather than KeyError
            symbol = symbol.upper()  # keep the (uppercased) key contract from base.py
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
