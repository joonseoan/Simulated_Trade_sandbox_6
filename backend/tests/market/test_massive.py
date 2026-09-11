import asyncio
from datetime import datetime, timezone

import httpx
import pytest

from market.massive import MassiveProvider, extract_price, ns_to_dt

BASE_URL = "https://api.massive.com"
SNAPSHOT_URL = f"{BASE_URL}/v2/snapshot/locale/us/markets/stocks/tickers?tickers=AAPL"
SINGLE_URL = f"{BASE_URL}/v2/snapshot/locale/us/markets/stocks/tickers/AAPL"


# ── extract_price fallback chain ────────────────────────────────────────────

def test_extract_price_prefers_last_trade():
    t = {
        "lastTrade": {"p": 190.72},
        "min": {"c": 190.5},
        "day": {"c": 189.5},
        "prevDay": {"c": 187.1},
    }
    assert extract_price(t) == 190.72


def test_extract_price_falls_back_to_min_close_when_last_trade_missing():
    t = {"min": {"c": 190.5}, "day": {"c": 189.5}, "prevDay": {"c": 187.1}}
    assert extract_price(t) == 190.5


def test_extract_price_falls_back_to_day_close():
    t = {"day": {"c": 189.5}, "prevDay": {"c": 187.1}}
    assert extract_price(t) == 189.5


def test_extract_price_falls_back_to_prev_day_close():
    t = {"prevDay": {"c": 187.1}}
    assert extract_price(t) == 187.1


def test_extract_price_treats_zero_as_missing_and_falls_through():
    t = {
        "lastTrade": {"p": 0.0},
        "min": {"c": 0},
        "day": {"c": 0.0},
        "prevDay": {"c": 187.1},
    }
    assert extract_price(t) == 187.1


def test_extract_price_returns_none_when_nothing_usable():
    assert extract_price({}) is None
    assert extract_price({"lastTrade": {"p": None}, "day": {}}) is None


def test_ns_to_dt_converts_nanoseconds_to_utc():
    dt = ns_to_dt(1_739_210_399_900_000_000)
    assert dt.tzinfo == timezone.utc
    assert dt == datetime.fromtimestamp(1_739_210_399_900_000_000 / 1e9, tz=timezone.utc)


# ── get_prices ───────────────────────────────────────────────────────────

async def test_get_prices_empty_input_short_circuits_without_http_call(httpx_mock):
    provider = MassiveProvider(api_key="test-key")
    try:
        result = await provider.get_prices([])
        assert result == {}
        assert len(httpx_mock.get_requests()) == 0
    finally:
        await provider.aclose()


async def test_get_prices_parses_snapshot_response(httpx_mock):
    httpx_mock.add_response(
        url=SNAPSHOT_URL,
        json={
            "status": "OK",
            "count": 1,
            "tickers": [{
                "ticker": "AAPL",
                "lastTrade": {"p": 190.72, "t": 1739210399900000000},
                "day": {"c": 189.5},
                "prevDay": {"c": 187.1},
                "updated": 1739210400000000000,
            }],
        },
    )
    provider = MassiveProvider(api_key="test-key")
    try:
        prices = await provider.get_prices(["AAPL"])
        assert prices["AAPL"].price == 190.72
        assert prices["AAPL"].ticker == "AAPL"
        assert prices["AAPL"].timestamp == ns_to_dt(1739210400000000000)
    finally:
        await provider.aclose()


async def test_get_prices_skips_tickers_with_no_usable_price(httpx_mock):
    httpx_mock.add_response(
        url=SNAPSHOT_URL,
        json={"status": "OK", "count": 1, "tickers": [{"ticker": "AAPL"}]},
    )
    provider = MassiveProvider(api_key="test-key")
    try:
        prices = await provider.get_prices(["AAPL"])
        assert prices == {}
    finally:
        await provider.aclose()


async def test_get_prices_uses_bearer_auth_header(httpx_mock):
    httpx_mock.add_response(
        url=SNAPSHOT_URL,
        json={"status": "OK", "count": 0, "tickers": []},
    )
    provider = MassiveProvider(api_key="secret-key")
    try:
        await provider.get_prices(["AAPL"])
        request = httpx_mock.get_requests()[0]
        assert request.headers["Authorization"] == "Bearer secret-key"
        assert "apiKey" not in str(request.url)
    finally:
        await provider.aclose()


async def test_get_prices_returns_empty_on_5xx_without_raising(httpx_mock):
    httpx_mock.add_response(url=SNAPSHOT_URL, status_code=500)
    provider = MassiveProvider(api_key="test-key")
    try:
        prices = await provider.get_prices(["AAPL"])
        assert prices == {}
    finally:
        await provider.aclose()


async def test_get_prices_returns_empty_on_transport_error_without_raising(httpx_mock):
    httpx_mock.add_exception(httpx.ConnectError("boom"), url=SNAPSHOT_URL)
    provider = MassiveProvider(api_key="test-key")
    try:
        prices = await provider.get_prices(["AAPL"])
        assert prices == {}
    finally:
        await provider.aclose()


async def test_get_prices_returns_empty_on_auth_error(httpx_mock):
    httpx_mock.add_response(url=SNAPSHOT_URL, status_code=403)
    provider = MassiveProvider(api_key="bad-key")
    try:
        prices = await provider.get_prices(["AAPL"])
        assert prices == {}
    finally:
        await provider.aclose()


async def test_get_prices_backs_off_after_429_honoring_retry_after(httpx_mock):
    httpx_mock.add_response(
        url=SNAPSHOT_URL, status_code=429, headers={"Retry-After": "5"},
    )
    provider = MassiveProvider(api_key="test-key", poll_interval=15.0)
    try:
        prices = await provider.get_prices(["AAPL"])
        assert prices == {}

        loop = asyncio.get_running_loop()
        assert provider._backoff_until > loop.time()
        assert provider._backoff_until <= loop.time() + 5.5

        # Still backing off — must not make a second HTTP request.
        prices_again = await provider.get_prices(["AAPL"])
        assert prices_again == {}
        assert len(httpx_mock.get_requests()) == 1
    finally:
        await provider.aclose()


async def test_get_prices_backoff_falls_back_to_poll_interval_when_header_missing(httpx_mock):
    httpx_mock.add_response(url=SNAPSHOT_URL, status_code=429)
    provider = MassiveProvider(api_key="test-key", poll_interval=15.0)
    try:
        loop = asyncio.get_running_loop()
        before = loop.time()
        await provider.get_prices(["AAPL"])
        assert provider._backoff_until == pytest.approx(before + 15.0, abs=1.0)
    finally:
        await provider.aclose()


async def test_get_prices_backoff_capped_at_max_backoff_seconds(httpx_mock):
    httpx_mock.add_response(
        url=SNAPSHOT_URL, status_code=429, headers={"Retry-After": "99999"},
    )
    provider = MassiveProvider(api_key="test-key", poll_interval=15.0)
    try:
        loop = asyncio.get_running_loop()
        before = loop.time()
        await provider.get_prices(["AAPL"])
        assert provider._backoff_until <= before + 60.0 + 1.0
    finally:
        await provider.aclose()


# ── validate_ticker ─────────────────────────────────────────────────────

async def test_validate_ticker_true_when_price_resolvable(httpx_mock):
    httpx_mock.add_response(
        url=SINGLE_URL,
        json={"status": "OK", "ticker": {"ticker": "AAPL", "day": {"c": 190.0}}},
    )
    provider = MassiveProvider(api_key="test-key")
    try:
        assert await provider.validate_ticker("aapl") is True
    finally:
        await provider.aclose()


async def test_validate_ticker_false_on_404(httpx_mock):
    httpx_mock.add_response(url=SINGLE_URL, status_code=404)
    provider = MassiveProvider(api_key="test-key")
    try:
        assert await provider.validate_ticker("AAPL") is False
    finally:
        await provider.aclose()


async def test_validate_ticker_false_on_transport_error_fail_closed(httpx_mock):
    httpx_mock.add_exception(httpx.ConnectError("boom"), url=SINGLE_URL)
    provider = MassiveProvider(api_key="test-key")
    try:
        assert await provider.validate_ticker("AAPL") is False
    finally:
        await provider.aclose()


async def test_validate_ticker_false_when_no_price_extractable(httpx_mock):
    httpx_mock.add_response(
        url=SINGLE_URL,
        json={"status": "OK", "ticker": {"ticker": "AAPL"}},
    )
    provider = MassiveProvider(api_key="test-key")
    try:
        assert await provider.validate_ticker("AAPL") is False
    finally:
        await provider.aclose()


async def test_validate_ticker_false_on_unexpected_status(httpx_mock):
    httpx_mock.add_response(url=SINGLE_URL, status_code=500)
    provider = MassiveProvider(api_key="test-key")
    try:
        assert await provider.validate_ticker("AAPL") is False
    finally:
        await provider.aclose()
