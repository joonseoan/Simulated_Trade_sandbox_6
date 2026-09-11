import asyncio
from datetime import datetime, timezone

import pytest

from market.cache import PriceCache
from market.hub import BROADCAST_INTERVAL, PriceStreamHub, _direction
from market.types import PriceTick


@pytest.mark.parametrize(
    "prev,cur,expected",
    [
        (100.0, 101.0, "up"),
        (100.0, 99.0, "down"),
        (100.0, 100.0, "flat"),
        (100.0, 100.0 + 1e-10, "flat"),  # within FLAT_EPSILON
        (100.0, 100.0 + 1e-8, "up"),  # outside FLAT_EPSILON
    ],
)
def test_direction(prev, cur, expected):
    assert _direction(prev, cur) == expected


def _ingest(cache: PriceCache, ticker: str, price: float) -> None:
    cache.ingest(PriceTick(ticker, price, datetime.now(timezone.utc)))


def test_build_events_reports_direction_and_marks_emitted():
    cache = PriceCache()
    _ingest(cache, "AAPL", 190.0)

    hub = PriceStreamHub(cache)
    events = hub._build_events()

    assert len(events) == 1
    assert events[0].ticker == "AAPL"
    assert events[0].direction == "flat"  # first ever tick: previous == price

    # A price move without an intervening emit still compares to the last
    # *emitted* value, not the last ingested one.
    _ingest(cache, "AAPL", 195.0)
    events = hub._build_events()
    assert events[0].direction == "up"
    assert events[0].previous_price == 190.0
    assert events[0].price == 195.0


def test_unchanged_price_still_re_emits_as_flat_every_tick():
    cache = PriceCache()
    _ingest(cache, "AAPL", 190.0)

    hub = PriceStreamHub(cache)
    first = hub._build_events()
    assert first[0].direction == "flat"

    # No new ingest between builds — price truly unchanged.
    second = hub._build_events()
    assert len(second) == 1
    assert second[0].direction == "flat"


def test_build_events_covers_every_known_ticker():
    cache = PriceCache()
    _ingest(cache, "AAPL", 190.0)
    _ingest(cache, "MSFT", 415.0)

    hub = PriceStreamHub(cache)
    events = hub._build_events()
    assert {e.ticker for e in events} == {"AAPL", "MSFT"}


def test_fanout_delivers_events_to_all_subscribers():
    cache = PriceCache()
    _ingest(cache, "AAPL", 190.0)
    hub = PriceStreamHub(cache)

    q1 = hub.subscribe()
    q2 = hub.subscribe()
    events = hub._build_events()
    hub._fanout(events)

    assert q1.get_nowait() == events[0]
    assert q2.get_nowait() == events[0]


def test_fanout_drops_a_subscriber_whose_queue_is_full():
    cache = PriceCache()
    _ingest(cache, "AAPL", 190.0)
    hub = PriceStreamHub(cache)

    slow_q: asyncio.Queue = asyncio.Queue(maxsize=1)
    hub._subscribers.add(slow_q)
    slow_q.put_nowait("already-full-sentinel")

    events = hub._build_events()
    hub._fanout(events)

    assert slow_q not in hub._subscribers


def test_unsubscribe_stops_delivery():
    cache = PriceCache()
    _ingest(cache, "AAPL", 190.0)
    hub = PriceStreamHub(cache)

    q = hub.subscribe()
    hub.unsubscribe(q)

    events = hub._build_events()
    hub._fanout(events)
    assert q.empty()


async def test_run_loop_broadcasts_on_its_own_cadence():
    cache = PriceCache()
    _ingest(cache, "AAPL", 190.0)
    hub = PriceStreamHub(cache)
    q = hub.subscribe()

    await hub.start()
    try:
        ev = await asyncio.wait_for(q.get(), timeout=BROADCAST_INTERVAL + 2.0)
        assert ev.ticker == "AAPL"
    finally:
        await hub.stop()
