"""Interface-conformance tests shared by both MarketDataProvider implementations.

See planning/MARKET_DATA_DESIGN.md §12/§14: written once, run against both
SimulatedProvider and MassiveProvider via pytest.mark.parametrize, so the
ABC in market/base.py is the actual spec rather than just documentation.

Massive responses are stubbed with method-only matchers (no URL/query
assertions) so these tests stay agnostic to the exact query-string encoding
of the outgoing request — that's covered precisely in test_massive.py.
"""
import pytest

from market.base import MarketDataProvider
from market.massive import MassiveProvider
from market.simulator import SimulatedProvider


def _stub_massive_snapshot(httpx_mock, tickers=()):
    httpx_mock.add_response(
        method="GET",
        json={"status": "OK", "count": len(tickers), "tickers": list(tickers)},
    )


@pytest.fixture(params=["simulator", "massive"])
def provider(request):
    if request.param == "simulator":
        return SimulatedProvider(seed=42)
    return MassiveProvider(api_key="test-key")


def test_provider_implements_the_abc(provider):
    assert isinstance(provider, MarketDataProvider)
    assert isinstance(provider.source, str) and provider.source
    assert provider.poll_interval > 0


@pytest.mark.asyncio
async def test_get_prices_skips_malformed_symbols_without_raising(provider, httpx_mock):
    if isinstance(provider, MassiveProvider):
        _stub_massive_snapshot(httpx_mock)  # upstream resolves nothing for these
    prices = await provider.get_prices(["", "123", "aa pl"])
    assert prices == {}
    await provider.aclose()


@pytest.mark.asyncio
async def test_get_prices_never_raises_on_partial_failure(provider, httpx_mock):
    if isinstance(provider, MassiveProvider):
        _stub_massive_snapshot(httpx_mock, tickers=[{"ticker": "AAPL", "prevDay": {"c": 190.0}}])
    # Whatever "NOTAREALTICKER" resolves to (nothing, for both providers),
    # the call must complete and return a dict, never raise.
    prices = await provider.get_prices(["AAPL", "NOTAREALTICKER"])
    assert isinstance(prices, dict)
    assert "NOTAREALTICKER" not in prices
    await provider.aclose()


@pytest.mark.asyncio
async def test_get_prices_result_keys_are_subset_of_requested_tickers(provider, httpx_mock):
    if isinstance(provider, MassiveProvider):
        _stub_massive_snapshot(httpx_mock, tickers=[{"ticker": "AAPL", "prevDay": {"c": 190.0}}])
    prices = await provider.get_prices(["AAPL"])
    assert set(prices).issubset({"AAPL"})
    await provider.aclose()


@pytest.mark.asyncio
async def test_start_and_aclose_do_not_raise(provider):
    await provider.start()
    await provider.aclose()
