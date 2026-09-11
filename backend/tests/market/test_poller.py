import asyncio
from datetime import datetime, timezone

import pytest

from market.base import MarketDataProvider
from market.cache import PriceCache
from market.poller import MarketPoller
from market.types import PriceTick


class FakeProvider(MarketDataProvider):
    source = "fake"

    def __init__(self, poll_interval: float = 0.01, fail_after: int | None = None) -> None:
        self.poll_interval = poll_interval
        self.calls: list[list[str]] = []
        self.started = False
        self.closed = False
        self._fail_after = fail_after

    async def start(self) -> None:
        self.started = True

    async def aclose(self) -> None:
        self.closed = True

    async def get_prices(self, tickers: list[str]) -> dict[str, PriceTick]:
        self.calls.append(sorted(tickers))
        if self._fail_after is not None and len(self.calls) > self._fail_after:
            raise RuntimeError("upstream exploded")
        now = datetime.now(timezone.utc)
        return {t: PriceTick(t, 100.0, now) for t in tickers}

    async def validate_ticker(self, ticker: str) -> bool:
        return True


@pytest.mark.asyncio
async def test_start_calls_provider_start_and_primes_cache_synchronously():
    provider = FakeProvider()
    cache = PriceCache()
    poller = MarketPoller(provider, cache, tickers_supplier=lambda: ["AAPL", "MSFT"])

    await poller.start()
    try:
        assert provider.started is True
        assert cache.known_tickers() == {"AAPL", "MSFT"}
        assert len(provider.calls) == 1
    finally:
        await poller.stop()
        assert provider.closed is True


@pytest.mark.asyncio
async def test_poll_once_unions_watchlist_and_cached_tickers():
    provider = FakeProvider()
    cache = PriceCache()
    cache.seed_missing("HELD", 50.0)  # e.g. a position dropped from the watchlist
    poller = MarketPoller(provider, cache, tickers_supplier=lambda: ["AAPL"])

    await poller._poll_once()

    assert provider.calls[-1] == ["AAPL", "HELD"]


@pytest.mark.asyncio
async def test_poll_once_is_a_noop_when_nothing_to_poll():
    provider = FakeProvider()
    cache = PriceCache()
    poller = MarketPoller(provider, cache, tickers_supplier=lambda: [])

    await poller._poll_once()

    assert provider.calls == []


@pytest.mark.asyncio
async def test_run_loop_swallows_provider_exceptions_and_keeps_going():
    # First call (during start()'s synchronous priming) succeeds; every
    # subsequent call raises. The background loop must survive.
    provider = FakeProvider(poll_interval=0.01, fail_after=1)
    cache = PriceCache()
    poller = MarketPoller(provider, cache, tickers_supplier=lambda: ["AAPL"])

    await poller.start()
    try:
        await asyncio.sleep(0.05)
        assert not poller._task.done()
        assert len(provider.calls) > 1  # the loop kept retrying despite failures
        # the cache still serves the last good value, un-clobbered by the failed cycles
        assert cache.latest_price("AAPL") == 100.0
    finally:
        await poller.stop()
