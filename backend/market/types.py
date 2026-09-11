"""Core types shared by the market data cache, hub, and providers.

See planning/MARKET_DATA_DESIGN.md §4.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

Direction = Literal["up", "down", "flat"]


def to_iso_z(dt: datetime) -> str:
    """Format a timezone-aware datetime as ISO-8601 UTC with a 'Z' suffix."""
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
        return f"data: {json.dumps(self.__dict__)}\n\n"
