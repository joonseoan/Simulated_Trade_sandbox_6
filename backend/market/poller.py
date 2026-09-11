"""Drives a MarketDataProvider on its own cadence and writes into the cache.

See planning/MARKET_DATA_DESIGN.md §8.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Callable

from .base import MarketDataProvider
from .cache import PriceCache

log = logging.getLogger("market.poller")


class MarketPoller:
    """Drives provider.get_prices() on the provider's own interval and
    writes results into the cache. This is the only code that calls the
    provider — the SSE hub and REST routes only ever touch the cache."""

    def __init__(
        self,
        provider: MarketDataProvider,
        cache: PriceCache,
        tickers_supplier: Callable[[], list[str]],  # () -> watchlist ∪ held positions
    ) -> None:
        self._provider = provider
        self._cache = cache
        self._tickers_supplier = tickers_supplier
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        await self._provider.start()
        await self._poll_once()  # prime synchronously so tick #1 of the SSE hub isn't empty
        self._task = asyncio.create_task(self._run(), name="market-poller")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self._provider.aclose()

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._provider.poll_interval)
            try:
                await self._poll_once()
            except Exception:
                log.exception("poll cycle failed; keeping last cached prices")

    async def _poll_once(self) -> None:
        # Union: current watchlist ∪ everything already cached (held positions,
        # tickers dropped from the watchlist but still owned). Never shrinks
        # the set the provider is asked about just because a ticker was
        # unwatched — PLAN §8 "held tickers stay known regardless of
        # watchlist membership".
        tickers = sorted(set(self._tickers_supplier()) | self._cache.known_tickers())
        if not tickers:
            return
        prices = await self._provider.get_prices(tickers)
        for tick in prices.values():
            self._cache.ingest(tick)
