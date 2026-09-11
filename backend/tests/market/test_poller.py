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
        self.calls = 0
        self.raise_on_calls: set[int] = set()
        self.started = False
        self.closed = False
        self.received_ticker_sets: list[list[str]] = []

    async def start(self) -> None:
        self.started = True

    async def aclose(self) -> None:
        self.closed = True

    async def get_prices(self, tickers: list[str]) -> dict[str, PriceTick]:
        self.calls += 1
        self.received_ticker_sets.append(sorted(tickers))
        if self.calls in self.raise_on_calls:
            raise RuntimeError("simulated upstream failure")
        now = datetime.now(timezone.utc)
        return {t: PriceTick(t, 100.0 + self.calls, now) for t in tickers}

    async def validate_ticker(self, ticker: str) -> bool:
        return True


async def test_start_primes_cache_synchronously_before_periodic_task():
    provider = FakeProvider(poll_interval=10.0)  # long enough that only priming runs
    cache = PriceCache()
    poller = MarketPoller(provider, cache, tickers_supplier=lambda: ["AAPL"])

    await poller.start()
    try:
        assert provider.started is True
        assert provider.calls == 1
        assert cache.latest_price("AAPL") is not None
    finally:
        await poller.stop()
    assert provider.closed is True


async def test_poll_once_unions_watchlist_and_cache_keys():
    provider = FakeProvider(poll_interval=10.0)
    cache = PriceCache()
    cache.seed_missing("MSFT", 415.0)  # e.g. a held position dropped from the watchlist
    poller = MarketPoller(provider, cache, tickers_supplier=lambda: ["AAPL"])

    await poller._poll_once()

    assert provider.received_ticker_sets[-1] == ["AAPL", "MSFT"]
    assert cache.latest_price("AAPL") is not None
    assert cache.has("MSFT")


async def test_poll_once_is_a_noop_when_no_tickers_known():
    provider = FakeProvider()
    cache = PriceCache()
    poller = MarketPoller(provider, cache, tickers_supplier=lambda: [])

    await poller._poll_once()

    assert provider.calls == 0


async def test_run_swallows_provider_exceptions_and_keeps_polling():
    provider = FakeProvider(poll_interval=0.01)
    provider.raise_on_calls = {2}  # call #1 is the synchronous priming call
    cache = PriceCache()
    poller = MarketPoller(provider, cache, tickers_supplier=lambda: ["AAPL"])

    await poller.start()
    try:
        # Give the periodic loop several cycles to run past the failing one.
        for _ in range(50):
            if provider.calls >= 3:
                break
            await asyncio.sleep(0.01)
        assert provider.calls >= 3
        # The cache must still hold a value despite the failed cycle.
        assert cache.latest_price("AAPL") is not None
    finally:
        await poller.stop()
