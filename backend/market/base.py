"""The unified market data provider interface.

See planning/MARKET_DATA_DESIGN.md §5. SimulatedProvider and MassiveProvider
both implement this and nothing else in the codebase is allowed to know which
one is active (except to report `.source` in /api/health).
"""
from __future__ import annotations

import abc

from .types import PriceTick


class MarketDataProvider(abc.ABC):
    """Abstract market data source."""

    #: "simulator" | "massive" — reported verbatim by /api/health.
    source: str

    #: Seconds between MarketPoller cycles for this provider.
    poll_interval: float

    @abc.abstractmethod
    async def get_prices(self, tickers: list[str]) -> dict[str, PriceTick]:
        """Return the latest price for each resolvable ticker in `tickers`.

        Contract:
        - Keys of the result are a subset of (uppercased) `tickers`.
        - A ticker absent from the result means "no update this cycle" —
          the caller (MarketPoller) simply keeps the last cached value.
        - Never raises for a bad/unknown symbol in the list; skip it.
        - An upstream-wide failure (network error, 5xx, rate limit) must be
          caught internally and logged; return whatever partial data is
          available, or {} — never propagate the exception.
        """

    @abc.abstractmethod
    async def validate_ticker(self, ticker: str) -> bool:
        """True if `ticker` is a resolvable symbol. Backs POST /api/watchlist."""

    async def start(self) -> None:
        """Optional warm-up hook (seed simulator state / prime an HTTP client).

        Called once by MarketPoller.start(). Default no-op.
        """

    async def aclose(self) -> None:
        """Optional teardown (close an HTTP client, cancel an internal task).

        Called once by MarketPoller.stop(). Default no-op.
        """
