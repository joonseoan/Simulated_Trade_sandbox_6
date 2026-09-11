import math
import random

import pytest

from market.simulator import (
    DEFAULT_DRIFT,
    DEFAULT_VOL,
    PRICE_CEIL_SEED,
    PRICE_FLOOR,
    PRICE_FLOOR_SEED,
    SEED_PARAMS,
    SEED_PRICES,
    SimulatedProvider,
    TickerState,
    deterministic_seed_price,
    rng_for,
    seed_for,
)


# ── GBM step mechanics ──────────────────────────────────────────────────────


def test_step_sign_follows_mocked_gauss_draw(monkeypatch):
    st = TickerState("AAPL", 100.0, drift=0.0, volatility=0.3, rng=random.Random(0))
    monkeypatch.setattr(st.rng, "gauss", lambda mu, sigma: 5.0)
    st.step()
    assert st.price > 100.0

    monkeypatch.setattr(st.rng, "gauss", lambda mu, sigma: -5.0)
    st.step()
    assert st.price < st.prev_price


def test_step_magnitude_matches_annualized_volatility():
    sigma = 0.3
    dt = 0.01
    n = 4000
    st = TickerState("TEST", 100.0, drift=0.0, volatility=sigma, rng=random.Random(42))

    log_returns = []
    for _ in range(n):
        prev = st.price
        st.step(dt=dt)
        log_returns.append(math.log(st.price / prev))

    mean = sum(log_returns) / n
    variance = sum((r - mean) ** 2 for r in log_returns) / n
    sample_std = math.sqrt(variance)
    expected = sigma * math.sqrt(dt)

    assert expected * 0.8 <= sample_std <= expected * 1.2


def test_price_never_goes_non_positive_under_high_volatility():
    st = TickerState("WILD", 10.0, drift=0.0, volatility=3.0, rng=random.Random(7))
    for _ in range(100_000):
        st.step(dt=0.001)
        assert st.price > 0
    assert st.price >= PRICE_FLOOR


def test_updated_at_advances_on_each_step():
    st = TickerState("AAPL", 100.0, drift=0.0, volatility=0.3, rng=random.Random(0))
    first = st.updated_at
    st.step()
    assert st.updated_at >= first


# ── RNG independence / determinism ──────────────────────────────────────────


def test_rng_for_is_deterministic_given_a_master_seed():
    a1 = rng_for("AAPL", master_seed=7)
    a2 = rng_for("AAPL", master_seed=7)
    assert [a1.gauss(0, 1) for _ in range(5)] == [a2.gauss(0, 1) for _ in range(5)]


def test_rng_for_is_independent_across_symbols_under_shared_master_seed():
    rng_a = rng_for("AAPL", master_seed=7)
    rng_b = rng_for("MSFT", master_seed=7)
    seq_a = [rng_a.gauss(0, 1) for _ in range(5)]
    seq_b = [rng_b.gauss(0, 1) for _ in range(5)]
    assert seq_a != seq_b


def test_rng_for_none_seed_uses_fresh_entropy():
    rng_a = rng_for("AAPL", master_seed=None)
    rng_b = rng_for("AAPL", master_seed=None)
    seq_a = [rng_a.gauss(0, 1) for _ in range(5)]
    seq_b = [rng_b.gauss(0, 1) for _ in range(5)]
    assert seq_a != seq_b


# ── Seed tables ──────────────────────────────────────────────────────────────


def test_deterministic_seed_price_golden_value():
    # Pins hashlib.sha256("PYPL") -> first 8 bytes -> uniform [0,1) -> [15,600].
    # Verified independently outside the test suite (sha256sum + manual
    # big-endian hex-fraction conversion); guards against silently swapping
    # the hash function, byte order, or scaling formula.
    assert deterministic_seed_price("PYPL") == 393.46


def test_deterministic_seed_price_is_stable_and_case_insensitive():
    first = deterministic_seed_price("PYPL")
    assert deterministic_seed_price("PYPL") == first
    assert deterministic_seed_price("pypl") == first


def test_deterministic_seed_price_within_bounds():
    for symbol in ("PYPL", "XYZ", "Q", "ZZZZ"):
        price = deterministic_seed_price(symbol)
        assert PRICE_FLOOR_SEED <= price <= PRICE_CEIL_SEED


def test_seed_for_curated_ticker_uses_table_values():
    price, drift, vol = seed_for("AAPL")
    assert price == SEED_PRICES["AAPL"]
    assert drift == DEFAULT_DRIFT
    assert vol == DEFAULT_VOL


def test_seed_for_curated_ticker_with_override_uses_override_params():
    price, drift, vol = seed_for("TSLA")
    assert price == SEED_PRICES["TSLA"]
    assert (drift, vol) == SEED_PARAMS["TSLA"]


def test_seed_for_novel_ticker_uses_deterministic_price_and_defaults():
    price, drift, vol = seed_for("PYPL")
    assert price == deterministic_seed_price("PYPL")
    assert drift == 0.0
    assert vol == DEFAULT_VOL
    assert PRICE_FLOOR_SEED <= price <= PRICE_CEIL_SEED


def test_seed_for_is_case_insensitive():
    assert seed_for("aapl") == seed_for("AAPL")


# ── SimulatedProvider ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_seeds_curated_prices_exactly():
    provider = SimulatedProvider(seed=1)
    await provider.start()
    try:
        prices = await provider.get_prices(list(SEED_PRICES))
        for ticker, expected in SEED_PRICES.items():
            assert prices[ticker].price == expected
    finally:
        await provider.aclose()


@pytest.mark.asyncio
async def test_get_prices_lazily_materializes_novel_ticker():
    provider = SimulatedProvider(seed=1)
    prices = await provider.get_prices(["PYPL"])
    assert "PYPL" in prices
    assert prices["PYPL"].price == deterministic_seed_price("PYPL")
    assert "PYPL" in provider._states


@pytest.mark.asyncio
async def test_get_prices_skips_malformed_symbols():
    provider = SimulatedProvider(seed=1)
    prices = await provider.get_prices(["", "123", "aa pl"])
    assert prices == {}


@pytest.mark.asyncio
async def test_get_prices_is_case_insensitive_and_uppercases_keys():
    provider = SimulatedProvider(seed=1)
    prices = await provider.get_prices(["aapl"])
    assert "AAPL" in prices


@pytest.mark.asyncio
async def test_validate_ticker_accepts_well_formed_symbols():
    provider = SimulatedProvider(seed=1)
    assert await provider.validate_ticker("AAPL") is True
    assert await provider.validate_ticker("pypl") is True


@pytest.mark.asyncio
async def test_validate_ticker_rejects_malformed_symbols():
    provider = SimulatedProvider(seed=1)
    assert await provider.validate_ticker("") is False
    assert await provider.validate_ticker("123") is False
    assert await provider.validate_ticker("aa pl") is False


@pytest.mark.asyncio
async def test_aclose_without_start_does_not_raise():
    provider = SimulatedProvider(seed=1)
    await provider.aclose()
