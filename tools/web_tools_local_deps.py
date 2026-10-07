"""Fork: on-demand provisioning for web_extract's ``local`` backend.

The tiered local fetcher (tools/web_tools.py) needs the ``web-local`` extra (curl_cffi, Scrapling
with its [fetchers] browser stack, trafilatura, PyMuPDF) plus the Chromium build patchright pins.
Neither is a core dependency, so a fresh checkout has none of it and a PM venv rebuild (every
``hermes update`` whose uv.lock changed) drops anything pip-installed by hand.

Both are provisioned here the first time ``extract_backend: local`` is used in a process:

* the extra goes through ``pm.extras.ensure_and_bind`` — PM syncs the venv with the extra
  enabled and records it in its ledger, so every later generation rebuild keeps it (the same
  path the firecrawl / parallel providers use for their SDKs);
* Chromium goes through patchright's own installer into playwright's browser cache, which lives
  outside the venv and outside PM's tool store, so it survives rebuilds and ``pm gc``.

Each step runs once per process; a failure logs the manual command and the fetcher degrades
to whatever tiers are importable (httpx at minimum).
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("tools.web_tools")

EXTRA = "web-local"
STACK_FLAGS = ("HAS_CURL_CPERF", "HAS_SCRAPLING", "HAS_PYMUPDF", "HAS_TRAFILATURA")
MANUAL_HINT = "hermes pm install venv --extra web-local"
CHROMIUM_INSTALL_TIMEOUT_S = 900

_lock = threading.Lock()
_stack_checked = False
_chromium_checked = False


def _import_stack() -> Dict[str, Any]:
    """The module-level names tools/web_tools.py binds at import, re-imported after an install."""
    from curl_cffi import requests as curl_requests
    from scrapling.fetchers import AsyncStealthySession
    import pymupdf as fitz
    import trafilatura
    return {
        "curl_requests": curl_requests, "HAS_CURL_CPERF": True,
        "AsyncStealthySession": AsyncStealthySession, "HAS_SCRAPLING": True,
        "fitz": fitz, "HAS_PYMUPDF": True,
        "trafilatura": trafilatura, "HAS_TRAFILATURA": True,
    }


def stack_present(target_globals: Dict[str, Any]) -> bool:
    return all(target_globals.get(flag) for flag in STACK_FLAGS)


def ensure_local_fetcher_stack(target_globals: Dict[str, Any]) -> bool:
    """Make the ``web-local`` extra importable in *target_globals* (tools.web_tools' namespace),
    installing it through PM on first use. Once per process: a declined prompt, a failed sync or
    a generation that needs a restart is reported once, then the fetcher runs degraded."""
    global _stack_checked
    if stack_present(target_globals):
        return True
    with _lock:
        if _stack_checked:
            return stack_present(target_globals)
        _stack_checked = True
        from pm.extras import ensure_and_bind
        if ensure_and_bind(EXTRA, _import_stack, target_globals):
            logger.info("web-local extra ready: curl_cffi, Scrapling, trafilatura, PyMuPDF")
            return True
        logger.warning(
            "web_extract local backend is running without its full fetcher stack; install it with `%s` "
            "(then restart Hermes).", MANUAL_HINT,
        )
        return False


# ── Chromium for Scrapling's stealth tier ──────────────────────────────────────

def browsers_root() -> Path:
    """Where playwright/patchright resolve browsers: ``PLAYWRIGHT_BROWSERS_PATH`` when set, else the
    per-platform cache dir. ``0`` is playwright's legacy 'inside the package' spelling."""
    env = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip()
    if env == "0":
        import patchright
        return Path(patchright.__file__).parent / "driver" / "package" / ".local-browsers"
    if env:
        return Path(env)
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches"
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache"))
    return base / "ms-playwright"


def required_chromium_revision() -> Optional[str]:
    """The chromium revision the installed patchright driver pins (its browsers.json)."""
    try:
        import patchright
        manifest = Path(patchright.__file__).parent / "driver" / "package" / "browsers.json"
        for browser in json.loads(manifest.read_text(encoding="utf-8-sig")).get("browsers", []):
            if browser.get("name") == "chromium":
                return str(browser["revision"])
    except Exception as exc:  # noqa: BLE001 — no patchright, or an unexpected driver layout
        logger.debug("patchright chromium revision unavailable: %s", exc)
    return None


def chromium_installed(root: Path, revision: str) -> bool:
    """playwright's own completeness contract: the revision directory carries INSTALLATION_COMPLETE."""
    return (root / f"chromium-{revision}" / "INSTALLATION_COMPLETE").is_file()


def _run_installer(timeout: float) -> int:
    """``python -m patchright install chromium`` without depending on which interpreter spawned us:
    the driver node binary + cli.js is what that entry point runs."""
    from patchright._impl._driver import compute_driver_executable, get_driver_env
    node, cli = compute_driver_executable()
    proc = subprocess.run([str(node), str(cli), "install", "chromium"], env=get_driver_env(),
                          stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        logger.warning("patchright chromium install failed (rc=%s): %s", proc.returncode, (proc.stderr or proc.stdout)[-800:])
    return proc.returncode


def ensure_chromium(timeout: float = CHROMIUM_INSTALL_TIMEOUT_S) -> bool:
    """Download the Chromium build patchright pins if it is missing. Once per process; after a
    patchright pin bump the first local extract fetches the new revision the same way."""
    global _chromium_checked
    revision = required_chromium_revision()
    if revision is None:
        return False
    root = browsers_root()
    if chromium_installed(root, revision):
        return True
    with _lock:
        if _chromium_checked:
            return chromium_installed(root, revision)
        _chromium_checked = True
    logger.warning("Downloading the Chromium build Scrapling needs (revision %s) into %s — first use on this "
                   "machine only; this can take a few minutes.", revision, root)
    try:
        if _run_installer(timeout) == 0 and chromium_installed(root, revision):
            logger.info("Chromium %s ready for Scrapling", revision)
            return True
    except Exception as exc:  # noqa: BLE001 — timeout, missing node binary, network
        logger.warning("Chromium download failed: %s", exc)
    logger.warning("Scrapling tier unavailable until Chromium is installed: run `python -m patchright install chromium` "
                   "from the Hermes venv.")
    return False
