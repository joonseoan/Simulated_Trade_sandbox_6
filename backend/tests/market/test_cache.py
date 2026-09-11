from __future__ import annotations

from datetime import datetime, timezone

from market.cache import PriceCache
from market.types import PriceTick


def make_tick(ticker: str, price: float) -> PriceTick:
    return PriceTick(ticker=ticker, price=price, timestamp=datetime.now(timezone.utc))


def test_first_ingest_sets_previous_price_to_price():
    cache = PriceCache()
    cache.ingest(make_tick("AAPL", 100.0))
    entry = cache.snapshot()["AAPL"]
    assert entry.price == 100.0
    assert entry.previous_price == 100.0


def test_ingest_never_advances_previous_price_only_mark_emitted_does():
    cache = PriceCache()
    cache.ingest(make_tick("AAPL", 100.0))
    cache.ingest(make_tick("AAPL", 101.0))
    cache.ingest(make_tick("AAPL", 102.0))

    entry = cache.snapshot()["AAPL"]
    # previous_price reflects the last EMITTED value, not the last ingested
    # one — three ingests with no mark_emitted() in between must all leave
    # previous_price pinned at the very first value.
    assert entry.price == 102.0
    assert entry.previous_price == 100.0


def test_mark_emitted_advances_previous_price():
    cache = PriceCache()
    cache.ingest(make_tick("AAPL", 100.0))
    cache.mark_emitted("AAPL", 100.0)
    cache.ingest(make_tick("AAPL", 105.0))

    entry = cache.snapshot()["AAPL"]
    assert entry.price == 105.0
    assert entry.previous_price == 100.0


def test_mark_emitted_on_unknown_ticker_is_a_noop():
    cache = PriceCache()
    cache.mark_emitted("GHOST", 1.0)  # must not raise
    assert cache.known_tickers() == set()


def test_seed_missing_is_idempotent_and_does_not_clobber_existing_entry():
    cache = PriceCache()
    cache.ingest(make_tick("AAPL", 100.0))
    cache.mark_emitted("AAPL", 100.0)
    cache.ingest(make_tick("AAPL", 150.0))

    cache.seed_missing("AAPL", 1.0)  # already present — must be a no-op
    entry = cache.snapshot()["AAPL"]
    assert entry.price == 150.0
    assert entry.previous_price == 100.0


def test_seed_missing_registers_a_new_ticker_at_a_flat_baseline():
    cache = PriceCache()
    cache.seed_missing("PYPL", 42.0)
    entry = cache.snapshot()["PYPL"]
    assert entry.price == 42.0
    assert entry.previous_price == 42.0


def test_known_tickers_and_has():
    cache = PriceCache()
    assert cache.known_tickers() == set()
    assert cache.has("AAPL") is False

    cache.ingest(make_tick("AAPL", 100.0))
    assert cache.known_tickers() == {"AAPL"}
    assert cache.has("AAPL") is True


def test_latest_price_none_for_unknown_ticker():
    cache = PriceCache()
    assert cache.latest_price("GHOST") is None
    cache.ingest(make_tick("AAPL", 100.0))
    assert cache.latest_price("AAPL") == 100.0


def test_newest_update_age_ms_none_when_empty():
    cache = PriceCache()
    assert cache.newest_update_age_ms() is None


def test_newest_update_age_ms_non_negative_right_after_ingest():
    cache = PriceCache()
    cache.ingest(make_tick("AAPL", 100.0))
    age = cache.newest_update_age_ms()
    assert age is not None
    assert 0 <= age < 1000


def test_snapshot_is_a_defensive_copy():
    cache = PriceCache()
    cache.ingest(make_tick("AAPL", 100.0))
    snap = cache.snapshot()
    snap["AAPL"].price = 999.0
    assert cache.snapshot()["AAPL"].price == 100.0
