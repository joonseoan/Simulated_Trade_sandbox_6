"""Process-global latest-price store.

See planning/MARKET_DATA_DESIGN.md §6.
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone

from .types import CacheEntry, PriceTick


class PriceCache:
    """Plain dict + lock — no async, no I/O.

    Safe to read/write from sync or async code on any event loop.
    """

    def __init__(self) -> None:
        self._entries: dict[str, CacheEntry] = {}
        self._lock = threading.Lock()

    def known_tickers(self) -> set[str]:
        with self._lock:
            return set(self._entries)

    def has(self, ticker: str) -> bool:
        with self._lock:
            return ticker in self._entries

    def ingest(self, tick: PriceTick) -> None:
        """Write a fresh observation from the provider.

        `previous_price` is left untouched here — only the hub's
        mark_emitted() advances it, so `direction` reflects the last value a
        *client saw*, not the last value the provider reported.
        """
        with self._lock:
            prev = self._entries.get(tick.ticker)
            self._entries[tick.ticker] = CacheEntry(
                price=tick.price,
                previous_price=prev.previous_price if prev else tick.price,
                updated_at=datetime.now(timezone.utc),
                source_time=tick.timestamp,
            )

    def seed_missing(self, ticker: str, price: float) -> None:
        """Register a ticker that must stream even though the provider
        hasn't reported it yet this cycle (e.g. a held position, or a
        brand-new watchlist add before the first poll completes).
        """
        with self._lock:
            self._entries.setdefault(
                ticker,
                CacheEntry(price, price, datetime.now(timezone.utc), datetime.now(timezone.utc)),
            )

    def mark_emitted(self, ticker: str, emitted_price: float) -> None:
        """Called by PriceStreamHub right after it sends a tick for `ticker`,
        so the *next* diff is computed against the value the client saw.
        """
        with self._lock:
            e = self._entries.get(ticker)
            if e:
                e.previous_price = emitted_price

    def snapshot(self) -> dict[str, CacheEntry]:
        """A defensive copy — callers iterate this without holding the lock."""
        with self._lock:
            return {
                k: CacheEntry(v.price, v.previous_price, v.updated_at, v.source_time)
                for k, v in self._entries.items()
            }

    def latest_price(self, ticker: str) -> float | None:
        with self._lock:
            e = self._entries.get(ticker)
            return e.price if e else None

    def newest_update_age_ms(self) -> float | None:
        """Age of the freshest entry, in ms. None if the cache is empty
        (startup, before the first poll). Feeds /api/health.
        """
        with self._lock:
            if not self._entries:
                return None
            newest = max(e.updated_at for e in self._entries.values())
        return (datetime.now(timezone.utc) - newest).total_seconds() * 1000
