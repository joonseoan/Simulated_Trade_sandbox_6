from __future__ import annotations

import asyncio

from .cache import PriceCache
from .types import Direction, PriceEvent, to_iso_z

BROADCAST_INTERVAL = 0.5  # seconds — fixed SSE cadence, independent of provider poll rate
FLAT_EPSILON = 1e-9
SUBSCRIBER_QUEUE_MAX = 1000


def direction_of(prev: float, cur: float) -> Direction:
    if cur > prev + FLAT_EPSILON:
        return "up"
    if cur < prev - FLAT_EPSILON:
        return "down"
    return "flat"


class PriceStreamHub:
    """The single loop that turns cache state into the wire format and fans
    it out to every open SSE connection. Owns 'what was last emitted' so
    `direction` is correct even when the upstream provider polls far slower
    than the 500ms broadcast cadence (Massive free tier: 15s)."""

    def __init__(self, cache: PriceCache) -> None:
        self._cache = cache
        self._subscribers: set[asyncio.Queue[PriceEvent]] = set()
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="price-stream-hub")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def subscribe(self) -> asyncio.Queue[PriceEvent]:
        q: asyncio.Queue[PriceEvent] = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_MAX)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[PriceEvent]) -> None:
        self._subscribers.discard(q)

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(BROADCAST_INTERVAL)
            events = self._build_events()
            if not events:
                continue
            self._fanout(events)

    def _build_events(self) -> list[PriceEvent]:
        events: list[PriceEvent] = []
        for ticker, entry in self._cache.snapshot().items():
            # Round before computing direction, not after: comparing the raw
            # (unrounded) floats let a sub-precision move (e.g. 1e-6) come out
            # "up"/"down" while `price` and `previous_price` — both rounded to
            # 4dp for emission — displayed as identical values, producing a
            # visible flash with no visible number change.
            price = round(entry.price, 4)
            previous_price = round(entry.previous_price, 4)
            direction = direction_of(previous_price, price)
            events.append(
                PriceEvent(
                    ticker=ticker,
                    price=price,
                    previous_price=previous_price,
                    timestamp=to_iso_z(entry.source_time),
                    direction=direction,
                )
            )
            self._cache.mark_emitted(ticker, entry.price)
        return events

    def _fanout(self, events: list[PriceEvent]) -> None:
        dead: list[asyncio.Queue] = []
        for q in self._subscribers:
            for ev in events:
                try:
                    q.put_nowait(ev)
                except asyncio.QueueFull:
                    dead.append(q)  # slow consumer — drop it, EventSource reconnects
                    break
        for q in dead:
            self._subscribers.discard(q)
