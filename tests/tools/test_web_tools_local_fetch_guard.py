"""The ``extract_backend: local`` fetcher honours the same two gates as the cloud providers.

Regression for the fork's tiered local fetcher, which checked the model-supplied URL once and then
let curl_cffi/httpx follow redirects unguarded and never consulted the website blocklist. The
blocklist must run before any local fetch, and a redirect hop into a private address must be
refused rather than fetched or handed to the cloud fallback.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from tools import url_safety, web_tools


@pytest.fixture
def local_mode(monkeypatch):
    monkeypatch.setattr(web_tools, "_load_web_config", lambda: {"extract_backend": "local"})
    monkeypatch.setattr(web_tools, "_ensure_web_plugins_loaded", lambda: None)

    async def _safe(_url):
        return True

    monkeypatch.setattr(web_tools, "async_is_safe_url", _safe)


@pytest.mark.asyncio
async def test_policy_blocked_url_never_reaches_the_local_fetcher(local_mode, monkeypatch):
    block = {"host": "blocked.example", "rule": "blocked.example", "source": "config", "message": "Blocked by policy"}
    monkeypatch.setattr(web_tools, "check_website_access", lambda url, config_path=None: block)

    async def _must_not_run(url, timeout=60):
        raise AssertionError(f"local fetcher ran for policy-blocked {url}")

    monkeypatch.setattr(web_tools, "_fetch_and_process_locally", _must_not_run)
    out = json.loads(await web_tools.web_extract_tool(["https://blocked.example/page"]))
    entry = out["results"][0]
    assert entry["error"] == block["message"]
    assert entry["blocked_by_policy"]["host"] == block["host"]
    assert not entry.get("content")


class _RedirectToMetadata(BaseHTTPRequestHandler):
    hits = 0

    def log_message(self, *_args):  # silence
        pass

    def do_GET(self):
        type(self).hits += 1
        self.send_response(302)
        self.send_header("Location", "http://169.254.169.254/latest/meta-data/")
        self.end_headers()


@pytest.fixture
def redirecting_server():
    srv = HTTPServer(("127.0.0.1", 0), _RedirectToMetadata)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}/start"
    finally:
        srv.shutdown()


@pytest.mark.asyncio
async def test_httpx_tier_refuses_redirect_into_metadata_address(redirecting_server, monkeypatch):
    # The first hop is loopback, so opt in to private URLs; the cloud-metadata range stays blocked
    # regardless, which is exactly the hop the redirect tries to reach.
    monkeypatch.setenv("HERMES_ALLOW_PRIVATE_URLS", "1")
    url_safety._reset_allow_private_cache()
    monkeypatch.setattr(web_tools, "HAS_CURL_CPERF", False)
    monkeypatch.setattr(web_tools, "HAS_SCRAPLING", False)
    try:
        entry = await web_tools._fetch_and_process_locally(redirecting_server, timeout=10)
    finally:
        url_safety._reset_allow_private_cache()
    assert entry is not None, "a blocked hop must be a final error, not a cloud-fallback signal"
    assert entry["error"] == web_tools._UNSAFE_HOP_MSG
    assert not entry.get("content")
    assert _RedirectToMetadata.hits == 1


@pytest.mark.asyncio
async def test_guard_hop_applies_both_gates(monkeypatch):
    # SSRF gate (used by the curl_cffi manual-redirect loop and Scrapling's landing-URL check).
    with pytest.raises(web_tools._LocalFetchBlocked) as exc:
        await web_tools._guard_hop("http://169.254.169.254/latest/meta-data/")
    assert exc.value.message == web_tools._UNSAFE_HOP_MSG and exc.value.blocked is None

    # Policy gate on a hop that passes SSRF.
    async def _safe(_url):
        return True

    block = {"host": "h", "rule": "h", "source": "config", "message": "nope"}
    monkeypatch.setattr(web_tools, "async_is_safe_url", _safe)
    monkeypatch.setattr(web_tools, "check_website_access", lambda url, config_path=None: block)
    with pytest.raises(web_tools._LocalFetchBlocked) as exc:
        await web_tools._guard_hop("https://h/x")
    assert exc.value.blocked == block
