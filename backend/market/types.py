from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

Direction = Literal["up", "down", "flat"]


def to_iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class PriceTick:
    """One observation of a ticker's price from a market data source."""

    ticker: str
    price: float
    timestamp: datetime  # timezone-aware UTC


@dataclass(slots=True)
class CacheEntry:
    price: float
    previous_price: float  # value the hub last EMITTED (not last ingested)
    updated_at: datetime  # when the cache last received a write
    source_time: datetime  # timestamp reported by the source itself


@dataclass(frozen=True, slots=True)
class PriceEvent:
    """Exactly the JSON shape defined in PLAN.md §6."""

    ticker: str
    price: float
    previous_price: float
    timestamp: str  # ISO-8601 UTC, 'Z' suffix
    direction: Direction

    def to_sse(self) -> str:
        # `slots=True` dataclasses have no `__dict__`; use dataclasses.asdict
        # to serialize instead (see PR review: this used to raise AttributeError).
        return f"data: {json.dumps(dataclasses.asdict(self))}\n\n"
