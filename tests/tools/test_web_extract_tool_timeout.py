"""``web_extract_tool`` must reach the wall-clock-capped dispatcher on every cloud path.

Regression for the fork's inline extract dispatcher, which called ``provider.extract`` directly
and so ignored ``web.extract_timeout`` (tools/web_tools_extract.py::_dispatch_extract). Covers
both the plain cloud backend and the ``extract_backend: local`` cloud fallback.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from agent import web_search_registry
from agent.web_search_provider import WebSearchProvider
from tools import web_tools, web_tools_extract as wte


class _HangingExtractProvider(WebSearchProvider):
    @property
    def name(self) -> str:
        return "hanging-cloud"

    @property
    def display_name(self) -> str:
        return "Hanging Cloud"

    def is_available(self) -> bool:
        return True

    def supports_extract(self) -> bool:
        return True

    async def extract(self, urls, **kwargs):
        await asyncio.sleep(9999)


@pytest.fixture
def hanging_provider(monkeypatch):
    with web_search_registry._lock:
        previous = dict(web_search_registry._providers)
        web_search_registry._providers.clear()
    provider = _HangingExtractProvider()
    web_search_registry.register_provider(provider)
    monkeypatch.setattr(web_tools, "_ensure_web_plugins_loaded", lambda: None)
    monkeypatch.setattr(wte, "_extract_timeout_seconds", lambda: 0.2)
    monkeypatch.setattr(wte, "_rescue_eligible", lambda p: False)

    async def _safe(_url):
        return True

    monkeypatch.setattr(web_tools, "async_is_safe_url", _safe)
    yield provider
    with web_search_registry._lock:
        web_search_registry._providers.clear()
        web_search_registry._providers.update(previous)


URLS = ["https://example.com/a", "https://example.com/b"]


def _assert_timed_out(payload: str, provider_name: str) -> None:
    results = json.loads(payload)["results"]
    assert [r["url"] for r in results] == URLS
    for r in results:
        assert "timed out" in r["error"].lower()
        assert provider_name in r["error"]


@pytest.mark.asyncio
async def test_cloud_backend_hang_is_bounded_by_extract_timeout(hanging_provider, monkeypatch):
    monkeypatch.setattr(web_tools, "_load_web_config", lambda: {"extract_backend": hanging_provider.name})
    payload = await asyncio.wait_for(web_tools.web_extract_tool(URLS), timeout=5)
    _assert_timed_out(payload, hanging_provider.name)


@pytest.mark.asyncio
async def test_local_mode_cloud_fallback_hang_is_bounded_by_extract_timeout(hanging_provider, monkeypatch):
    monkeypatch.setattr(
        web_tools, "_load_web_config",
        lambda: {"extract_backend": "local", "backend": hanging_provider.name},
    )

    async def _local_fails(url, timeout=60):
        return None

    monkeypatch.setattr(web_tools, "_fetch_and_process_locally", _local_fails)
    payload = await asyncio.wait_for(web_tools.web_extract_tool(URLS), timeout=5)
    _assert_timed_out(payload, hanging_provider.name)
