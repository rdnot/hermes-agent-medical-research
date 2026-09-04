#!/usr/bin/env python3
"""Generic web_search / web_extract tools over pluggable backends.

Backend is selected during ``hermes tools`` (``web.backend`` in config.yaml; per
capability via ``web.search_backend`` / ``web.extract_backend``). Every vendor
implementation lives in ``plugins/web/<vendor>/provider.py`` and registers with
``agent.web_search_registry``; this module owns selection, safety gates,
caching, keyless rescue, and the truncate-and-store result pipeline.
Debug: ``WEB_TOOLS_DEBUG=true`` writes ``logs/web_tools_debug_<UUID>.json``.
"""

import asyncio
import json
import logging
import os
from typing import Dict, List, Any, Optional
# Per-vendor client cache slots; plugins read/write these via tools.web_tools (tests reset them to None).
_firecrawl_client = _firecrawl_client_config = _parallel_client = _async_parallel_client = _exa_client = None

# ─── Optional Local Fetcher Dependencies (fork) ───────────────────────────────
# These provide free local fallback when cloud APIs are unavailable or fail.
# Install with: pip install curl_cffi scrapling PyMuPDF trafilatura

try:
    from curl_cffi import requests as curl_requests
    HAS_CURL_CPERF = True
except ImportError:
    HAS_CURL_CPERF = False

try:
    # Public API path: `scrapling.fetchers` lazy-exports AsyncStealthySession
    # via its _LAZY_IMPORTS dict (verified on Scrapling 0.4.9). Prefer this over
    # the deep internal path `scrapling.engines._browsers._stealth` because the
    # latter is a private module (leading underscore) and may move between
    # Scrapling releases. Both paths resolve to the same class object.
    # Matches nanobot fork scrapling branch (rdnot/nanobot-medical-research).
    from scrapling.fetchers import AsyncStealthySession
    HAS_SCRAPLING = True
except ImportError:
    HAS_SCRAPLING = False

try:
    import fitz  # PyMuPDF
    HAS_PYMUPDF = True
except ImportError:
    HAS_PYMUPDF = False

try:
    import trafilatura
    HAS_TRAFILATURA = True
except ImportError:
    HAS_TRAFILATURA = False

# NOTE (fork): agent.auxiliary_client import removed — upstream eliminated LLM
# summarization in favor of deterministic truncation (_truncate_with_footer).
# The fork's process_content_with_llm was dead code (never called post-merge).
from plugins.web.firecrawl.provider import _is_tool_gateway_ready, check_firecrawl_api_key
from tools.debug_helpers import DebugSession
from tools.tool_backend_helpers import NOUS_MANAGED_PROVIDER, selection_exists
from tools.url_safety import async_is_safe_url
from tools.web_tools_rescue import _rescue_eligible, _rescue_extract, _rescue_search
from tools.web_tools_truncate import _effective_char_limit, _trim_results, _truncate_results, convert_base64_images_to_links
from tools.web_tools_extract import (
    _extract_safe_urls, _merge_in_order, _no_provider_error, _resolve_extract_provider, _result_entry,
    _strict_selection_error, _validate_extract_urls,
)

logger = logging.getLogger(__name__)


# ─── Backend Selection ────────────────────────────────────────────────────────

def _env_value(name: str) -> str:
    """Resolve ``name`` via the config-aware env layer (``hermes config set`` values), then process env.

    Mirrors the SearXNG provider's ``_searxng_url()`` so that values set through Hermes' config/.env layer
    (``hermes config set``, ``hermes tools``) are honored here too — not just raw process-env exports.
    Without this, a config-only ``SEARXNG_URL`` (or any provider key) leaves the backend auto-detect cascade
    and ``check_web_api_key()`` blind to it. See #34290.
    """
    try:
        from hermes_cli.config import get_env_value
        val = get_env_value(name)
    except Exception:
        val = None
    return ((os.getenv(name, "") if val is None else val) or "").strip()


def _has_env(name: str) -> bool:
    return bool(_env_value(name))


def _load_web_config() -> dict:
    """Load the ``web:`` section from config.yaml; always a dict (a null section yields ``{}``)."""
    try:
        from hermes_cli.config import load_config
        return load_config().get("web") or {}
    except Exception:
        return {}


def _configured_backend(key: str = "backend") -> str:
    """Lower-cased, stripped ``web.<key>`` value ("" when unset/null)."""
    return (_load_web_config().get(key) or "").lower().strip()


def _registry_call(func_name: str, default, *args):
    """``agent.web_search_registry.<func_name>(*args)``, or *default* if it raised (registry never fatal)."""
    try:
        import agent.web_search_registry as registry_mod
        return getattr(registry_mod, func_name)(*args)
    except Exception as exc:  # noqa: BLE001 — registry optional; never fatal
        logger.debug("web provider registry %s%r failed: %s", func_name, args, exc)
        return default


def _registered_web_provider(backend: str):
    """Plugin-registered web provider by name, or ``None``."""
    return _registry_call("get_provider", None, backend) if backend else None


def _list_registered_web_providers():
    """All plugin-registered web providers (empty list on failure)."""
    return _registry_call("list_providers", [])


def _probe(provider, method: str, context: str = "") -> Optional[bool]:
    """``bool(provider.<method>())``, or ``None`` if it raised (a broken provider is unavailable; *context* is
    appended to the debug log line, e.g. " during readiness check")."""
    try:
        return bool(getattr(provider, method)())
    except Exception as exc:  # noqa: BLE001 — a broken provider is "unavailable"
        name = getattr(provider, "name", provider)
        logger.debug("web provider %r.%s() raised%s: %s", name, method, context, exc)
        return None


def _get_backend() -> str:
    """Shared web backend name. A stored ``web.backend`` is returned as-is — no availability probe, no
    fallback — so a broken selection surfaces the vendor's honest error rather than silently rerouting.
    Autodetect runs ONLY when no web selection has ever been stored."""
    configured = _configured_backend()
    if configured:
        # "nous" (managed subscription) is serviced by firecrawl, routed through the managed Tool Gateway.
        return "firecrawl" if configured == NOUS_MANAGED_PROVIDER else configured
    if selection_exists("web"):
        # Selection exists (use_gateway / per-capability keys) but no shared name: firecrawl, no ladder.
        return "firecrawl"

    # Never-configured install. Explicit user credentials beat the managed-gateway probe (a Nous OAuth
    # token's tier may not grant web access; the gateway then fails at runtime with no fallback).
    # Free tiers trail paid.
    backend_candidates = (
        ("tavily", _has_env("TAVILY_API_KEY")), ("perplexity", _has_env("PERPLEXITY_API_KEY")),
        ("exa", _has_env("EXA_API_KEY")),
        ("parallel", _has_env("PARALLEL_API_KEY")), ("keenable", _has_env("KEENABLE_API_KEY")),
        ("firecrawl", _has_env("FIRECRAWL_API_KEY") or _has_env("FIRECRAWL_API_URL")),
        ("firecrawl", _is_tool_gateway_ready()), ("searxng", _has_env("SEARXNG_URL")),
        ("brave-free", _has_env("BRAVE_SEARCH_API_KEY")), ("ddgs", _ddgs_package_importable()),
    )
    for backend, available in backend_candidates:
        if available:
            return backend

    # Plugin-contributed providers (built-ins are covered above); probe the held object directly.
    for provider in _list_registered_web_providers():
        if provider.name not in _LEGACY_WEB_BACKENDS and _probe(provider, "is_available"):
            return provider.name

    # Keyless free tier — strictly last so it never pre-empts a keyed backend. Discovery must run
    # first: reachable from contexts that haven't loaded plugins (subprocess runs, delegate children).
    try:
        _ensure_web_plugins_loaded()
        from agent.web_search_registry import _keyless_preference, _keyless_tier_enabled
        if _keyless_tier_enabled():
            for name in _keyless_preference():
                provider = _registered_web_provider(name)
                if provider is not None and _probe(provider, "is_keyless_available"):
                    return name
    except Exception as exc:  # noqa: BLE001 — registry optional; never fatal
        logger.debug("keyless fallback walk failed: %s", exc)

    return "firecrawl"  # default (backward compat)


def _get_search_backend() -> str:
    """Backend for web_search: ``web.search_backend`` (strict, no probe) > ``web.backend`` > autodetect."""
    return _configured_backend("search_backend") or _get_backend()


def _get_extract_backend() -> str:
    """Backend for web_extract: ``web.extract_backend`` (strict, no probe) > ``web.backend`` > autodetect.

    Fork: ``web.extract_backend == "local"`` routes to the tiered local fetcher
    (curl_cffi → Scrapling → httpx) with auto-fallback to a cloud provider.
    """
    if _configured_backend("extract_backend") == "local":
        return "local"
    return _configured_backend("extract_backend") or _get_backend()


def _ddgs_package_importable() -> bool:
    """ddgs is the only backend gated on package presence; single symbol so tests can patch it."""
    try:
        import ddgs  # noqa: F401
        return True
    except ImportError:
        return False


def _xai_available() -> bool:
    # Cheap probe only (env var OR auth.json OAuth): resolve_xai_http_credentials() may hit the network.
    try:
        from tools.xai_http import has_xai_credentials
        return has_xai_credentials()
    except Exception:
        return False


# Built-in backends -> cheap availability probes; any other name is a plugin provider resolved via the
# registry's ``is_available()``. Lambdas so test patches of module-level helpers (_ddgs_package_importable,
# check_firecrawl_api_key) are honored at call time. ``xai`` is probed via has_xai_credentials(), not a
# registered provider, though the registry's _LEGACY_PREFERENCE omits it — drop it if xai ever registers.
_BUILTIN_AVAILABILITY = {
    "exa": lambda: _has_env("EXA_API_KEY"),
    "parallel": lambda: _has_env("PARALLEL_API_KEY"),
    "keenable": lambda: _has_env("KEENABLE_API_KEY"),
    "firecrawl": lambda: check_firecrawl_api_key(),
    "tavily": lambda: _has_env("TAVILY_API_KEY")
    or any(_configured_backend(k) == "tavily" for k in ("backend", "search_backend", "extract_backend")),
    "perplexity": lambda: _has_env("PERPLEXITY_API_KEY"),
    "searxng": lambda: _has_env("SEARXNG_URL"),
    "brave-free": lambda: _has_env("BRAVE_SEARCH_API_KEY"),
    "ddgs": lambda: _ddgs_package_importable(),
    "xai": _xai_available,
}
_LEGACY_WEB_BACKENDS = frozenset(_BUILTIN_AVAILABILITY)


def _is_backend_available(backend: str) -> bool:
    """True when *backend* is usable — the single availability chokepoint. Non-legacy names delegate to the
    registered provider's ``is_available()`` (unregistered names fall through); built-ins use cheap probes.

    For plugin-registered backends (any name outside :data:`_LEGACY_WEB_BACKENDS`), availability is
    delegated to the provider's ``is_available()`` via the web_search_registry. This is the single
    chokepoint through which ``_get_backend``, ``_get_capability_backend``, and ``check_web_api_key`` all
    resolve availability — fixing custom-provider discovery for every caller at once (issues #28651, #31873,
    #32698). Built-in backends keep their cheap hardcoded probes below.
    """
    backend = (backend or "").lower().strip()
    provider = None if backend in _LEGACY_WEB_BACKENDS else _registered_web_provider(backend)
    if provider is not None:
        return _probe(provider, "is_available") or False
    # Fork: "local" is the tiered fetcher (curl_cffi → Scrapling → httpx), not a
    # plugin-registered provider — valid for extract only, never search.
    if backend == "local":
        return HAS_CURL_CPERF or HAS_SCRAPLING
    probe = _BUILTIN_AVAILABILITY.get(backend)
    return probe() if probe else False


# ─── Firecrawl Client ──────────────────────────────────────────────────────── After PR #25182, the
# firecrawl client, lazy SDK proxy, dual-auth config resolution, response normalizers, and
# check_firecrawl_api_key() all live in plugins.web.firecrawl.provider.
def _web_requires_env() -> list[str]:
    """Tool-registry metadata env vars for the web backends. Gateway vars are always listed: gating them
    on ``managed_nous_tools_enabled()`` cost a synchronous portal HTTP refresh at every CLI startup.
    Contract: set var -> tool sees it; extras are harmless for the not-logged-in."""
    return [
        "EXA_API_KEY", "PARALLEL_API_KEY", "TAVILY_API_KEY", "PERPLEXITY_API_KEY", "KEENABLE_API_KEY", "FIRECRAWL_API_KEY",
        "FIRECRAWL_API_URL", "FIRECRAWL_GATEWAY_URL", "TOOL_GATEWAY_DOMAIN", "TOOL_GATEWAY_SCHEME",
        "TOOL_GATEWAY_USER_TOKEN",
    ]

_debug = DebugSession("web_tools", env_var="WEB_TOOLS_DEBUG")


# ─── Dispatch ─────────────────────────────────────────────────────────────────

# ─── Tiered Local Fetcher (Free Fallback) ─────────────────────────────────────
# Tries local fetchers before falling back to cloud APIs.
# Order: curl_cffi (fast, no browser) → Scrapling (JS/Cloudflare) → httpx (last resort)

def _extract_pdf_text(pdf_data: bytes) -> str:
    """Extract text from PDF using PyMuPDF."""
    if not HAS_PYMUPDF:
        raise ImportError("PyMuPDF not installed. Install with: pip install PyMuPDF")
    doc = fitz.open(stream=pdf_data, filetype="pdf")
    text_lines = []
    for page_num in range(len(doc)):
        page = doc[page_num]
        text = page.get_text()
        text_lines.append(f"--- Page {page_num + 1} ---\n{text}")
    doc.close()
    return "\n".join(text_lines)


def _is_content_sufficient(content_bytes: bytes, url: str) -> bool:
    """
    Returns False if we got a JS shell → escalate to Scrapling browser.
    Tuned on real Reddit HTML (Feb 2026).
    """
    try:
        raw = content_bytes.decode("utf-8", errors="replace").lower()
    except Exception:
        return True

    # Real rendered pages are significantly larger than shells
    if len(raw) < 8000:
        return False

    # Cloudflare challenge page — not real content, escalate to browser tier
    # (checked here so curl_cffi returns False → falls through to Scrapling)
    if any(sig in raw for sig in [
        "just a moment", "checking your browser",
        "cf-browser-verification", "cf_chl_opt",
    ]):
        return False
    # "challenge-platform" is a Cloudflare marker, but the benign beacon
    # script at /cdn-cgi/challenge-platform/scripts/jsd/main.js also
    # contains it — only flag it when the beacon path is NOT present.
    if "challenge-platform" in raw and "/cdn-cgi/challenge-platform/" not in raw:
        return False

    # Generic JS-shell signals (framework-agnostic)
    if '<div id="root"></div>' in raw or '<div id="app"></div>' in raw:
        return False
    if any(sig in raw for sig in ["enable javascript", "requires javascript", "javascript is required"]):
        # False positive: NCBI Bookshelf pages contain "requires javascript" in a header banner
        # but ship full SSR content (not a JS shell). Strong markers of real NCBI content:
        if "ncbi.nlm.nih.gov" in url.lower() and any(m in raw for m in [
            "statpearls", "bookshelf", "citation_title", "ncbi_acc",
            "ncbi_bookparttype", "ncbi_pagename", "continuing education",
        ]):
            return True
        return False

    if "reddit.com" in url.lower():
        # Strong positive markers of real content
        if any(m in raw for m in [
            "shreddit-app",            # root component
            "shreddit-post",           # post body
            "shreddit-comment",        # crucial for threads
            "shreddit-comment-tree",   # comment container
            "faceplate-tracker",       # engagement tracker (only in real render)
            'data-testid="post-content"',
        ]):
            return True

        # Edge-case: old Reddit structure without new components = shell
        if 'id="comment-tree"' in raw and "shreddit-comment" not in raw:
            return False

    if "bbc.com" in url.lower() or "bbc.co.uk" in url.lower():
        # BBC SSR sends real HTML but article body is lazy-loaded via XHR.
        has_article_body = any(m in raw for m in [
            'data-component="text-block"',        # article body paragraphs
            'data-testid="article-body"',         # newer layout
            '"articleBody"',                      # JSON-LD structured data
            'data-e2e="article-body"',            # sport/live pages
            'data-testid="live-post"',            # live blog post block
            'data-component="livepost"',          # live blog component
            'data-component="liveblog"',          # live blog wrapper
            'data-testid="liveblog"',             # live blog testid
            'data-post-id=',                      # individual live blog post
            'data-testid="lx-stream-post"',       # live experience stream post
            'data-e2e="lx-stream-post"',          # live experience stream post (alt)
            '"liveblogposting"',                  # JSON-LD LiveBlogPosting type
        ])
        if not has_article_body:
            return False

    return True


def _is_cloudflare_protected(status: int | None, content: bytes | None) -> bool:
    """
    Detect if curl_cffi hit a solvable Cloudflare challenge page.
    Only returns True for actual CF interstitial/Turnstile pages — NOT bare 403s.
    A bare 403 (e.g. GameStop Bot Fight Mode) has no challenge to solve,
    so solve_cloudflare=True would waste time and still fail.
    """
    if not content:
        return False
    try:
        snippet = content[:8000].decode("utf-8", errors="replace").lower()
        return any(m in snippet for m in [
            "just a moment",            # CF interstitial spinner
            "cf-browser-verification",  # CF challenge form
            "checking your browser",    # CF spinner text
            "cf_chl_opt",               # CF challenge JS variable
        ]) or (
            "challenge-platform" in snippet   # CF challenge platform
            and "/cdn-cgi/challenge-platform/" not in snippet  # but NOT the benign beacon
        )
    except Exception:
        return False


def _html_to_text(html: str, url: str = "") -> str:
    """Extract readable text from HTML using trafilatura."""
    if not HAS_TRAFILATURA:
        # Fallback: strip HTML tags
        import re
        text = re.sub(r'<[^>]+>', '', html)
        text = re.sub(r'\s+', ' ', text).strip()
        return text
    # Use trafilatura for better extraction
    # include_comments=True keeps Reddit-thread/forum/Disqus comment text;
    # default in trafilatura 2.x is True, but Hermes historically passed False
    # which discarded `<shreddit-comment>`/`<div class="comment-*">` subtrees.
    # Set to True to match nanobot fork scrapling branch (rdnot/nanobot-medical-research
    # web.py:323-330 leaves include_comments at its default). On a Reddit thread this
    # raises extraction from ~310 words to ~1,100 words.
    text = trafilatura.extract(html, url=url, include_comments=True, include_tables=True)
    return text or html


def _is_recaptcha_challenge(content_bytes: bytes) -> bool:
    """
    Detect Google reCAPTCHA Enterprise challenge pages (HTTP 200).
    PMC/PubMed serves these as an interstitial before the real article.
    The page contains 'Checking your browser' and loads grecaptcha.enterprise.js.
    """
    try:
        raw = content_bytes.decode("utf-8", errors="replace").lower()
    except Exception:
        return False
    return "checking your browser" in raw and "recaptcha" in raw


def _has_pubmed_article_content(content_bytes: bytes) -> bool:
    """Return True when PubMed/PMC HTML contains the real article body, not a shell/challenge."""
    try:
        raw = content_bytes.decode("utf-8", errors="replace").lower()
    except Exception:
        return False
    if _is_recaptcha_challenge(content_bytes):
        return False
    # PMC full article pages consistently include these server-rendered article markers.
    # Title-only/shell pages can still return HTTP 200, so status alone is not enough.
    return any(marker in raw for marker in [
        'id="main-content"',
        'id="article-container"',
        'pmc-article-section',
        'article-body',
        'class="abstract"',
        'section class="abstract"',
    ])


async def _fetch_raw(url: str, timeout: int = 60) -> tuple[bytes, dict, int, str]:
    """
    Fetch URL bytes with tiered fallback strategy:
      1. curl_cffi           — Chrome TLS impersonation, fast, no browser
                               (skipped for Reddit — always needs real browser)
      2. AsyncStealthySession — stealth Playwright (Patchright), handles JS-rendered
                                pages: Reddit comments, Cloudflare, heavy SPAs.
                                solve_cloudflare auto-enabled when CF detected.
      3. httpx               — last resort, no stealth

    Returns (content_bytes, headers_dict, status_code, fetcher_name)
    """
    import httpx

    is_reddit = "reddit.com" in url.lower()

    # PubMed/PMC blocks curl_cffi and httpx with Cloudflare — skip curl_cffi, go straight to Scrapling
    is_pubmed = "pubmed.ncbi.nlm.nih.gov" in url.lower() or "pmc.ncbi.nlm.nih.gov" in url.lower()
    if is_pubmed:
        logger.debug("PubMed detected — skipping curl_cffi, routing through Scrapling")

    curl_cffi_status: int | None = None
    curl_cffi_content: bytes | None = None

    # ── Tier 1: curl_cffi (Chrome TLS fingerprint, fast, no browser) ──────
    # Skipped for Reddit (JS shell / "prove you are human") and PubMed (reCAPTCHA)
    if HAS_CURL_CPERF and not is_reddit and not is_pubmed:
        try:
            logger.debug("Fetching with curl_cffi: %s", url)
            resp = curl_requests.get(url, timeout=timeout, impersonate="chrome")
            curl_cffi_status = resp.status_code
            curl_cffi_content = resp.content
            if resp.status_code < 400 and _is_content_sufficient(resp.content, url):
                content_type = resp.headers.get("content-type", "text/html")
                return resp.content, dict(resp.headers), resp.status_code, "curl_cffi"
            # status >= 400 or JS shell → fall through to browser tier
            logger.debug("curl_cffi: status=%d, content insufficient → escalating", resp.status_code)
        except Exception as e:
            logger.debug("curl_cffi failed: %s", e)

    # ── Tier 2: Scrapling (Playwright-based, handles JS/Cloudflare) ───────
    if HAS_SCRAPLING:
        try:
            solve_cf = _is_cloudflare_protected(curl_cffi_status, curl_cffi_content)
            if solve_cf:
                logger.debug("Cloudflare detected → enabling solve_cloudflare")
            logger.debug("Fetching with Scrapling: %s (cf_solve=%s)", url, solve_cf)

            # ── PubMed/PMC reCAPTCHA Enterprise page_action callback ──
            # PMC serves a Google reCAPTCHA Enterprise challenge (HTTP 200) that
            # sets a cookie (recaptcha-ca-e / recaptcha-fastly-e / recaptcha-cf-e)
            # after invisible reCAPTCHA solves, then calls location.reload(true).
            # Scrapling's fetch() would return the initial challenge HTML before the
            # redirect fires. This page_action runs after navigation + CF solving,
            # detects the reCAPTCHA challenge page, waits for the cookie, and lets
            # the page reload before Scrapling captures the response.
            _pubmed_recaptcha_action = None
            if is_pubmed:

                async def _pubmed_recaptcha_action(page):
                    """Wait for PubMed/PMC reCAPTCHA to yield real article HTML inside Scrapling."""
                    try:
                        async def _page_has_article() -> bool:
                            page_html = await page.content()
                            return _has_pubmed_article_content(page_html.encode("utf-8", errors="replace"))

                        if await _page_has_article():
                            logger.debug("PubMed: article content already present after navigation")
                            return

                        page_html = await page.content()
                        if not _is_recaptcha_challenge(page_html.encode("utf-8", errors="replace")):
                            # Not the known challenge, but also not article content. Give JS a short
                            # chance to render before Scrapling captures a title-only shell.
                            logger.debug("PubMed: no reCAPTCHA marker but article content absent; waiting for body markers")
                            for _ in range(50):
                                if await _page_has_article():
                                    return
                                await page.wait_for_timeout(100)
                            return

                        logger.info("PubMed reCAPTCHA challenge detected — waiting for article content")
                        # Poll for the success cookie OR for real article markers. Some NCBI/PMC
                        # variants do not expose the historical recaptcha-* cookie names to Playwright,
                        # so DOM/article-content detection is the reliable success condition.
                        _recaptcha_cookies = {
                            "recaptcha-ca-e", "recaptcha-fastly-e",
                            "recaptcha-cf-e", "recaptcha-akam-e",
                        }
                        for _ in range(200):
                            if await _page_has_article():
                                logger.info("PubMed: article content appeared after reCAPTCHA wait")
                                return
                            cookies = await page.context.cookies()
                            cookie_names = {c["name"] for c in cookies}
                            if cookie_names & _recaptcha_cookies:
                                logger.debug("reCAPTCHA cookie detected, waiting for page reload/article markers")
                                try:
                                    await page.wait_for_load_state("domcontentloaded", timeout=10000)
                                except Exception:
                                    pass
                                for _ in range(30):
                                    if await _page_has_article():
                                        logger.info("PubMed: reCAPTCHA bypassed, article content loaded")
                                        return
                                    await page.wait_for_timeout(100)
                            await page.wait_for_timeout(100)

                        # Cookie/content not seen — try manual reload as last resort, then wait for
                        # the article markers rather than returning immediately after a 200 shell.
                        logger.debug("PubMed: reCAPTCHA cookie/content not detected, attempting page.reload()")
                        await page.reload(wait_until="domcontentloaded", timeout=10000)
                        for _ in range(50):
                            if await _page_has_article():
                                logger.info("PubMed: article content loaded after manual reload")
                                return
                            await page.wait_for_timeout(100)
                    except Exception as rc_err:
                        logger.debug("PubMed reCAPTCHA page_action failed: %s", rc_err)

            # PubMed/PMC: retry up to 2 attempts if reCAPTCHA challenge persists
            _pubmed_max_attempts = 2 if is_pubmed else 1

            for _pubmed_attempt in range(_pubmed_max_attempts):
                logger.debug(
                    "AsyncStealthySession fetch (attempt %d/%d): %s",
                    _pubmed_attempt + 1, _pubmed_max_attempts, url,
                )
                session = AsyncStealthySession(
                    headless=True,
                    solve_cloudflare=solve_cf,
                )
                await session.start()
                try:
                    # network_idle=False — Reddit/CF never fully idle (polls, pings)
                    # Use shorter timeout for CF sites (30s), longer for Reddit/SPA (45s)
                    fetch_timeout = 30000 if solve_cf else min(timeout * 1000, 45000)
                    fetch_kwargs = dict(
                        url=url,
                        network_idle=False,
                        adaptive=True,
                        timeout_ms=fetch_timeout,
                    )
                    # Attach the reCAPTCHA wait callback for PubMed/PMC URLs
                    if _pubmed_recaptcha_action is not None:
                        fetch_kwargs["page_action"] = _pubmed_recaptcha_action
                    # Hard timeout for the entire scrapling fetch including CF solving.
                    # Scrapling's _cloudflare_solver has unbounded recursion — each attempt
                    # takes ~12s, so without a cap it loops forever on unsolvable challenges.
                    _scrapling_hard_timeout = 45 if solve_cf else 60
                    try:
                        resp = await asyncio.wait_for(
                            session.fetch(**fetch_kwargs),
                            timeout=_scrapling_hard_timeout,
                        )
                    except asyncio.TimeoutError:
                        logger.warning(
                            "Scrapling fetch timed out after %ds (CF solve=%s) — "
                            "Cloudflare challenge likely unsolvable, skipping to next tier",
                            _scrapling_hard_timeout, solve_cf,
                        )
                        resp = None
                    if resp and resp.status < 400:
                        content = resp.body
                        # Scrapling may return bytes or str
                        if isinstance(content, str):
                            content = content.encode("utf-8", errors="replace")
                        # Reject results that are still a Cloudflare challenge page
                        # (scrapling solver may return without actually solving it)
                        if solve_cf and _is_cloudflare_protected(resp.status, content):
                            logger.warning(
                                "Scrapling returned content still showing Cloudflare challenge — "
                                "solver failed, skipping to next tier"
                            )
                            break  # CF unsolvable, don't retry
                        # Reject PubMed/PMC title-only shells or unresolved challenge pages.
                        # Scrapling can return HTTP 200 before the real article body exists;
                        # accepting that poisons downstream processing with 40-word output.
                        if is_pubmed and not _has_pubmed_article_content(content):
                            if _is_recaptcha_challenge(content):
                                reason = "reCAPTCHA still present"
                            else:
                                reason = "article content markers absent"
                            if _pubmed_attempt < _pubmed_max_attempts - 1:
                                logger.info(
                                    "PubMed: Scrapling returned %s after attempt %d/%d, retrying…",
                                    reason, _pubmed_attempt + 1, _pubmed_max_attempts,
                                )
                                continue
                            logger.warning(
                                "PubMed: Scrapling returned %s after %d attempts, skipping to next tier",
                                reason, _pubmed_max_attempts,
                            )
                            break
                        content_type = resp.headers.get("content-type", "text/html") if resp.headers else "text/html"
                        headers = dict(resp.headers or {})
                        logger.debug("Scrapling fetch succeeded (status=%d)", resp.status)
                        return content, headers, resp.status, "scrapling"
                    logger.debug("Scrapling returned status %d", resp.status if resp else -1)
                finally:
                    await session.close()
        except Exception as e:
            logger.debug("Scrapling failed: %s", e)

    # ── Tier 3: httpx (last resort, no stealth) ───────────────────────────
    try:
        logger.debug("Fetching with httpx (fallback): %s", url)
        async with httpx.AsyncClient(follow_redirects=True) as client:
            resp = await client.get(url, timeout=timeout)
            content_type = resp.headers.get("content-type", "text/html")
            return resp.content, dict(resp.headers), resp.status_code, "httpx"
    except Exception as e:
        logger.debug("httpx failed: %s", e)

    raise Exception(f"All local fetchers failed for {url}")


async def _fetch_jina(url: str, timeout: int = 30) -> Optional[Dict[str, Any]]:
    """Fetch article content via Jina Reader (free, no API key needed for r.jina.ai)."""
    try:
        import httpx
        jina_url = f"https://r.jina.ai/{url}"
        async with httpx.AsyncClient(follow_redirects=True) as client:
            resp = await client.get(
                jina_url,
                timeout=timeout,
                headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"},
            )
            resp.raise_for_status()
            data = resp.json().get("data", {})
            text = data.get("content", "")
            title = data.get("title", "")
        if not text:
            return None
        # NOTE: No inline truncation — web_extract_tool's _truncate_with_footer()
        # handles head+tail + disk-storage uniformly for all results.
        return {
            "url": url,
            "title": title,
            "content": text,
            "raw_content": text,
            "metadata": {"sourceURL": url, "content_type": "text/markdown", "fetcher": "jina"},
        }
    except Exception as e:
        logger.debug("Jina Reader fallback failed for %s: %s", url, e)
        return None


async def _fetch_and_process_locally(url: str, timeout: int = 60) -> Optional[Dict[str, Any]]:
    """
    Fetch URL using tiered local fetchers and process content.
    
    Returns:
        Dict with url, title, content, raw_content, metadata
        OR None if local fetch should fall back to cloud API.
    
    Raises:
        Exception if fetch succeeds but processing fails.
    """
    try:
        content_bytes, headers, status_code, fetcher = await _fetch_raw(url, timeout)
    except Exception as e:
        logger.debug("Local fetch failed for %s: %s", url, e)
        return None  # Signal to fall back to cloud API

    # PubMed/PMC sometimes returns HTTP 200 challenge/title-only shells from every raw
    # fetcher. Never process/cache those as 40-word "success"; use Jina Reader as a
    # last-resort article extractor if direct fetching did not obtain real article HTML.
    if (
        "ncbi.nlm.nih.gov" in url.lower()
        and "pmc" in url.lower()
        and not _has_pubmed_article_content(content_bytes)
    ):
        logger.warning("PubMed/PMC raw fetch returned non-article HTML via %s; trying Jina fallback", fetcher)
        jina_result = await _fetch_jina(url, timeout=30)
        if jina_result is not None:
            return jina_result

    content_type = headers.get("content-type", "text/html")

    # Handle PDF
    if "application/pdf" in content_type or url.lower().endswith(".pdf"):
        try:
            text = _extract_pdf_text(content_bytes)
            return {
                "url": url,
                "title": f"PDF: {url.split('/')[-1]}",
                "content": text,
                "raw_content": text,
                "metadata": {"sourceURL": url, "content_type": "application/pdf"},
            }
        except Exception as e:
            logger.warning("PDF extraction failed for %s: %s", url, e)
            return {
                "url": url,
                "title": "",
                "content": "",
                "error": f"PDF extraction failed: {e}",
                "metadata": {"sourceURL": url},
            }
    
    # Handle images (for vision models - return base64)
    if content_type.startswith("image/"):
        import base64
        base64_img = base64.b64encode(content_bytes).decode("utf-8")
        return {
            "url": url,
            "title": f"Image: {url.split('/')[-1]}",
            "content": f"![Image]({url})",
            "raw_content": f"data:{content_type};base64,{base64_img}",
            "metadata": {"sourceURL": url, "content_type": content_type, "is_image": True},
        }
    
    # Handle HTML/text
    try:
        html = content_bytes.decode("utf-8", errors="replace")
    except Exception as e:
        logger.warning("Failed to decode content for %s: %s", url, e)
        return None
    
    # Extract title
    title = ""
    title_match = re.search(r'<title[^>]*>([^<]+)</title>', html, re.IGNORECASE)
    if title_match:
        title = title_match.group(1).strip()
    
    # Extract readable text
    text = _html_to_text(html, url)

    # NOTE: No inline truncation here. web_extract_tool runs _truncate_with_footer()
    # on ALL results (local + cloud) after this returns. Pre-truncating here would
    # (1) discard the tail before _truncate_with_footer can include it in the
    # head+tail window, and (2) suppress the disk-storage + read_file footer path
    # (because _truncate_with_footer sees len <= char_limit → was_truncated=False).
    # Return the full extracted text and let the unified truncation handle it.
    return {
        "url": url,
        "title": title,
        "content": text,
        "raw_content": text,
        "metadata": {"sourceURL": url, "content_type": content_type},
    }


def _ensure_web_plugins_loaded() -> None:
    """Idempotently run plugin discovery so the web registry is populated. Dispatch is reachable from contexts
    that never triggered discovery (subprocess agent runs, delegate children, scripts); without it a
    configured backend yields a misleading "No web ... provider" error.

    Every bundled web provider (brave-free, ddgs, searxng, exa, parallel, tavily, firecrawl, keenable)
    registers itself via ``plugins/web/<vendor>/__init__.py`` during plugin discovery. Tool dispatch can be
    reached from contexts that haven't already triggered discovery — subprocess agent runs, delegate
    children, standalone scripts, certain test paths — and without it the registry is empty and
    ``get_provider('firecrawl')`` returns ``None`` even when the user has ``web.extract_backend: firecrawl``
    configured and ``FIRECRAWL_API_KEY`` set. See #27580.
    """
    try:
        from hermes_cli.plugins import _ensure_plugins_discovered
        _ensure_plugins_discovered()
    except Exception as exc:  # noqa: BLE001
        # Warning, not debug: a broken plugin import is otherwise invisible.
        logger.warning("Web plugin discovery failed (non-fatal): %s", exc)


def _finish_debug(call_name: str, debug_call_data: dict, error_msg: Optional[str] = None) -> Optional[str]:
    """Log the call into the debug session; with *error_msg*, record it and return its ``tool_error`` envelope."""
    if error_msg is not None:
        logger.debug("%s", error_msg)
        debug_call_data["error"] = error_msg
    _debug.log_call(call_name, debug_call_data)
    _debug.save()
    return None if error_msg is None else tool_error(error_msg)


def web_search_tool(query: str, limit: int = 5) -> str:
    """Search the web via the configured backend.

    Returns a JSON string ``{"success": bool, "data": {"web": [{"title", "url", "description", "position"},
    ...]}}`` (metadata only — use web_extract_tool for page content) or ``{"success": false, "error": ...}``.
    """
    try:
        limit = min(max(int(limit), 1), 100)
    except (TypeError, ValueError):
        limit = 5
    debug_call_data = {
        "parameters": {"query": query, "limit": limit}, "error": None, "results_count": 0,
        "original_response_size": 0, "final_response_size": 0,
    }

    try:
        from tools.interrupt import is_interrupted
        if is_interrupted():
            return tool_error("Interrupted", success=False)
        # Sync only — every provider's search() is sync.
        _ensure_web_plugins_loaded()
        from agent.web_search_registry import get_active_search_provider, get_provider as _wsp_get_provider
        backend = _get_search_backend()
        provider = _wsp_get_provider(backend) if backend else None
        if provider is None or not provider.supports_search():
            if provider is None and backend and selection_exists("web"):
                error_text = debug_call_data["error"] = _strict_selection_error("search", backend)
                _finish_debug("web_search_tool", debug_call_data)
                return json.dumps({"success": False, "error": error_text}, indent=2, ensure_ascii=False)
            # Never-configured install: legacy availability-walked autodetect.
            provider = get_active_search_provider()

        if provider is None:
            fallback = "No web search provider configured. Run `hermes tools` to set one up."
            response_data = {"success": False, "error": _no_provider_error("search", fallback)}
        else:
            logger.info("Web search via %s: '%s' (limit: %d)", provider.name, query, limit)
            response_data = _memoized_search(provider, query, limit)

        debug_call_data["results_count"] = len(response_data.get("data", {}).get("web", []))
        result_json = json.dumps(response_data, indent=2, ensure_ascii=False)
        debug_call_data["final_response_size"] = len(result_json)
        _finish_debug("web_search_tool", debug_call_data)
        return result_json
    except Exception as e:
        return _finish_debug("web_search_tool", debug_call_data, f"Error searching web: {str(e)}")


def _memoized_search(provider, query: str, limit: int) -> dict:
    """TTL memo + single-flight around the paid vendor call (tools/web_result_cache.py); sits after every
    safety/config check. The provider is asked for the BUCKETED count so near-identical limits share an entry;
    the caller's count is sliced out. Only successful, non-rescued responses are cached — caching a rescue
    would make the one-shot ring fallback sticky for a whole TTL."""
    from tools.web_result_cache import bucket_limit, search_memo, slice_search_response

    def _paid_search() -> tuple[dict, bool]:
        fetch_limit = bucket_limit(limit)
        try:
            resp = provider.search(query, fetch_limit)
        except Exception as exc:  # noqa: BLE001 — candidate for rescue
            if not _rescue_eligible(provider):
                raise
            return _rescue_search(provider.name, str(exc), query, fetch_limit), True
        if not resp.get("success") and _rescue_eligible(provider):
            return _rescue_search(provider.name, str(resp.get("error", "")), query, fetch_limit), True
        return resp, False

    response_data = search_memo.lookup(provider.name, query, limit)
    if response_data is None:
        with search_memo.flight_lock(provider.name, query, limit):
            # Re-check inside the lock: a concurrent identical call may have stored.
            response_data = search_memo.lookup(provider.name, query, limit)
            if response_data is None:
                response_data, was_rescued = _paid_search()
                if not was_rescued:
                    search_memo.store(provider.name, query, limit, response_data)
    return slice_search_response(response_data, limit)


async def web_extract_tool(urls: List[Any], format: str = None, char_limit: Optional[int] = None) -> str:
    """Extract clean page content (no LLM) from URLs via the configured backend.

    Pages over ``char_limit`` (default web.extract_char_limit or 15000) are head+tail truncated with a footer
    pointing at the stored full text; inline base64 images become ``[IMAGE: alt]``. URLs carrying secrets are
    refused before any fetch; private-network URLs are blocked per entry. Returns JSON ``{"results": [...]}``.
    """
    normalized_urls, normalized_indices, invalid_urls, blocked = _validate_extract_urls(urls)
    if blocked is not None:
        return blocked
    debug_call_data = {
        "parameters": {"urls": normalized_urls, "format": format, "char_limit": char_limit}, "error": None,
        "pages_extracted": 0, "pages_truncated": 0, "original_response_size": 0, "final_response_size": 0,
        "truncation_metrics": [], "processing_applied": [],
    }

    try:
        logger.info("Extracting content from %d URL(s)", len(normalized_urls))
        # SSRF protection — filter private/internal URLs before any backend.
        safe_urls, safe_indices, ssrf_blocked = [], [], {}
        for index, url in zip(normalized_indices, normalized_urls):
            if await async_is_safe_url(url):
                safe_urls.append(url)
                safe_indices.append(index)
            else:
                ssrf_blocked[index] = _result_entry(
                    url, "Blocked: URL targets a private or internal network address"
                )

        results = []
        if safe_urls:
            backend = _get_extract_backend()

            # Fork: "local" backend uses tiered local fetcher (curl_cffi → scrapling → httpx)
            # with auto-fallback to web.backend cloud provider on failure.
            if backend == "local":
                from agent.web_search_registry import (
                    get_active_extract_provider,
                    get_provider as _wsp_get_provider,
                )
                from tools.interrupt import is_interrupted as _is_interrupted

                # Phase 1: local fetch — position-aligned so upstream's
                # input-order reconstruction (by_index via safe_indices)
                # assigns each result to the correct URL.
                results = [None] * len(safe_urls)
                failed_positions = []  # (position, url) for local failures
                for pos, u in enumerate(safe_urls):
                    if _is_interrupted():
                        results[pos] = {"url": u, "error": "Interrupted", "title": ""}
                        continue
                    local_result = await _fetch_and_process_locally(u, timeout=60)
                    if local_result is not None:
                        results[pos] = local_result
                    else:
                        failed_positions.append((pos, u))

                # Phase 2: fallback to web.backend for failed URLs
                if failed_positions:
                    failed_urls = [u for _, u in failed_positions]
                    # Resolve cloud fallback: try web.backend first, then extract_backend,
                    # then active extract provider walk
                    cfg = _load_web_config()
                    fallback_backend = (cfg.get("backend") or "").lower().strip()
                    # If web.backend is search-only, try extract_backend explicitly
                    if fallback_backend in {"searxng", "brave-free", "ddgs"}:
                        fallback_backend = (cfg.get("extract_backend") or "").lower().strip()
                        if fallback_backend in {"searxng", "brave-free", "ddgs", "local", ""}:
                            fallback_backend = None
                    if fallback_backend and _is_backend_available(fallback_backend):
                        fb_provider = _wsp_get_provider(fallback_backend)
                    else:
                        fb_provider = get_active_extract_provider()

                    if fb_provider is not None and fb_provider.supports_extract():
                        logger.info(
                            "Local extract failed for %d URL(s), falling back to %s",
                            len(failed_urls), fb_provider.name,
                        )
                        import inspect
                        if inspect.iscoroutinefunction(fb_provider.extract):
                            fallback_results = await fb_provider.extract(failed_urls, format=format)
                        else:
                            fallback_results = await asyncio.to_thread(
                                fb_provider.extract, failed_urls, format=format
                            )
                        # Place fallback results back into their original positions
                        for (pos, _u), fb_res in zip(failed_positions, fallback_results):
                            results[pos] = fb_res
                    else:
                        # No extract-capable fallback available
                        for pos, u in failed_positions:
                            results[pos] = {
                                "url": u, "title": "", "content": "",
                                "error": "Local fetch failed — all local fetchers exhausted and no extract-capable cloud backend available. Set web.backend or web.extract_backend to firecrawl, keenable, exa, or parallel.",
                            }
            # All bundled providers (brave-free, ddgs, searxng, exa, parallel,
            # tavily, firecrawl, keenable) now live as plugins. The dispatcher is a
            # registry lookup + delegation. Some providers' extract() is
            # async (parallel, firecrawl), others sync (exa, tavily, keenable) — we
            # detect coroutine functions and await; sync functions run
            # inline (the policy gate, SSRF re-check, etc. live inside the
            # provider itself for the firecrawl per-URL loop).
            elif backend != "local":
                _ensure_web_plugins_loaded()
                from agent.web_search_registry import (
                    get_active_extract_provider,
                    get_provider as _wsp_get_provider,
                    _disabled_web_plugin_for,
                )

                provider = _wsp_get_provider(backend) if backend else None
                if provider is None or not provider.supports_extract():
                    # When the configured name IS registered but doesn't support
                    # extract (search-only providers like brave-free / ddgs /
                    # searxng), surface that as a typed "search-only" error
                    # rather than silently switching backends. When the name
                    # isn't registered at all (typo / uninstalled plugin), fall
                    # through to the active-provider walk.
                    if provider is not None and not provider.supports_extract():
                        return json.dumps(
                            {
                                "success": False,
                                "error": (
                                    f"{provider.display_name} is a search-only "
                                    "backend and cannot extract URL content. "
                                    "Set web.extract_backend to firecrawl, "
                                    "tavily, keenable, exa, or parallel."
                                ),
                            },
                            ensure_ascii=False,
                        )
                    from tools.tool_backend_helpers import (
                        selection_error,
                        selection_exists,
                    )

                    if backend and selection_exists("web"):
                        # Strict selection: a stored-but-unregistered backend
                        # errors by name instead of silently switching to
                        # whatever the availability walk finds.
                        disabled_key = _disabled_web_plugin_for(capability="extract")
                        if disabled_key:
                            _vendor = disabled_key.split("/", 1)[-1]
                            error_text = (
                                f"web.extract_backend is set to '{_vendor}', but "
                                f"its plugin ('{disabled_key}') is disabled in "
                                f"config. Re-enable it with `hermes plugins "
                                f"enable {disabled_key}` (or remove it from "
                                "plugins.disabled)."
                            )
                        else:
                            error_text = selection_error(
                                "web",
                                f"'{backend}'",
                                "no registered web extract provider has that name",
                            )
                        return json.dumps(
                            {"success": False, "error": error_text},
                            ensure_ascii=False,
                        )
                    provider = get_active_extract_provider()
                    if provider is None:
                        # If the configured backend is a bundled web plugin the
                        # user explicitly disabled, the backend is set correctly
                        # and the real fix is to re-enable the plugin — say so
                        # instead of telling them to set web.extract_backend
                        # (which they already did). #40190 follow-up.
                        disabled_key = _disabled_web_plugin_for(capability="extract")
                        if disabled_key:
                            _vendor = disabled_key.split("/", 1)[-1]
                            return json.dumps(
                                {
                                    "success": False,
                                    "error": (
                                        f"web.extract_backend is set to '{_vendor}', "
                                        f"but its plugin ('{disabled_key}') is disabled "
                                        "in config. Re-enable it with "
                                        f"`hermes plugins enable {disabled_key}` "
                                        "(or remove it from plugins.disabled)."
                                    ),
                                },
                                ensure_ascii=False,
                            )
                        return json.dumps(
                            {
                                "success": False,
                                "error": (
                                    "No web extract provider configured. "
                                    "Set web.extract_backend to firecrawl, "
                                    "tavily, keenable, exa, or parallel."
                                ),
                            },
                            ensure_ascii=False,
                        )


                # ── Extract cache (tools/web_result_cache.py) ─────────────────
                # Disk-backed via cache/web: a URL extracted within the TTL is
                # served from disk instead of re-scraped. Deliberately placed
                # AFTER the secret-URL gate, SSRF gate, provider resolution, and
                # strict-selection validation, and gated per-URL on the website
                # blocklist policy — a hit skips only the vendor call, never a
                # control. Policy-blocked URLs are treated as cache misses so
                # dispatch handles them exactly as it would without a cache.
                # Keys include the provider and format, so switching backends or
                # formats within the TTL never serves the other's content.
                from tools.web_result_cache import (
                    extract_cache_get as _extract_cache_get,
                    extract_cache_put as _extract_cache_put,
                )
                from tools.website_policy import check_website_access as _check_site
                cached_results: Dict[int, Dict[str, Any]] = {}
                fetch_urls: List[str] = []
                fetch_positions: List[int] = []
                for position, url in enumerate(safe_urls):
                    hit = None
                    try:
                        _policy_block = _check_site(url)
                    except Exception:  # noqa: BLE001 — policy errors fail open like dispatch
                        _policy_block = None
                    if _policy_block is None:
                        hit = _extract_cache_get(
                            url, format=format, provider=provider.name
                        )
                    if hit is not None:
                        cached_results[position] = hit
                    else:
                        fetch_urls.append(url)
                        fetch_positions.append(position)

                if not fetch_urls:
                    results = [cached_results[i] for i in range(len(safe_urls))]
                else:
                    logger.info(
                        "Web extract via %s: %d URL(s)", provider.name, len(fetch_urls)
                    )

                    # Async-or-sync dispatch: parallel + firecrawl have async
                    # extract(); exa + tavily + keenable are sync.
                    import inspect
                    _extract_rescued = False
                    try:
                        if inspect.iscoroutinefunction(provider.extract):
                            results = await provider.extract(fetch_urls, format=format)
                        else:
                            # Run sync extract() in a thread so we don't block the
                            # event loop on network I/O.
                            results = await asyncio.to_thread(
                                provider.extract, fetch_urls, format=format
                            )
                    except Exception as exc:  # noqa: BLE001 — candidate for rescue
                        if _rescue_eligible(provider):
                            _extract_rescued = True
                            failed = [
                                {"url": u, "title": "", "content": "", "error": str(exc)}
                                for u in fetch_urls
                            ]
                            results = await asyncio.to_thread(
                                _rescue_extract, provider.name, fetch_urls, failed
                            )
                        else:
                            raise
                    else:
                        # One-shot keyless rescue when the WHOLE batch failed
                        # (backend-level outage, not per-page problems). Stateless:
                        # the next web_extract call uses the chosen backend again.
                        if (
                            results
                            and all(r.get("error") for r in results)
                            and _rescue_eligible(provider)
                        ):
                            _extract_rescued = True
                            results = await asyncio.to_thread(
                                _rescue_extract, provider.name, fetch_urls, results
                            )

                    # Cache each successful fetch's full clean text for TTL reuse
                    # (best-effort; oversized pages are skipped by the cache).
                    # NEVER cache a rescue-served batch: it came from a ring
                    # vendor, not the chosen backend, and caching it would make
                    # the one-shot rescue sticky for a whole TTL — the next call
                    # must attempt the chosen backend again.
                    if not _extract_rescued:
                        for fetched_pos, fetched in enumerate(results):
                            if fetched_pos >= len(fetch_urls):
                                break
                            if fetched.get("error"):
                                continue
                            _content = (
                                fetched.get("raw_content", "") or fetched.get("content", "")
                            )
                            if _content:
                                _extract_cache_put(
                                    fetch_urls[fetched_pos],
                                    _content,
                                    title=fetched.get("title", ""),
                                    format=format,
                                    provider=provider.name,
                                )

                    # Merge fetched results back with cache hits, restoring the
                    # safe_urls order the downstream reconstruction expects.
                    if cached_results:
                        merged: List[Dict[str, Any]] = [None] * len(safe_urls)  # type: ignore[list-item]
                        for position, hit in cached_results.items():
                            merged[position] = hit
                        for fetched_pos, position in enumerate(fetch_positions):
                            merged[position] = (
                                results[fetched_pos]
                                if fetched_pos < len(results)
                                else {
                                    "url": safe_urls[position],
                                    "title": "",
                                    "content": "",
                                    "error": "Extract backend returned no result for this URL",
                                }
                            )
                        results = merged

        # Reconstruct the original input order across invalid, blocked, and
        # provider-processed entries. Providers are expected to preserve the
        # order of the safe URL list they receive.
        if invalid_urls or ssrf_blocked:
            fixed = {**ssrf_blocked, **invalid_urls}
            results = _merge_in_order(len(urls), fixed, safe_indices, safe_urls, results)

        logger.info("Extracted content from %d pages", len(results))
        debug_call_data["pages_extracted"] = len(results)
        debug_call_data["original_response_size"] = len(json.dumps({"results": results}))
        debug_call_data["processing_applied"].append("truncate_and_store")
        _truncate_results(results, _effective_char_limit(char_limit), debug_call_data)
        trimmed = _trim_results(results)
        result_json = (
            json.dumps({"results": trimmed}, indent=2, ensure_ascii=False) if trimmed
            else tool_error("Content was inaccessible or not found")
        )
        # Belt-and-suspenders sweep of the serialized JSON: a provider may tuck a base64 blob in metadata.
        cleaned_result = convert_base64_images_to_links(result_json)
        debug_call_data["final_response_size"] = len(cleaned_result)
        debug_call_data["processing_applied"].append("base64_image_conversion")
        _finish_debug("web_extract_tool", debug_call_data)
        return cleaned_result
    except Exception as e:
        return _finish_debug("web_extract_tool", debug_call_data, f"Error extracting content: {str(e)}")


def _provider_is_ready(provider) -> bool:
    """True when *provider* is keyed-available OR keyless-capable, without raising.

    ``get_active_*_provider()`` returns an explicitly configured backend even when ``is_available()`` is
    False (so dispatch can emit a precise error), so readiness gates (tool check_fn, ``hermes doctor``)
    must probe for real. Keyless mode (Exa/Parallel free tier) is a working state, not a misconfig.

    See #78412.
    """
    if provider is None:
        return False
    ready = _probe(provider, "is_available", " during readiness check")
    if ready is None:  # broken provider == not ready; don't try the keyless probe
        return False
    return bool(ready or _probe(provider, "is_keyless_available", " during readiness check"))


def check_web_api_key() -> bool:
    """``check_fn`` gate for web_search / web_extract: is any web backend available?

    A plugin-registered provider reporting ``is_available()`` must light the tools up even with no
    built-in credentials; resolution funnels through :func:`_is_backend_available`.

    See #28651, #31873.
    """
    # Boolean OR over configured + built-ins — probe order is irrelevant here.
    candidates = [c for c in (_configured_backend(),) if c] + list(_LEGACY_WEB_BACKENDS)
    if any(_is_backend_available(backend) for backend in candidates):
        return True
    # Plugin path. Discovery must run first: check_fn fires at tool-registration time, before any dispatch.
    try:
        _ensure_web_plugins_loaded()
        from agent.web_search_registry import get_active_search_provider, get_active_extract_provider
        return _provider_is_ready(get_active_search_provider()) or _provider_is_ready(
            get_active_extract_provider()
        )
    except Exception as exc:  # noqa: BLE001 — registry optional; never fatal
        logger.debug("web provider registry availability check failed: %s", exc)
        return False


# ─── Registry ─────────────────────────────────────────────────────────────────
from tools.registry import registry, tool_error

WEB_SEARCH_SCHEMA = {
    "name": "web_search",
    "description": "Search the web for information. Returns up to 5 results by default with titles, URLs, and descriptions. The query is passed through to the configured backend, so operators such as site:domain, filetype:pdf, intitle:word, -term, and \"exact phrase\" may work when the backend supports them.",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The search query to look up on the web. You may include backend-supported operators such as site:example.com, filetype:pdf, intitle:word, -term, or \"exact phrase\"."
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of results to return. Defaults to 5.",
                "minimum": 1,
                "maximum": 100,
                "default": 5
            }
        },
        "required": ["query"]
    }
}

_EXTRACT_DESC = (
    "Extract content from web page URLs. Returns clean page content in markdown/text "
    "(no LLM summarization — fast). Also works with PDF URLs (arxiv papers, documents) — "
    "pass the PDF link directly. Pages within the char budget (default 400000) return whole; "
    "larger pages return a head+tail window with a footer telling you the full text's saved "
    "file path and the read_file call to page through the omitted middle. Inline images appear "
    "as [IMAGE: alt] placeholders; real image URLs are kept as links. If a URL fails or times "
    "out, use the browser tool instead."
)
# Fork: surface local extract backend awareness so the agent knows which fetcher is in use.
if _get_extract_backend() == "local":
    _EXTRACT_DESC += (
        " NOTE: web_extract currently uses the LOCAL tiered fetcher "
        "(curl_cffi → Scrapling → httpx) + trafilatura for text extraction."
    )

WEB_EXTRACT_SCHEMA = {
    "name": "web_extract",
    "description": _EXTRACT_DESC,
    "parameters": {
        "type": "object",
        "properties": {
            "urls": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of URLs to extract content from (max 5 URLs per call)",
                "maxItems": 5
            },
            "char_limit": {
                "type": "integer",
                "description": "Optional per-page character budget sent back (default 400000). Pages larger than this are head+tail truncated with the full text stored to disk. Raise it when you need more of a long page inline.",
                "minimum": 2000
            }
        },
        "required": ["urls"]
    }
}

registry.register(
    name="web_search", toolset="web", schema=WEB_SEARCH_SCHEMA,
    handler=lambda args, **kw: web_search_tool(args.get("query", ""), limit=args.get("limit", 5)),
    check_fn=check_web_api_key, requires_env=_web_requires_env(), emoji="🔍",
    max_result_size_chars=100_000,
)
registry.register(
    name="web_extract", toolset="web", schema=WEB_EXTRACT_SCHEMA,
    handler=lambda args, **kw: web_extract_tool(
        args.get("urls", [])[:5] if isinstance(args.get("urls"), list) else [], "markdown",
        char_limit=args.get("char_limit"),
    ),
    check_fn=check_web_api_key,
    requires_env=_web_requires_env(),
    is_async=True,
    emoji="📄",
    max_result_size_chars=500_000,
)


# ---- BEGIN PLUGIN-COMPAT (revert-scheduled; see COMPAT_MANIFEST.md) ----
# Names external plugins imported from this module before the Sep 2026 decomposition.
# Internal code MUST NOT use these (scripts/check_compat_pointers.py fails CI if it does).
# The whole block is removed by reverting the commit that added it.
from typing import Dict  # noqa: F401,E402
from typing import TYPE_CHECKING  # noqa: F401,E402
import asyncio  # noqa: F401,E402
import httpx  # noqa: F401,E402
import re  # noqa: F401,E402
import sys  # noqa: F401,E402


_PLUGIN_COMPAT_LAZY = {
    'DEFAULT_EXTRACT_CHAR_LIMIT': ('tools.web_tools_truncate', 'DEFAULT_EXTRACT_CHAR_LIMIT'),
    'Firecrawl': ('plugins.web.firecrawl.provider', 'Firecrawl'),
    'MAX_STORED_TEXT_CHARS': ('tools.web_tools_truncate', 'MAX_STORED_TEXT_CHARS'),
    'build_vendor_gateway_url': ('tools.managed_tool_gateway', 'build_vendor_gateway_url'),
    'managed_nous_tools_enabled': ('tools.tool_backend_helpers', 'managed_nous_tools_enabled'),
    'normalize_url_for_request': ('tools.url_safety', 'normalize_url_for_request'),
    'nous_tool_gateway_unavailable_message': ('tools.tool_backend_helpers', 'nous_tool_gateway_unavailable_message'),
    'prefers_gateway': ('tools.tool_backend_helpers', 'prefers_gateway'),
    'resolve_managed_tool_gateway': ('tools.managed_tool_gateway', 'resolve_managed_tool_gateway'),
    'sensitive_query_param_name': ('tools.url_safety', 'sensitive_query_param_name'),
}


def __getattr__(name):  # PEP 562 — lazy so no import cycles
    target = _PLUGIN_COMPAT_LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib
    from hermes_cli.plugin_compat import warn_once
    warn_once(__name__, name, *target)
    return getattr(importlib.import_module(target[0]), target[1])
# ---- END PLUGIN-COMPAT ----
