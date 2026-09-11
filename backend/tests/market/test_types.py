import json
from datetime import datetime, timedelta, timezone

import pytest

from market.types import PriceEvent, PriceTick, to_iso_z


def test_to_iso_z_formats_utc_with_z_suffix():
    dt = datetime(2026, 1, 2, 3, 4, 5, 678000, tzinfo=timezone.utc)
    assert to_iso_z(dt) == "2026-01-02T03:04:05.678000Z"


def test_to_iso_z_converts_non_utc_to_utc():
    tz = timezone(timedelta(hours=-5))
    dt = datetime(2026, 1, 2, 3, 4, 5, tzinfo=tz)
    assert to_iso_z(dt) == "2026-01-02T08:04:05Z"


def test_price_tick_is_immutable():
    tick = PriceTick("AAPL", 190.0, datetime.now(timezone.utc))
    with pytest.raises((AttributeError, TypeError)):
        tick.price = 200.0  # type: ignore[misc]


def test_price_event_to_sse_shape():
    ev = PriceEvent(
        ticker="AAPL",
        price=190.5,
        previous_price=190.0,
        timestamp="2026-01-02T03:04:05Z",
        direction="up",
    )
    sse = ev.to_sse()
    assert sse.startswith("data: ")
    assert sse.endswith("\n\n")
    payload = json.loads(sse[len("data: "):-2])
    assert payload == {
        "ticker": "AAPL",
        "price": 190.5,
        "previous_price": 190.0,
        "timestamp": "2026-01-02T03:04:05Z",
        "direction": "up",
    }
