from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from market.base import MarketDataProvider
from market.cache import PriceCache
from market.poller import MarketPoller
from market.types import PriceTick


class FakeProvider(MarketDataProvider):
    source = "fake"

    def __init__(self, poll_interval: float = 0.01) -> None:
        self.poll_interval = poll_interval
        self.started = False
        self.closed = False
        self.calls: list[list[str]] = []
        self.raise_on_next_call = False
        self.fail_count = 0

    async def start(self) -> None:
        self.started = True

    async def aclose(self) -> None:
        self.closed = True

    async def get_prices(self, tickers: list[str]) -> dict[str, PriceTick]:
        self.calls.append(sorted(tickers))
        if self.raise_on_next_call:
            self.raise_on_next_call = False
            self.fail_count += 1
            raise RuntimeError("upstream blew up")
        now = datetime.now(timezone.utc)
        return {t: PriceTick(t, 100.0, now) for t in tickers}

    async def validate_ticker(self, ticker: str) -> bool:
        return True


async def test_start_calls_provider_start_and_primes_cache_synchronously():
    provider = FakeProvider()
    cache = PriceCache()
    poller = MarketPoller(provider, cache, tickers_supplier=lambda: ["AAPL"])

    await poller.start()
    try:
        assert provider.started is True
        # primed before start() returns — tick #1 of the SSE hub isn't empty
        assert cache.has("AAPL")
        assert provider.calls == [["AAPL"]]
    finally:
        await poller.stop()

    assert provider.closed is True


async def test_poll_once_unions_watchlist_and_already_cached_tickers():
    provider = FakeProvider()
    cache = PriceCache()
    # simulate a ticker already known to the cache (e.g. a held position)
    # that has since been dropped from the watchlist.
    cache.ingest(PriceTick("MSFT", 50.0, datetime.now(timezone.utc)))

    poller = MarketPoller(provider, cache, tickers_supplier=lambda: ["AAPL"])
    await poller._poll_once()

    assert provider.calls == [["AAPL", "MSFT"]]
    assert cache.has("AAPL")
    assert cache.has("MSFT")


async def test_poll_once_no_op_when_no_tickers_known_anywhere():
    provider = FakeProvider()
    cache = PriceCache()
    poller = MarketPoller(provider, cache, tickers_supplier=lambda: [])

    await poller._poll_once()
    assert provider.calls == []


async def test_run_loop_survives_a_provider_exception_and_keeps_polling():
    provider = FakeProvider(poll_interval=0.01)
    cache = PriceCache()
    poller = MarketPoller(provider, cache, tickers_supplier=lambda: ["AAPL"])

    await poller.start()  # first _poll_once() succeeds, primes the cache
    provider.raise_on_next_call = True
    try:
        # give the periodic loop a few cycles: one raises (caught & logged
        # by _run's try/except), later ones must still succeed.
        await asyncio.sleep(0.08)
        assert provider.fail_count == 1
        assert len(provider.calls) > 2
        assert cache.latest_price("AAPL") == 100.0
    finally:
        await poller.stop()
