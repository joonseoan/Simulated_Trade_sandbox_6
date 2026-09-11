import hashlib
import math
import random
import statistics

import pytest

from market.simulator import (
    DEFAULT_DRIFT,
    DEFAULT_VOL,
    DT_YEARS,
    PRICE_CEIL_SEED,
    PRICE_FLOOR,
    PRICE_FLOOR_SEED,
    SEED_PRICES,
    SimulatedProvider,
    TickerState,
    deterministic_seed_price,
    rng_for,
    seed_for,
)


def test_step_sign_follows_the_gaussian_draw(monkeypatch):
    st = TickerState("AAPL", 100.0, drift=0.0, volatility=0.3, rng=random.Random(0))
    monkeypatch.setattr(st.rng, "gauss", lambda mu, sigma: 5.0)
    st.step()
    assert st.price > 100.0

    monkeypatch.setattr(st.rng, "gauss", lambda mu, sigma: -5.0)
    prev = st.price
    st.step()
    assert st.price < prev
    assert st.prev_price == prev


def test_step_updates_prev_price_and_updated_at():
    st = TickerState("AAPL", 100.0, drift=0.0, volatility=0.3, rng=random.Random(0))
    first_updated_at = st.updated_at
    st.step()
    assert st.prev_price == 100.0
    assert st.updated_at >= first_updated_at


def test_step_magnitude_matches_theoretical_stdev():
    rng = random.Random(12345)
    st = TickerState("AAPL", 100.0, drift=0.0, volatility=0.30, rng=rng)
    log_returns = []
    for _ in range(20_000):
        before = st.price
        st.step()
        log_returns.append(math.log(st.price / before))

    sample_stdev = statistics.stdev(log_returns)
    theoretical = 0.30 * math.sqrt(DT_YEARS)
    # Statistical test: allow generous relative tolerance to avoid flakiness.
    assert theoretical * 0.85 < sample_stdev < theoretical * 1.15


def test_two_tickers_same_params_diverge_under_one_master_seed():
    st_a = TickerState("AAA", 100.0, drift=0.0, volatility=0.3, rng=rng_for("AAA", master_seed=1))
    st_b = TickerState("BBB", 100.0, drift=0.0, volatility=0.3, rng=rng_for("BBB", master_seed=1))
    for _ in range(50):
        st_a.step()
        st_b.step()
    assert st_a.price != st_b.price


def test_rng_for_is_reproducible_with_a_fixed_master_seed():
    a1 = rng_for("AAPL", master_seed=42)
    a2 = rng_for("AAPL", master_seed=42)
    assert a1.gauss(0, 1) == a2.gauss(0, 1)


def test_rng_for_uses_entropy_when_master_seed_is_none():
    r1 = rng_for("AAPL", master_seed=None)
    r2 = rng_for("AAPL", master_seed=None)
    # Vanishingly unlikely to collide if each really draws fresh OS entropy.
    assert r1.random() != r2.random()


def test_no_negative_or_zero_prices_under_high_volatility():
    rng = random.Random(999)
    st = TickerState("WILD", 100.0, drift=0.0, volatility=3.0, rng=rng)
    for _ in range(100_000):
        st.step()
        assert st.price >= PRICE_FLOOR


def test_deterministic_seed_price_is_stable_and_matches_the_documented_formula():
    # Independent re-derivation of the documented formula (planning/MARKET_SIMULATOR.md §4.2),
    # computed directly from hashlib rather than by calling the function under test.
    digest = hashlib.sha256(b"PYPL").digest()
    frac = int.from_bytes(digest[:8], "big") / 2**64
    expected = round(PRICE_FLOOR_SEED + frac * (PRICE_CEIL_SEED - PRICE_FLOOR_SEED), 2)

    assert deterministic_seed_price("PYPL") == expected
    # Stable across repeated calls / case-insensitive.
    assert deterministic_seed_price("PYPL") == deterministic_seed_price("pypl")
    assert deterministic_seed_price("PYPL") == deterministic_seed_price("PYPL")


def test_deterministic_seed_price_is_within_the_documented_range():
    for symbol in ["PYPL", "XOM", "Z", "ZZZZZZZZZZ"]:
        price = deterministic_seed_price(symbol)
        assert PRICE_FLOOR_SEED <= price <= PRICE_CEIL_SEED


def test_seed_for_curated_ticker_uses_the_curated_table():
    price, drift, vol = seed_for("AAPL")
    assert price == SEED_PRICES["AAPL"]
    assert drift == 0.0


def test_seed_for_curated_ticker_with_override_uses_override_vol():
    price, drift, vol = seed_for("TSLA")
    assert price == SEED_PRICES["TSLA"]
    assert vol == 0.55


def test_seed_for_novel_ticker_uses_hash_seed_and_defaults():
    price, drift, vol = seed_for("PYPL")
    assert price == deterministic_seed_price("PYPL")
    assert drift == DEFAULT_DRIFT
    assert vol == DEFAULT_VOL


async def test_provider_reports_curated_seed_prices_exactly_before_any_step():
    provider = SimulatedProvider(poll_interval=1000.0, seed=1)
    await provider.start()
    try:
        prices = await provider.get_prices(list(SEED_PRICES))
        for ticker, expected_price in SEED_PRICES.items():
            assert prices[ticker].price == pytest.approx(expected_price)
    finally:
        await provider.aclose()


async def test_get_prices_lazily_materializes_a_novel_ticker():
    provider = SimulatedProvider(poll_interval=1000.0, seed=1)
    prices = await provider.get_prices(["PYPL"])
    assert prices["PYPL"].price == pytest.approx(deterministic_seed_price("PYPL"))
    assert "PYPL" in provider._states


async def test_get_prices_skips_malformed_symbols_without_raising():
    provider = SimulatedProvider(poll_interval=1000.0, seed=1)
    prices = await provider.get_prices(["", "123", "aa pl", "-BAD"])
    assert prices == {}


async def test_get_prices_uppercases_input_symbols():
    provider = SimulatedProvider(poll_interval=1000.0, seed=1)
    prices = await provider.get_prices(["aapl"])
    assert "AAPL" in prices


async def test_validate_ticker_accepts_well_formed_symbols():
    provider = SimulatedProvider()
    assert await provider.validate_ticker("aapl") is True
    assert await provider.validate_ticker("BRK.B") is True


async def test_validate_ticker_rejects_malformed_symbols():
    provider = SimulatedProvider()
    assert await provider.validate_ticker("") is False
    assert await provider.validate_ticker("123") is False
    assert await provider.validate_ticker("a a") is False


async def test_start_and_aclose_manage_the_background_task_lifecycle():
    provider = SimulatedProvider(poll_interval=0.01, seed=1)
    await provider.start()
    assert provider._task is not None
    await provider.aclose()
    assert provider._task.cancelled() or provider._task.done()
