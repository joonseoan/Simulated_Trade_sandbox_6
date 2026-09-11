import asyncio
from datetime import datetime, timezone

import pytest

from market.cache import PriceCache
from market.hub import FLAT_EPSILON, PriceStreamHub, _direction
from market.types import PriceTick


def _tick(ticker: str, price: float) -> PriceTick:
    return PriceTick(ticker, price, datetime.now(timezone.utc))


@pytest.mark.parametrize(
    ("prev", "cur", "expected"),
    [
        (100.0, 101.0, "up"),
        (100.0, 99.0, "down"),
        (100.0, 100.0, "flat"),
        (100.0, 100.0 + FLAT_EPSILON / 2, "flat"),  # within epsilon still flat
        (100.0, 100.0 + FLAT_EPSILON * 10, "up"),
        (100.0, 100.0 - FLAT_EPSILON * 10, "down"),
    ],
)
def test_direction(prev, cur, expected):
    assert _direction(prev, cur) == expected


def test_build_events_computes_direction_and_marks_emitted():
    cache = PriceCache()
    cache.ingest(_tick("AAPL", 100.0))
    cache.mark_emitted("AAPL", 100.0)
    cache.ingest(_tick("AAPL", 105.0))

    hub = PriceStreamHub(cache)
    events = hub._build_events()

    assert len(events) == 1
    ev = events[0]
    assert ev.ticker == "AAPL"
    assert ev.price == 105.0
    assert ev.previous_price == 100.0
    assert ev.direction == "up"

    # mark_emitted was called: the next build should see no further change.
    events2 = hub._build_events()
    assert events2[0].direction == "flat"
    assert events2[0].previous_price == 105.0


def test_unchanged_price_still_reemits_as_flat_every_tick():
    cache = PriceCache()
    cache.ingest(_tick("AAPL", 100.0))
    hub = PriceStreamHub(cache)

    for _ in range(3):
        events = hub._build_events()
        assert len(events) == 1
        assert events[0].direction == "flat"


@pytest.mark.asyncio
async def test_fanout_drops_slow_consumer_whose_queue_is_full():
    cache = PriceCache()
    cache.ingest(_tick("AAPL", 100.0))
    cache.ingest(_tick("MSFT", 200.0))
    hub = PriceStreamHub(cache)

    slow_q: asyncio.Queue = asyncio.Queue(maxsize=1)
    slow_q.put_nowait("pre-filled")  # queue is now full before fanout starts
    hub._subscribers.add(slow_q)

    healthy_q = hub.subscribe()

    events = hub._build_events()
    hub._fanout(events)

    assert slow_q not in hub._subscribers
    assert healthy_q in hub._subscribers
    assert healthy_q.qsize() == len(events)


@pytest.mark.asyncio
async def test_start_and_stop_lifecycle():
    cache = PriceCache()
    hub = PriceStreamHub(cache)
    await hub.start()
    assert hub._task is not None
    await hub.stop()
    assert hub._task.cancelled() or hub._task.done()


@pytest.mark.asyncio
async def test_subscribe_and_unsubscribe():
    cache = PriceCache()
    hub = PriceStreamHub(cache)
    q = hub.subscribe()
    assert q in hub._subscribers
    hub.unsubscribe(q)
    assert q not in hub._subscribers
    hub.unsubscribe(q)  # unsubscribing twice must not raise
