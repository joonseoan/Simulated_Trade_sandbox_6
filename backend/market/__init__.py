"""Unified market data package: one provider interface, two implementations
(SimulatedProvider, MassiveProvider), a process-global price cache, an SSE
broadcast hub, and the poll loop that connects them.

See planning/MARKET_DATA_DESIGN.md for the full design.
"""
from __future__ import annotations

from .base import MarketDataProvider
from .cache import PriceCache
from .factory import create_provider
from .hub import PriceStreamHub
from .massive import MassiveProvider
from .poller import MarketPoller
from .simulator import SimulatedProvider
from .types import CacheEntry, Direction, PriceEvent, PriceTick, to_iso_z

__all__ = [
    "MarketDataProvider",
    "PriceCache",
    "create_provider",
    "PriceStreamHub",
    "MassiveProvider",
    "MarketPoller",
    "SimulatedProvider",
    "CacheEntry",
    "Direction",
    "PriceEvent",
    "PriceTick",
    "to_iso_z",
]
