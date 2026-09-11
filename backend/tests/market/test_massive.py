from __future__ import annotations

import httpx
import pytest

from market.massive import MassiveProvider, extract_price, ns_to_dt


def make_provider(poll_interval: float = 15.0) -> MassiveProvider:
    return MassiveProvider(api_key="test-key", poll_interval=poll_interval)


# -- extract_price -------------------------------------------------------


def test_extract_price_prefers_last_trade():
    obj = {
        "lastTrade": {"p": 190.72},
        "min": {"c": 190.0},
        "day": {"c": 189.5},
        "prevDay": {"c": 187.1},
    }
    assert extract_price(obj) == 190.72


@pytest.mark.parametrize(
    ("obj", "expected"),
    [
        ({"min": {"c": 190.0}, "day": {"c": 189.5}, "prevDay": {"c": 187.1}}, 190.0),
        ({"day": {"c": 189.5}, "prevDay": {"c": 187.1}}, 189.5),
        ({"prevDay": {"c": 187.1}}, 187.1),
        ({}, None),
    ],
)
def test_extract_price_falls_through_the_chain(obj, expected):
    assert extract_price(obj) == expected


def test_extract_price_treats_zero_and_missing_as_unusable_and_falls_through():
    obj = {
        "lastTrade": {"p": 0.0},  # present but falsy — must fall through
        "min": {},  # key present, field missing — must fall through
        "day": {"c": None},  # explicit null — must fall through
        "prevDay": {"c": 187.1},
    }
    assert extract_price(obj) == 187.1


def test_extract_price_handles_explicit_null_section_without_raising():
    # A section can be `null` rather than absent — must not AttributeError.
    obj = {"lastTrade": None, "min": None, "day": None, "prevDay": {"c": 187.1}}
    assert extract_price(obj) == 187.1


def test_extract_price_all_sections_unusable_returns_none():
    assert extract_price({"lastTrade": {"p": 0}, "min": {"c": None}}) is None


def test_ns_to_dt_converts_nanoseconds_to_utc_datetime():
    dt = ns_to_dt(1_739_210_399_900_000_000)
    assert dt.tzinfo is not None
    assert dt.utcoffset().total_seconds() == 0


# -- get_prices ------------------------------------------------------------


async def test_get_prices_empty_list_short_circuits_without_a_request(httpx_mock):
    provider = make_provider()
    prices = await provider.get_prices([])
    assert prices == {}
    assert httpx_mock.get_requests() == []


async def test_get_prices_success_parses_snapshot_response(httpx_mock):
    httpx_mock.add_response(
        json={
            "status": "OK",
            "count": 1,
            "tickers": [
                {
                    "ticker": "AAPL",
                    "lastTrade": {"p": 190.72, "t": 1739210399900000000},
                    "updated": 1739210399900000000,
                    "day": {"c": 189.5},
                    "prevDay": {"c": 187.1},
                }
            ],
        }
    )
    provider = make_provider()
    prices = await provider.get_prices(["aapl"])

    assert prices["AAPL"].price == 190.72
    assert prices["AAPL"].ticker == "AAPL"

    [request] = httpx_mock.get_requests()
    assert request.method == "GET"
    assert request.url.params.get("tickers") == "AAPL"
    assert request.headers["authorization"] == "Bearer test-key"


async def test_get_prices_sends_one_request_for_the_whole_sorted_deduped_watchlist(httpx_mock):
    httpx_mock.add_response(json={"status": "OK", "count": 0, "tickers": []})
    provider = make_provider()
    await provider.get_prices(["msft", "AAPL", "aapl"])

    [request] = httpx_mock.get_requests()
    assert request.url.params.get("tickers") == "AAPL,MSFT"


async def test_get_prices_skips_tickers_with_no_usable_price_field(httpx_mock):
    httpx_mock.add_response(
        json={
            "status": "OK",
            "count": 2,
            "tickers": [
                {"ticker": "AAPL", "lastTrade": {"p": 190.0}},
                {"ticker": "GHOST"},  # nothing usable anywhere
            ],
        }
    )
    provider = make_provider()
    prices = await provider.get_prices(["AAPL", "GHOST"])
    assert set(prices) == {"AAPL"}


async def test_get_prices_429_enters_backoff_honoring_retry_after(httpx_mock):
    httpx_mock.add_response(status_code=429, headers={"Retry-After": "30"})
    provider = make_provider(poll_interval=1.0)

    prices = await provider.get_prices(["AAPL"])
    assert prices == {}

    # A second call while still backing off must not hit the network at all —
    # only one response was registered, so a second real request would fail
    # the test (no matching mock).
    prices_again = await provider.get_prices(["AAPL"])
    assert prices_again == {}
    assert len(httpx_mock.get_requests()) == 1


async def test_get_prices_429_without_retry_after_falls_back_to_poll_interval(httpx_mock):
    httpx_mock.add_response(status_code=429)
    provider = make_provider(poll_interval=1.0)

    prices = await provider.get_prices(["AAPL"])
    assert prices == {}
    prices_again = await provider.get_prices(["AAPL"])
    assert prices_again == {}
    assert len(httpx_mock.get_requests()) == 1


async def test_get_prices_5xx_returns_empty_and_enters_backoff(httpx_mock):
    httpx_mock.add_response(status_code=503)
    provider = make_provider(poll_interval=1.0)

    prices = await provider.get_prices(["AAPL"])
    assert prices == {}
    prices_again = await provider.get_prices(["AAPL"])
    assert prices_again == {}
    assert len(httpx_mock.get_requests()) == 1


@pytest.mark.parametrize("status", [401, 403])
async def test_get_prices_auth_error_returns_empty_without_raising(httpx_mock, status):
    httpx_mock.add_response(status_code=status)
    provider = make_provider()
    prices = await provider.get_prices(["AAPL"])
    assert prices == {}


@pytest.mark.parametrize("status", [400, 404, 422])
async def test_get_prices_other_non_2xx_returns_empty_without_raising(httpx_mock, status):
    # Regression test: an earlier draft fell through to resp.raise_for_status()
    # for any status not explicitly handled (429/5xx/401/403), which raised
    # httpx.HTTPStatusError straight out of get_prices(). MarketPoller.start()
    # awaits _poll_once() synchronously and unguarded, and FastAPI's lifespan
    # awaits poller.start() directly — so this would have crashed the whole
    # app at boot on the very first unexpected status code.
    httpx_mock.add_response(status_code=status)
    provider = make_provider()
    prices = await provider.get_prices(["AAPL"])
    assert prices == {}


async def test_get_prices_invalid_json_body_returns_empty_without_raising(httpx_mock):
    # Regression test: a 200 response with a body that isn't valid JSON must
    # not let json.JSONDecodeError propagate out of get_prices() — same
    # never-raise contract as the status-code branches above.
    httpx_mock.add_response(status_code=200, content=b"not json")
    provider = make_provider()
    prices = await provider.get_prices(["AAPL"])
    assert prices == {}


async def test_get_prices_missing_tickers_list_returns_empty_without_raising(httpx_mock):
    httpx_mock.add_response(json={"status": "OK"})  # no "tickers" key at all
    provider = make_provider()
    prices = await provider.get_prices(["AAPL"])
    assert prices == {}


async def test_get_prices_entry_missing_ticker_field_is_skipped_without_raising(httpx_mock):
    # Regression test: a ticker object with a usable price field but no
    # "ticker" key must be skipped, not raise KeyError.
    httpx_mock.add_response(
        json={
            "status": "OK",
            "tickers": [
                {"lastTrade": {"p": 190.0}},  # no "ticker" key
                {"ticker": "AAPL", "lastTrade": {"p": 191.0}},
            ],
        }
    )
    provider = make_provider()
    prices = await provider.get_prices(["AAPL"])
    assert set(prices) == {"AAPL"}


async def test_get_prices_uppercases_response_ticker_symbol(httpx_mock):
    # base.py's ABC contract promises "(uppercased)" keys; enforce it on the
    # response side too, not just on the outgoing request params.
    httpx_mock.add_response(
        json={"status": "OK", "tickers": [{"ticker": "aapl", "lastTrade": {"p": 190.0}}]}
    )
    provider = make_provider()
    prices = await provider.get_prices(["AAPL"])
    assert set(prices) == {"AAPL"}


async def test_get_prices_transport_error_returns_empty_without_raising(httpx_mock):
    httpx_mock.add_exception(httpx.ConnectError("boom"))
    provider = make_provider()
    prices = await provider.get_prices(["AAPL"])
    assert prices == {}


# -- validate_ticker ---------------------------------------------------


async def test_validate_ticker_true_when_price_resolvable(httpx_mock):
    httpx_mock.add_response(json={"ticker": {"ticker": "AAPL", "lastTrade": {"p": 190.0}}})
    provider = make_provider()
    assert await provider.validate_ticker("aapl") is True


async def test_validate_ticker_false_on_404(httpx_mock):
    httpx_mock.add_response(status_code=404)
    provider = make_provider()
    assert await provider.validate_ticker("NOTATICKER") is False


async def test_validate_ticker_false_when_no_price_field_present(httpx_mock):
    httpx_mock.add_response(json={"ticker": {"ticker": "GHOST"}})
    provider = make_provider()
    assert await provider.validate_ticker("GHOST") is False


async def test_validate_ticker_false_on_unexpected_status(httpx_mock):
    httpx_mock.add_response(status_code=500)
    provider = make_provider()
    assert await provider.validate_ticker("AAPL") is False


async def test_validate_ticker_false_on_transport_error(httpx_mock):
    httpx_mock.add_exception(httpx.ConnectError("boom"))
    provider = make_provider()
    assert await provider.validate_ticker("AAPL") is False


async def test_aclose_closes_the_http_client():
    provider = make_provider()
    await provider.aclose()
    assert provider._client.is_closed
