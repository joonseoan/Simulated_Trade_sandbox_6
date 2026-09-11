from __future__ import annotations

import os

from .base import MarketDataProvider
from .massive import MassiveProvider
from .simulator import SimulatedProvider

# Poll cadence by situation (PLAN §6 / MASSIVE_API.md §11). Massive's tier is
# not machine-detectable from the key alone, so the free-tier interval is the
# safe default; override in code/env if a paid tier is confirmed.
MASSIVE_POLL_SECONDS = 15.0
SIMULATOR_POLL_SECONDS = 0.5


def create_provider() -> MarketDataProvider:
    key = os.environ.get("MASSIVE_API_KEY", "").strip()
    if key:
        return MassiveProvider(api_key=key, poll_interval=MASSIVE_POLL_SECONDS)
    return SimulatedProvider(poll_interval=SIMULATOR_POLL_SECONDS)
