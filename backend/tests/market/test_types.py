from __future__ import annotations

import json
from datetime import datetime, timezone

from market.types import PriceEvent, to_iso_z


def test_to_iso_z_formats_utc_with_z_suffix():
    dt = datetime(2026, 1, 2, 3, 4, 5, 123000, tzinfo=timezone.utc)
    assert to_iso_z(dt) == "2026-01-02T03:04:05.123000Z"
    assert "+00:00" not in to_iso_z(dt)


def test_price_event_to_sse_does_not_raise_and_round_trips_json():
    # Regression test: PriceEvent is a slots=True dataclass, which has no
    # __dict__. An earlier draft of to_sse() used self.__dict__ directly and
    # raised AttributeError on every call — this would have crashed
    # GET /api/stream/prices on the very first event.
    ev = PriceEvent(
        ticker="AAPL",
        price=190.12,
        previous_price=189.50,
        timestamp="2026-01-02T03:04:05Z",
        direction="up",
    )
    frame = ev.to_sse()
    assert frame.startswith("data: ")
    assert frame.endswith("\n\n")

    payload = json.loads(frame[len("data: ") : -2])
    assert payload == {
        "ticker": "AAPL",
        "price": 190.12,
        "previous_price": 189.50,
        "timestamp": "2026-01-02T03:04:05Z",
        "direction": "up",
    }
