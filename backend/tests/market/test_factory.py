from __future__ import annotations

from market.factory import MASSIVE_POLL_SECONDS, SIMULATOR_POLL_SECONDS, create_provider
from market.massive import MassiveProvider
from market.simulator import SimulatedProvider


async def _aclose(provider):
    await provider.aclose()


async def test_create_provider_returns_simulator_when_key_absent(monkeypatch):
    monkeypatch.delenv("MASSIVE_API_KEY", raising=False)
    provider = create_provider()
    try:
        assert isinstance(provider, SimulatedProvider)
        assert provider.poll_interval == SIMULATOR_POLL_SECONDS
    finally:
        await _aclose(provider)


async def test_create_provider_returns_simulator_when_key_is_empty_or_blank(monkeypatch):
    monkeypatch.setenv("MASSIVE_API_KEY", "   ")
    provider = create_provider()
    try:
        assert isinstance(provider, SimulatedProvider)
    finally:
        await _aclose(provider)


async def test_create_provider_returns_massive_when_key_present(monkeypatch):
    monkeypatch.setenv("MASSIVE_API_KEY", "secret-key")
    provider = create_provider()
    try:
        assert isinstance(provider, MassiveProvider)
        assert provider.poll_interval == MASSIVE_POLL_SECONDS
    finally:
        await _aclose(provider)


async def test_create_provider_strips_whitespace_around_key(monkeypatch):
    monkeypatch.setenv("MASSIVE_API_KEY", "  secret-key  ")
    provider = create_provider()
    try:
        assert isinstance(provider, MassiveProvider)
    finally:
        await _aclose(provider)
