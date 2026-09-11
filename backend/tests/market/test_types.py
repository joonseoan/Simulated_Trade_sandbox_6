import json
from datetime import datetime, timedelta, timezone

from market.types import PriceEvent, to_iso_z


def test_to_iso_z_formats_utc_with_z_suffix():
    dt = datetime(2026, 1, 15, 12, 30, 45, 123000, tzinfo=timezone.utc)
    assert to_iso_z(dt) == "2026-01-15T12:30:45.123000Z"


def test_to_iso_z_converts_non_utc_to_utc():
    tz = timezone(timedelta(hours=-5))
    dt = datetime(2026, 1, 15, 7, 30, 0, tzinfo=tz)
    assert to_iso_z(dt) == "2026-01-15T12:30:00Z"


def test_price_event_to_sse_shape():
    ev = PriceEvent(
        ticker="AAPL",
        price=190.12,
        previous_price=189.5,
        timestamp="2026-01-15T12:30:45Z",
        direction="up",
    )
    sse = ev.to_sse()
    assert sse.startswith("data: ")
    assert sse.endswith("\n\n")
    payload = json.loads(sse[len("data: "):].strip())
    assert payload == {
        "ticker": "AAPL",
        "price": 190.12,
        "previous_price": 189.5,
        "timestamp": "2026-01-15T12:30:45Z",
        "direction": "up",
    }
