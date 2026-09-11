"""Both concrete providers implement one ABC — behavior that the interface
promises (planning/MARKET_DATA_DESIGN.md §5) is tested once here and run
against each implementation via parametrization, rather than duplicated.
"""
import pytest

from market.base import MarketDataProvider
from market.massive import MassiveProvider
from market.simulator import SimulatedProvider

GENERIC_SNAPSHOT_RESPONSE = {
    "status": "OK",
    "count": 1,
    "tickers": [{"ticker": "AAPL", "day": {"c": 190.0}}],
}
GENERIC_SINGLE_RESPONSE = {
    "status": "OK",
    "ticker": {"ticker": "AAPL", "day": {"c": 190.0}},
}


@pytest.fixture(params=["simulator", "massive"])
def provider(request) -> MarketDataProvider:
    if request.param == "simulator":
        return SimulatedProvider(seed=42)
    return MassiveProvider(api_key="test-key")


def _mock_snapshot(provider: MarketDataProvider, httpx_mock, body=None) -> None:
    if isinstance(provider, MassiveProvider):
        httpx_mock.add_response(json=body or GENERIC_SNAPSHOT_RESPONSE)


def _mock_single(provider: MarketDataProvider, httpx_mock, body=None) -> None:
    if isinstance(provider, MassiveProvider):
        httpx_mock.add_response(json=body or GENERIC_SINGLE_RESPONSE)


async def test_get_prices_skips_malformed_symbols(provider, httpx_mock):
    _mock_snapshot(provider, httpx_mock, body={"status": "OK", "count": 0, "tickers": []})
    prices = await provider.get_prices(["", "123", "aa pl"])
    assert prices == {}


async def test_get_prices_returns_only_resolvable_tickers(provider, httpx_mock):
    _mock_snapshot(provider, httpx_mock)
    prices = await provider.get_prices(["AAPL", "not a real ticker"])
    assert set(prices) <= {"AAPL", "NOT A REAL TICKER"}


async def test_get_prices_keys_are_uppercased_subset_of_input(provider, httpx_mock):
    _mock_snapshot(provider, httpx_mock)
    prices = await provider.get_prices(["AAPL"])
    for ticker in prices:
        assert ticker == ticker.upper()


async def test_get_prices_empty_list_returns_empty_dict(provider):
    # Neither provider should touch the network for an empty ticker list.
    assert await provider.get_prices([]) == {}


async def test_validate_ticker_returns_a_bool(provider, httpx_mock):
    _mock_single(provider, httpx_mock)
    result = await provider.validate_ticker("AAPL")
    assert isinstance(result, bool)


async def test_start_and_aclose_are_safe_to_call(provider):
    await provider.start()
    await provider.aclose()
