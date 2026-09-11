"""Unified market data package.

One abstract interface (`MarketDataProvider`), two interchangeable
implementations (`SimulatedProvider`, `MassiveProvider`), a process-global
price cache, and an SSE broadcast hub. See planning/MARKET_DATA_DESIGN.md.
"""
from .base import MarketDataProvider
from .cache import PriceCache
from .factory import create_provider
from .hub import PriceStreamHub
from .massive import MassiveProvider
from .poller import MarketPoller
from .simulator import SimulatedProvider
from .types import CacheEntry, Direction, PriceEvent, PriceTick

__all__ = [
    "MarketDataProvider",
    "PriceCache",
    "PriceStreamHub",
    "MarketPoller",
    "SimulatedProvider",
    "MassiveProvider",
    "create_provider",
    "PriceTick",
    "CacheEntry",
    "PriceEvent",
    "Direction",
]
