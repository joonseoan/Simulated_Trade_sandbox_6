from datetime import datetime, timezone

from market.cache import PriceCache
from market.types import PriceTick


def _tick(ticker: str, price: float, ts: datetime | None = None) -> PriceTick:
    return PriceTick(ticker, price, ts or datetime.now(timezone.utc))


def test_empty_cache_has_no_known_tickers_and_no_age():
    cache = PriceCache()
    assert cache.known_tickers() == set()
    assert cache.has("AAPL") is False
    assert cache.latest_price("AAPL") is None
    assert cache.newest_update_age_ms() is None


def test_ingest_sets_previous_price_to_current_on_first_write():
    cache = PriceCache()
    cache.ingest(_tick("AAPL", 100.0))
    entry = cache.snapshot()["AAPL"]
    assert entry.price == 100.0
    assert entry.previous_price == 100.0


def test_ingest_preserves_previous_price_across_writes_until_marked_emitted():
    cache = PriceCache()
    cache.ingest(_tick("AAPL", 100.0))
    cache.ingest(_tick("AAPL", 101.0))
    cache.ingest(_tick("AAPL", 102.0))
    entry = cache.snapshot()["AAPL"]
    # previous_price only advances via mark_emitted, never via ingest.
    assert entry.price == 102.0
    assert entry.previous_price == 100.0


def test_mark_emitted_advances_previous_price():
    cache = PriceCache()
    cache.ingest(_tick("AAPL", 100.0))
    cache.mark_emitted("AAPL", 100.0)
    cache.ingest(_tick("AAPL", 105.0))
    entry = cache.snapshot()["AAPL"]
    assert entry.previous_price == 100.0
    cache.mark_emitted("AAPL", entry.price)
    assert cache.snapshot()["AAPL"].previous_price == 105.0


def test_mark_emitted_on_unknown_ticker_is_a_noop():
    cache = PriceCache()
    cache.mark_emitted("NOPE", 1.0)  # must not raise
    assert cache.known_tickers() == set()


def test_seed_missing_is_idempotent():
    cache = PriceCache()
    cache.seed_missing("AAPL", 100.0)
    cache.ingest(_tick("AAPL", 150.0))
    cache.seed_missing("AAPL", 999.0)  # must not clobber an existing entry
    assert cache.latest_price("AAPL") == 150.0


def test_seed_missing_registers_a_new_ticker():
    cache = PriceCache()
    cache.seed_missing("PYPL", 55.0)
    entry = cache.snapshot()["PYPL"]
    assert entry.price == 55.0
    assert entry.previous_price == 55.0
    assert "PYPL" in cache.known_tickers()


def test_snapshot_returns_defensive_copies():
    cache = PriceCache()
    cache.ingest(_tick("AAPL", 100.0))
    snap1 = cache.snapshot()
    snap1["AAPL"].price = 999.0
    snap2 = cache.snapshot()
    assert snap2["AAPL"].price == 100.0


def test_newest_update_age_ms_is_non_negative_after_ingest():
    cache = PriceCache()
    cache.ingest(_tick("AAPL", 100.0))
    age = cache.newest_update_age_ms()
    assert age is not None
    assert age >= 0
