from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from market.cache import PriceCache
from market.hub import FLAT_EPSILON, PriceStreamHub, direction_of
from market.types import PriceTick


def make_tick(ticker: str, price: float) -> PriceTick:
    return PriceTick(ticker=ticker, price=price, timestamp=datetime.now(timezone.utc))


@pytest.mark.parametrize(
    ("prev", "cur", "expected"),
    [
        (100.0, 101.0, "up"),
        (100.0, 99.0, "down"),
        (100.0, 100.0, "flat"),
        (100.0, 100.0 + FLAT_EPSILON / 2, "flat"),
        (100.0, 100.0 + FLAT_EPSILON * 10, "up"),
        (100.0, 100.0 - FLAT_EPSILON * 10, "down"),
    ],
)
def test_direction_of(prev, cur, expected):
    assert direction_of(prev, cur) == expected


async def test_build_events_computes_direction_against_last_emitted_value():
    cache = PriceCache()
    cache.ingest(make_tick("AAPL", 100.0))
    hub = PriceStreamHub(cache)

    events = hub._build_events()
    assert len(events) == 1
    assert events[0].ticker == "AAPL"
    assert events[0].direction == "flat"  # first ever emit: previous == price

    # Price moves up, but we haven't "emitted" yet from the caller's POV —
    # build_events() marks emitted internally, so a second call after a bump
    # should report "up".
    cache.ingest(make_tick("AAPL", 105.0))
    events = hub._build_events()
    assert events[0].direction == "up"
    assert events[0].previous_price == 100.0
    assert events[0].price == 105.0


async def test_build_events_re_emits_unchanged_price_as_flat_every_tick():
    cache = PriceCache()
    cache.ingest(make_tick("AAPL", 100.0))
    hub = PriceStreamHub(cache)

    hub._build_events()  # first emit: flat baseline
    events = hub._build_events()  # nothing ingested in between
    assert events[0].direction == "flat"
    events = hub._build_events()  # still nothing new — must keep re-emitting
    assert events[0].direction == "flat"


async def test_build_events_direction_matches_the_rounded_displayed_values():
    # Regression test: direction used to be computed from the raw unrounded
    # cache floats while price/previous_price were rounded to 4dp only for
    # emission. A sub-precision move (e.g. 1e-6) then reported "up"/"down"
    # even though the two rounded values displayed identically — a green/red
    # flash with no visible price change. Direction must be computed from the
    # same rounded values that are actually emitted.
    cache = PriceCache()
    cache.ingest(make_tick("AAPL", 100.00001))
    hub = PriceStreamHub(cache)
    hub._build_events()  # flat baseline

    cache.ingest(make_tick("AAPL", 100.00002))  # +1e-5: > FLAT_EPSILON, rounds to same 4dp value
    events = hub._build_events()
    assert events[0].price == events[0].previous_price == 100.0
    assert events[0].direction == "flat"


async def test_build_events_empty_cache_returns_no_events():
    hub = PriceStreamHub(PriceCache())
    assert hub._build_events() == []


async def test_fanout_delivers_events_to_all_subscribers():
    cache = PriceCache()
    cache.ingest(make_tick("AAPL", 100.0))
    hub = PriceStreamHub(cache)

    q1 = hub.subscribe()
    q2 = hub.subscribe()
    events = hub._build_events()
    hub._fanout(events)

    got1 = q1.get_nowait()
    got2 = q2.get_nowait()
    assert got1.ticker == got2.ticker == "AAPL"


async def test_unsubscribe_stops_delivery():
    cache = PriceCache()
    cache.ingest(make_tick("AAPL", 100.0))
    hub = PriceStreamHub(cache)

    q = hub.subscribe()
    hub.unsubscribe(q)
    hub._fanout(hub._build_events())
    assert q.empty()


async def test_fanout_drops_slow_subscriber_without_blocking_others():
    cache = PriceCache()
    cache.ingest(make_tick("AAPL", 100.0))
    hub = PriceStreamHub(cache)

    slow = asyncio.Queue(maxsize=1)
    slow.put_nowait("stale-item-already-fills-the-queue")
    hub._subscribers.add(slow)
    healthy = hub.subscribe()

    hub._fanout(hub._build_events())

    assert slow not in hub._subscribers  # dropped for being full
    assert not healthy.empty()  # unaffected


async def test_start_and_stop_run_without_error():
    hub = PriceStreamHub(PriceCache())
    await hub.start()
    await hub.stop()
