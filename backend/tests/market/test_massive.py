import asyncio

import httpx
import pytest

from market.massive import MassiveProvider, extract_price, ns_to_dt

SNAPSHOT_URL = "https://api.massive.com/v2/snapshot/locale/us/markets/stocks/tickers?tickers=AAPL"


def _snapshot_body(*tickers: dict) -> dict:
    return {"status": "OK", "count": len(tickers), "tickers": list(tickers)}


# ── extract_price fallback chain ────────────────────────────────────────────


def test_extract_price_prefers_last_trade():
    t = {"lastTrade": {"p": 190.72}, "min": {"c": 189.9}, "day": {"c": 189.5}, "prevDay": {"c": 187.1}}
    assert extract_price(t) == 190.72


def test_extract_price_falls_back_to_min_close_when_no_last_trade():
    t = {"min": {"c": 189.9}, "day": {"c": 189.5}, "prevDay": {"c": 187.1}}
    assert extract_price(t) == 189.9


def test_extract_price_falls_back_to_day_close():
    t = {"day": {"c": 189.5}, "prevDay": {"c": 187.1}}
    assert extract_price(t) == 189.5


def test_extract_price_falls_back_to_prev_day_close():
    t = {"prevDay": {"c": 187.1}}
    assert extract_price(t) == 187.1


def test_extract_price_zero_value_falls_through_to_next_field():
    t = {"lastTrade": {"p": 0.0}, "min": {"c": 0}, "day": {"c": 189.5}, "prevDay": {"c": 187.1}}
    assert extract_price(t) == 189.5


def test_extract_price_missing_field_falls_through():
    t = {"lastTrade": {}, "min": {"c": None}, "prevDay": {"c": 187.1}}
    assert extract_price(t) == 187.1


def test_extract_price_returns_none_when_nothing_usable():
    assert extract_price({}) is None
    assert extract_price({"lastTrade": {"p": 0.0}, "prevDay": {"c": 0}}) is None


def test_ns_to_dt_converts_nanoseconds_to_utc_datetime():
    # 2025-02-10T18:00:00Z in nanoseconds since epoch.
    dt = ns_to_dt(1739210400000000000)
    assert dt.isoformat().replace("+00:00", "Z") == "2025-02-10T18:00:00Z"


# ── MassiveProvider.get_prices ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_prices_empty_list_short_circuits_without_http_call(httpx_mock):
    provider = MassiveProvider(api_key="test-key")
    prices = await provider.get_prices([])
    assert prices == {}
    await provider.aclose()


@pytest.mark.asyncio
async def test_get_prices_parses_snapshot_response(httpx_mock):
    httpx_mock.add_response(
        url=SNAPSHOT_URL,
        json=_snapshot_body(
            {
                "ticker": "AAPL",
                "lastTrade": {"p": 190.72, "t": 1739210399900000000},
                "day": {"c": 189.5},
                "prevDay": {"c": 187.1},
                "updated": 1739210400000000000,
            }
        ),
    )
    provider = MassiveProvider(api_key="test-key")
    prices = await provider.get_prices(["AAPL"])
    assert prices["AAPL"].price == 190.72
    assert prices["AAPL"].ticker == "AAPL"
    await provider.aclose()


@pytest.mark.asyncio
async def test_get_prices_sends_one_request_for_the_whole_watchlist(httpx_mock):
    httpx_mock.add_response(
        method="GET",
        json=_snapshot_body(
            {"ticker": "AAPL", "prevDay": {"c": 190.0}},
            {"ticker": "MSFT", "prevDay": {"c": 400.0}},
            {"ticker": "TSLA", "prevDay": {"c": 250.0}},
        ),
    )
    provider = MassiveProvider(api_key="test-key")
    prices = await provider.get_prices(["tsla", "aapl", "msft"])
    assert set(prices) == {"AAPL", "MSFT", "TSLA"}

    # One request for the whole watchlist, not one per ticker (MASSIVE_API.md §2).
    requests = httpx_mock.get_requests()
    assert len(requests) == 1
    requested_tickers = requests[0].url.params["tickers"]
    assert set(requested_tickers.split(",")) == {"AAPL", "MSFT", "TSLA"}
    await provider.aclose()


@pytest.mark.asyncio
async def test_get_prices_skips_tickers_with_no_usable_price(httpx_mock):
    httpx_mock.add_response(
        url=SNAPSHOT_URL,
        json=_snapshot_body({"ticker": "AAPL"}),
    )
    provider = MassiveProvider(api_key="test-key")
    prices = await provider.get_prices(["AAPL"])
    assert prices == {}
    await provider.aclose()


@pytest.mark.asyncio
async def test_get_prices_returns_empty_on_transport_error(httpx_mock):
    httpx_mock.add_exception(httpx.ConnectError("connection refused"))
    provider = MassiveProvider(api_key="test-key")
    prices = await provider.get_prices(["AAPL"])
    assert prices == {}
    await provider.aclose()


@pytest.mark.asyncio
async def test_get_prices_returns_empty_on_5xx(httpx_mock):
    httpx_mock.add_response(url=SNAPSHOT_URL, status_code=503)
    provider = MassiveProvider(api_key="test-key")
    prices = await provider.get_prices(["AAPL"])
    assert prices == {}
    await provider.aclose()


@pytest.mark.asyncio
async def test_get_prices_returns_empty_on_auth_error(httpx_mock):
    httpx_mock.add_response(url=SNAPSHOT_URL, status_code=401)
    provider = MassiveProvider(api_key="bad-key")
    prices = await provider.get_prices(["AAPL"])
    assert prices == {}
    await provider.aclose()


@pytest.mark.asyncio
async def test_429_enters_backoff_honoring_retry_after_and_skips_next_call(httpx_mock):
    httpx_mock.add_response(url=SNAPSHOT_URL, status_code=429, headers={"Retry-After": "5"})
    provider = MassiveProvider(api_key="test-key", poll_interval=15.0)

    loop = asyncio.get_running_loop()
    before = loop.time()
    prices = await provider.get_prices(["AAPL"])
    assert prices == {}
    assert 4.0 <= provider._backoff_until - before <= 6.0

    # Still backing off: get_prices must NOT issue a second HTTP request.
    # (httpx_mock would raise if an unmatched request were sent.)
    prices_again = await provider.get_prices(["AAPL"])
    assert prices_again == {}
    await provider.aclose()


@pytest.mark.asyncio
async def test_429_without_retry_after_falls_back_to_poll_interval(httpx_mock):
    httpx_mock.add_response(url=SNAPSHOT_URL, status_code=429)
    provider = MassiveProvider(api_key="test-key", poll_interval=20.0)

    loop = asyncio.get_running_loop()
    before = loop.time()
    await provider.get_prices(["AAPL"])
    assert 19.0 <= provider._backoff_until - before <= 21.0
    await provider.aclose()


@pytest.mark.asyncio
async def test_backoff_is_capped_at_max_backoff_seconds(httpx_mock):
    httpx_mock.add_response(url=SNAPSHOT_URL, status_code=429, headers={"Retry-After": "999"})
    provider = MassiveProvider(api_key="test-key")

    loop = asyncio.get_running_loop()
    before = loop.time()
    await provider.get_prices(["AAPL"])
    assert 58.0 <= provider._backoff_until - before <= 61.0
    await provider.aclose()


# ── MassiveProvider.validate_ticker ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_validate_ticker_true_when_price_resolvable(httpx_mock):
    httpx_mock.add_response(
        url="https://api.massive.com/v2/snapshot/locale/us/markets/stocks/tickers/AAPL",
        json={"status": "OK", "ticker": {"prevDay": {"c": 190.0}}},
    )
    provider = MassiveProvider(api_key="test-key")
    assert await provider.validate_ticker("aapl") is True
    await provider.aclose()


@pytest.mark.asyncio
async def test_validate_ticker_false_on_404(httpx_mock):
    httpx_mock.add_response(
        url="https://api.massive.com/v2/snapshot/locale/us/markets/stocks/tickers/NOTREAL",
        status_code=404,
    )
    provider = MassiveProvider(api_key="test-key")
    assert await provider.validate_ticker("NOTREAL") is False
    await provider.aclose()


@pytest.mark.asyncio
async def test_validate_ticker_false_on_unexpected_status(httpx_mock):
    httpx_mock.add_response(
        url="https://api.massive.com/v2/snapshot/locale/us/markets/stocks/tickers/AAPL",
        status_code=500,
    )
    provider = MassiveProvider(api_key="test-key")
    assert await provider.validate_ticker("AAPL") is False
    await provider.aclose()


@pytest.mark.asyncio
async def test_validate_ticker_fails_closed_on_transport_error(httpx_mock):
    httpx_mock.add_exception(httpx.ConnectError("connection refused"))
    provider = MassiveProvider(api_key="test-key")
    assert await provider.validate_ticker("AAPL") is False
    await provider.aclose()
