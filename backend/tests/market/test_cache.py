from datetime import datetime, timezone

from market.cache import PriceCache
from market.types import PriceTick


def test_empty_cache_has_no_known_tickers():
    cache = PriceCache()
    assert cache.known_tickers() == set()
    assert cache.has("AAPL") is False
    assert cache.latest_price("AAPL") is None


def test_newest_update_age_ms_is_none_when_empty():
    cache = PriceCache()
    assert cache.newest_update_age_ms() is None


def test_ingest_first_write_sets_previous_price_to_price():
    cache = PriceCache()
    cache.ingest(PriceTick("AAPL", 190.0, datetime.now(timezone.utc)))
    entry = cache.snapshot()["AAPL"]
    assert entry.price == 190.0
    assert entry.previous_price == 190.0


def test_ingest_preserves_previous_price_across_writes_until_emitted():
    cache = PriceCache()
    cache.ingest(PriceTick("AAPL", 190.0, datetime.now(timezone.utc)))
    # A second ingest (new provider observation) must NOT move previous_price —
    # only mark_emitted (the hub) is allowed to do that.
    cache.ingest(PriceTick("AAPL", 191.0, datetime.now(timezone.utc)))
    entry = cache.snapshot()["AAPL"]
    assert entry.price == 191.0
    assert entry.previous_price == 190.0


def test_mark_emitted_advances_previous_price():
    cache = PriceCache()
    cache.ingest(PriceTick("AAPL", 190.0, datetime.now(timezone.utc)))
    cache.mark_emitted("AAPL", 190.0)
    cache.ingest(PriceTick("AAPL", 191.0, datetime.now(timezone.utc)))
    entry = cache.snapshot()["AAPL"]
    assert entry.previous_price == 190.0  # value emitted on the prior tick


def test_mark_emitted_on_unknown_ticker_is_a_noop():
    cache = PriceCache()
    cache.mark_emitted("NOPE", 100.0)  # must not raise
    assert cache.known_tickers() == set()


def test_seed_missing_registers_a_new_ticker():
    cache = PriceCache()
    cache.seed_missing("MSFT", 415.0)
    entry = cache.snapshot()["MSFT"]
    assert entry.price == 415.0
    assert entry.previous_price == 415.0
    assert cache.has("MSFT")


def test_seed_missing_is_idempotent_and_never_overwrites_existing_entry():
    cache = PriceCache()
    cache.ingest(PriceTick("AAPL", 190.0, datetime.now(timezone.utc)))
    cache.mark_emitted("AAPL", 190.0)
    cache.ingest(PriceTick("AAPL", 195.0, datetime.now(timezone.utc)))
    cache.seed_missing("AAPL", 1.0)  # must not clobber the real, already-cached entry
    entry = cache.snapshot()["AAPL"]
    assert entry.price == 195.0
    assert entry.previous_price == 190.0


def test_snapshot_is_a_defensive_copy():
    cache = PriceCache()
    cache.ingest(PriceTick("AAPL", 190.0, datetime.now(timezone.utc)))
    snap = cache.snapshot()
    snap["AAPL"].price = 0.0
    assert cache.latest_price("AAPL") == 190.0


def test_newest_update_age_ms_is_non_negative_and_small_immediately_after_ingest():
    cache = PriceCache()
    cache.ingest(PriceTick("AAPL", 190.0, datetime.now(timezone.utc)))
    age = cache.newest_update_age_ms()
    assert age is not None
    assert 0 <= age < 1000


def test_newest_update_age_ms_reflects_the_freshest_entry():
    cache = PriceCache()
    old_time = datetime.now(timezone.utc)
    cache.ingest(PriceTick("AAPL", 190.0, old_time))
    cache.ingest(PriceTick("MSFT", 415.0, old_time))
    age_before = cache.newest_update_age_ms()
    # Re-ingesting AAPL should refresh updated_at and keep age from growing
    # relative to the previous reading (grows monotonically otherwise).
    cache.ingest(PriceTick("AAPL", 191.0, old_time))
    age_after = cache.newest_update_age_ms()
    assert age_after is not None and age_before is not None
    assert age_after <= age_before + 5  # allow small scheduling jitter
