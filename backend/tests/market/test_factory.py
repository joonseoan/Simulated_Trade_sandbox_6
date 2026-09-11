from market.factory import (
    MASSIVE_POLL_SECONDS,
    SIMULATOR_POLL_SECONDS,
    create_provider,
)
from market.massive import MassiveProvider
from market.simulator import SimulatedProvider


def test_create_provider_returns_simulator_when_key_absent(monkeypatch):
    monkeypatch.delenv("MASSIVE_API_KEY", raising=False)
    provider = create_provider()
    assert isinstance(provider, SimulatedProvider)
    assert provider.source == "simulator"
    assert provider.poll_interval == SIMULATOR_POLL_SECONDS


def test_create_provider_returns_simulator_when_key_is_empty_string(monkeypatch):
    monkeypatch.setenv("MASSIVE_API_KEY", "")
    provider = create_provider()
    assert isinstance(provider, SimulatedProvider)


def test_create_provider_returns_simulator_when_key_is_only_whitespace(monkeypatch):
    monkeypatch.setenv("MASSIVE_API_KEY", "   ")
    provider = create_provider()
    assert isinstance(provider, SimulatedProvider)


def test_create_provider_returns_massive_when_key_present(monkeypatch):
    monkeypatch.setenv("MASSIVE_API_KEY", "test-key-123")
    provider = create_provider()
    assert isinstance(provider, MassiveProvider)
    assert provider.source == "massive"
    assert provider.poll_interval == MASSIVE_POLL_SECONDS
