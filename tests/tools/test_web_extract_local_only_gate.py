"""Fork: ``web.extract_backend: local`` keeps web_extract exposed with no cloud backend.

The local tiered fetcher needs no credentials, so the web_extract ``check_fn`` must not depend on
``check_web_api_key()`` alone: with no cloud keys and ``keyless_fallback: false`` that returns False
and the registry would drop web_extract even though local extraction works. web_search stays gated
on a real search backend ("local" is extract-only).
"""
from __future__ import annotations

from tools import web_tools
from tools.registry import registry


def _no_cloud(monkeypatch, cfg):
    monkeypatch.setattr(web_tools, "_load_web_config", lambda: cfg)
    monkeypatch.setattr(web_tools, "check_web_api_key", lambda: False)


def test_local_pin_keeps_web_extract_without_cloud(monkeypatch):
    _no_cloud(monkeypatch, {"extract_backend": "local", "keyless_fallback": False})
    assert web_tools.check_web_extract_available() is True


def test_no_local_pin_defers_to_cloud_gate(monkeypatch):
    _no_cloud(monkeypatch, {"keyless_fallback": False})
    assert web_tools.check_web_extract_available() is False
    monkeypatch.setattr(web_tools, "check_web_api_key", lambda: True)
    assert web_tools.check_web_extract_available() is True


def test_registry_wiring():
    assert registry.get_entry("web_extract").check_fn is web_tools.check_web_extract_available
    assert registry.get_entry("web_search").check_fn is web_tools.check_web_api_key
