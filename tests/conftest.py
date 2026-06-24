"""Shared test environment — dummy settings, no real secrets, no network."""
import pytest


@pytest.fixture(autouse=True)
def _test_env(monkeypatch):
    monkeypatch.setenv("TMS_HOST", "tms.test")
    monkeypatch.setenv("TMS_PORT", "1234")
    monkeypatch.setenv("TMS_TOKEN", "test-token")
    monkeypatch.setenv("FMCSA_API_KEY", "test-fmcsa")
    monkeypatch.setenv("BRIDGE_API_KEY", "test-bridge-key")
    from config import get_settings
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
