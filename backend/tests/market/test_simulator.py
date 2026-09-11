from __future__ import annotations

import math
import random

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


# -- seed data ---------------------------------------------------------------


def test_deterministic_seed_price_pypl_matches_golden_value():
    # Golden value independently derived outside Python (this sandbox blocks
    # process execution, including `python3`): sha256("PYPL").hexdigest() =
    # a59e305d64206c904d841835ca4a85b7848ff2736704ec163224a61f48e34885
    # (verified via `sha256sum`). The first 8 bytes, big-endian, as an
    # unsigned 64-bit int (verified via bash `printf '%u'`) are
    # 11934029240248331408. frac = that / 2**64 ≈ 0.6469450214, so
    # price = round(15 + frac * 585, 2) = 393.46. Pins the hashing/byte-order
    # choice so it can't silently drift (e.g. to hash() or a different
    # endianness) without this test failing.
    assert deterministic_seed_price("PYPL") == 393.46


def test_deterministic_seed_price_is_stable_across_calls():
    assert deterministic_seed_price("PYPL") == deterministic_seed_price("PYPL")
    assert deterministic_seed_price("pypl") == deterministic_seed_price("PYPL")


def test_deterministic_seed_price_within_novel_ticker_range():
    for sym in ("PYPL", "XYZ", "Q", "ZZZZZZZZZZ"):
        price = deterministic_seed_price(sym)
        assert PRICE_FLOOR_SEED <= price <= PRICE_CEIL_SEED


def test_seed_for_curated_ticker_uses_curated_table():
    price, drift, vol = seed_for("AAPL")
    assert price == SEED_PRICES["AAPL"]
    assert drift == 0.0
    assert vol == DEFAULT_VOL


def test_seed_for_curated_ticker_with_override_params():
    price, drift, vol = seed_for("TSLA")
    assert price == SEED_PRICES["TSLA"]
    assert (drift, vol) == (0.0, 0.55)


def test_seed_for_novel_ticker_uses_deterministic_hash_and_default_params():
    price, drift, vol = seed_for("PYPL")
    assert price == deterministic_seed_price("PYPL")
    assert drift == DEFAULT_DRIFT
    assert vol == DEFAULT_VOL


def test_rng_for_is_reproducible_with_a_master_seed_and_random_without_one():
    r1 = rng_for("AAPL", master_seed=42)
    r2 = rng_for("AAPL", master_seed=42)
    assert r1.random() == r2.random()

    r3 = rng_for("AAPL", master_seed=None)
    r4 = rng_for("AAPL", master_seed=None)
    # Practically certain to differ (OS-entropy seeded); not a hard guarantee.
    assert r3.random() != r4.random()


# -- GBM step ------------------------------------------------------------


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
    st = TickerState("AAPL", 100.0, drift=0.0, volatility=0.3, rng=random.Random(1))
    first_updated_at = st.updated_at
    st.step()
    assert st.prev_price == 100.0
    assert st.updated_at >= first_updated_at


def test_step_price_never_reaches_or_crosses_the_floor():
    # Deliberately extreme volatility, many steps: the floor clamp must hold.
    st = TickerState("PENNY", 0.02, drift=0.0, volatility=8.0, rng=random.Random(7))
    for _ in range(20_000):
        st.step()
        assert st.price >= PRICE_FLOOR


def test_log_return_stdev_matches_sigma_sqrt_dt_within_tolerance():
    vol = 0.3
    st = TickerState("AAPL", 100.0, drift=0.0, volatility=vol, rng=random.Random(123))
    n = 50_000
    log_returns = []
    for _ in range(n):
        before = st.price
        st.step()
        log_returns.append(math.log(st.price / before))

    mean = sum(log_returns) / n
    variance = sum((r - mean) ** 2 for r in log_returns) / (n - 1)
    sample_std = math.sqrt(variance)

    expected = vol * math.sqrt(DT_YEARS)
    assert abs(sample_std - expected) / expected < 0.25


def test_two_tickers_same_params_diverge_under_one_master_seed():
    master_seed = 99
    a = TickerState("AAA", 100.0, drift=0.0, volatility=0.3, rng=rng_for("AAA", master_seed))
    b = TickerState("BBB", 100.0, drift=0.0, volatility=0.3, rng=rng_for("BBB", master_seed))

    for _ in range(50):
        a.step()
        b.step()

    assert a.price != b.price


def test_same_ticker_same_master_seed_is_reproducible():
    a = TickerState("AAA", 100.0, drift=0.0, volatility=0.3, rng=rng_for("AAA", 99))
    b = TickerState("AAA", 100.0, drift=0.0, volatility=0.3, rng=rng_for("AAA", 99))

    for _ in range(50):
        a.step()
        b.step()

    assert a.price == b.price


# -- SimulatedProvider ---------------------------------------------------


async def test_start_seeds_curated_tickers_at_exact_seed_prices_before_any_step():
    provider = SimulatedProvider(seed=1)
    await provider.start()
    try:
        prices = await provider.get_prices(list(SEED_PRICES))
        for ticker, expected in SEED_PRICES.items():
            assert prices[ticker].price == expected
    finally:
        await provider.aclose()


async def test_get_prices_lazily_registers_a_novel_ticker():
    provider = SimulatedProvider(seed=1)
    prices = await provider.get_prices(["pypl"])
    assert "PYPL" in prices
    assert prices["PYPL"].price == deterministic_seed_price("PYPL")


async def test_get_prices_skips_malformed_symbols():
    provider = SimulatedProvider(seed=1)
    prices = await provider.get_prices(["", "123", "aa pl", "this-symbol-is-way-too-long"])
    assert prices == {}


async def test_get_prices_uppercases_input():
    provider = SimulatedProvider(seed=1)
    prices = await provider.get_prices(["aapl"])
    assert "AAPL" in prices
    assert "aapl" not in prices


async def test_validate_ticker_accepts_well_formed_symbols():
    provider = SimulatedProvider(seed=1)
    assert await provider.validate_ticker("aapl") is True
    assert await provider.validate_ticker("BRK.B") is True
    assert await provider.validate_ticker("BF-B") is True


async def test_validate_ticker_rejects_malformed_symbols():
    provider = SimulatedProvider(seed=1)
    assert await provider.validate_ticker("") is False
    assert await provider.validate_ticker("123") is False
    assert await provider.validate_ticker("aa pl") is False


async def test_aclose_without_start_does_not_raise():
    provider = SimulatedProvider(seed=1)
    await provider.aclose()
