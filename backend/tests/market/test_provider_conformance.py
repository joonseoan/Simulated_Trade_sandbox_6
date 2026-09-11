"""Interface-level behavior that both MarketDataProvider implementations must
satisfy (planning/MARKET_DATA_DESIGN.md §12). Each provider wires up its own
inputs (the simulator needs none; Massive needs an httpx_mock fixture) since
the two sources validate/report failures very differently under the hood —
but the observable contract from base.py is asserted identically for both.
"""

from __future__ import annotations

from market.massive import MassiveProvider
from market.simulator import SimulatedProvider

SNAPSHOT_URL = "https://api.massive.com/v2/snapshot/locale/us/markets/stocks/tickers"


async def test_simulator_get_prices_result_keys_are_subset_of_requested():
    provider = SimulatedProvider(seed=1)
    prices = await provider.get_prices(["AAPL", "MSFT"])
    assert set(prices) <= {"AAPL", "MSFT"}


async def test_massive_get_prices_result_keys_are_subset_of_requested(httpx_mock):
    httpx_mock.add_response(
        json={
            "status": "OK",
            "count": 1,
            "tickers": [{"ticker": "AAPL", "lastTrade": {"p": 190.0}}],
        }
    )
    provider = MassiveProvider(api_key="test-key")
    prices = await provider.get_prices(["AAPL", "MSFT"])
    assert set(prices) <= {"AAPL", "MSFT"}


async def test_simulator_get_prices_never_raises_on_an_unresolvable_symbol():
    provider = SimulatedProvider(seed=1)
    # every syntactically valid symbol is synthesizable, so "unresolvable"
    # for the simulator means malformed input — this must not raise either.
    prices = await provider.get_prices(["AAPL", "not a real ticker"])
    assert "AAPL" in prices


async def test_massive_get_prices_never_raises_on_an_unresolvable_symbol(httpx_mock):
    httpx_mock.add_response(
        json={
            "status": "OK",
            "count": 1,
            "tickers": [{"ticker": "AAPL", "lastTrade": {"p": 190.0}}],
        }
    )
    provider = MassiveProvider(api_key="test-key")
    prices = await provider.get_prices(["AAPL", "NOTAREALTICKER"])
    assert prices.keys() == {"AAPL"}


async def test_simulator_get_prices_never_raises_on_upstream_style_failure():
    # The simulator has no upstream to fail, but it must still tolerate a
    # completely empty/garbage input list without raising.
    provider = SimulatedProvider(seed=1)
    prices = await provider.get_prices([])
    assert prices == {}


async def test_massive_get_prices_never_raises_on_upstream_wide_failure(httpx_mock):
    httpx_mock.add_response(status_code=500)
    provider = MassiveProvider(api_key="test-key", poll_interval=1.0)
    prices = await provider.get_prices(["AAPL"])
    assert prices == {}


async def test_simulator_validate_ticker_backs_watchlist_add():
    provider = SimulatedProvider(seed=1)
    assert await provider.validate_ticker("AAPL") is True
    assert await provider.validate_ticker("not a real ticker") is False


async def test_massive_validate_ticker_backs_watchlist_add(httpx_mock):
    httpx_mock.add_response(json={"ticker": {"ticker": "AAPL", "lastTrade": {"p": 190.0}}})
    provider = MassiveProvider(api_key="test-key")
    assert await provider.validate_ticker("AAPL") is True


async def test_simulator_start_and_aclose_are_idempotent_enough_to_call_once():
    provider = SimulatedProvider(seed=1)
    await provider.start()
    await provider.aclose()


async def test_massive_start_and_aclose_are_idempotent_enough_to_call_once():
    provider = MassiveProvider(api_key="test-key")
    await provider.start()
    await provider.aclose()
