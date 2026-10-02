"""First-use provisioning of the ``web-local`` extra and its Chromium build (fork).

Contracts: the extra is installed through PM at most once per process and rebinds the fetcher's
module names; the Chromium download runs only when the revision patchright pins is absent from
the browser cache, and never twice in one process.
"""
from __future__ import annotations

import sys
import types

import pytest

from tools import web_tools_local_deps as deps


def _fake_stack(monkeypatch):
    curl = types.ModuleType("curl_cffi"); curl.requests = types.ModuleType("curl_cffi.requests")
    scr = types.ModuleType("scrapling"); fetchers = types.ModuleType("scrapling.fetchers")
    fetchers.AsyncStealthySession = type("AsyncStealthySession", (), {}); scr.fetchers = fetchers
    for name, mod in {"curl_cffi": curl, "curl_cffi.requests": curl.requests, "scrapling": scr,
                      "scrapling.fetchers": fetchers, "pymupdf": types.ModuleType("pymupdf"),
                      "trafilatura": types.ModuleType("trafilatura")}.items():
        monkeypatch.setitem(sys.modules, name, mod)


def test_stack_install_runs_once_and_rebinds_the_fetcher_namespace(monkeypatch):
    monkeypatch.setattr(deps, "_stack_checked", False)
    _fake_stack(monkeypatch)
    import pm.extras as extras
    calls = []
    monkeypatch.setattr(extras, "ensure_import", lambda extra: calls.append(extra))
    ns = {flag: False for flag in deps.STACK_FLAGS}

    assert deps.ensure_local_fetcher_stack(ns) is True
    assert calls == [deps.EXTRA]
    assert all(ns[flag] for flag in deps.STACK_FLAGS)
    assert ns["AsyncStealthySession"].__name__ == "AsyncStealthySession"

    # Already present (flags true) → PM is not consulted again, with or without the once-guard.
    monkeypatch.setattr(extras, "ensure_import", lambda extra: pytest.fail("ensure_import called twice"))
    assert deps.ensure_local_fetcher_stack(ns) is True


def test_stack_install_failure_is_reported_once_then_degrades(monkeypatch):
    monkeypatch.setattr(deps, "_stack_checked", False)
    import pm.extras as extras
    calls = []

    def _declined(extra):
        calls.append(extra)
        raise RuntimeError("installation of extra 'web-local' declined")

    monkeypatch.setattr(extras, "ensure_import", _declined)
    ns = {flag: False for flag in deps.STACK_FLAGS}
    assert deps.ensure_local_fetcher_stack(ns) is False
    assert deps.ensure_local_fetcher_stack(ns) is False
    assert calls == [deps.EXTRA], "a declined/failed install must not re-prompt in the same process"
    assert not any(ns[flag] for flag in deps.STACK_FLAGS)


def test_chromium_download_only_when_pinned_revision_is_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(deps, "_chromium_checked", False)
    monkeypatch.setattr(deps, "required_chromium_revision", lambda: "1234")
    monkeypatch.setattr(deps, "browsers_root", lambda: tmp_path)
    runs = []

    def _installer(timeout):
        runs.append(timeout)
        (tmp_path / "chromium-1234").mkdir()
        (tmp_path / "chromium-1234" / "INSTALLATION_COMPLETE").write_text("", encoding="utf-8")
        return 0

    monkeypatch.setattr(deps, "_run_installer", _installer)
    assert deps.ensure_chromium() is True
    assert runs == [deps.CHROMIUM_INSTALL_TIMEOUT_S]
    assert deps.ensure_chromium() is True and runs == [deps.CHROMIUM_INSTALL_TIMEOUT_S]

    # A different pinned revision (patchright bump) is a fresh download; a present one never is.
    monkeypatch.setattr(deps, "_chromium_checked", False)
    monkeypatch.setattr(deps, "required_chromium_revision", lambda: "1300")
    monkeypatch.setattr(deps, "_run_installer", lambda timeout: runs.append("1300") or 1)
    assert deps.ensure_chromium() is False
    assert runs[-1] == "1300"
