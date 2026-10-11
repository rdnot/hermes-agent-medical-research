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
import re
from typing import Any, Callable, Dict, List, Optional
# Per-vendor client cache slots; plugins read/write these via tools.web_tools (tests reset them to None).
_firecrawl_client = _firecrawl_client_config = _parallel_client = _async_parallel_client = _exa_client = None

# ─── Optional Local Fetcher Dependencies (fork) ───────────────────────────────
# The ``web-local`` extra (pyproject.toml). Absent on a fresh checkout: web_extract's local
# branch installs it through PM on first use (tools/web_tools_local_deps.py) and rebinds
# these names, so the flags below are the import-time state only.

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
    import pymupdf as fitz  # PyMuPDF (the `fitz` alias import is deprecated upstream)
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
from tools.tool_backend_helpers import NOUS_MANAGED_PROVIDER, read_selection, selection_exists
from tools.url_safety import SSRFConnectionBlocked, async_is_safe_url, create_ssrf_safe_async_client
from tools.website_policy import check_website_access
from tools.web_tools_rescue import _managed_search_fallback, _rescue_eligible, _rescue_search
from tools.web_tools_truncate import _effective_char_limit, _trim_results, _truncate_results, convert_base64_images_to_links
from tools.web_tools_extract import (
    _dispatch_extract, _extract_safe_urls, _merge_in_order, _no_provider_error, _resolve_extract_provider, _result_entry,
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
    except Exception as exc:
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
    except Exception as exc:
        name = getattr(provider, "name", provider)
        logger.debug("web provider %r.%s() raised%s: %s", name, method, context, exc)
        return None


def _get_backend() -> str:
    """Shared web backend name. A stored ``web.backend`` is returned as-is — no availability probe, no
    fallback — so a broken selection surfaces the vendor's honest error rather than silently rerouting.
    The managed ``use_gateway`` selection also resolves to firecrawl with no ladder. Autodetect runs
    whenever no SHARED web selection was ever stored: per-capability keys (``web.search_backend``,
    ``web.extract_backend``) name only their own capability and never reroute the other (#113017)."""
    configured = _configured_backend()
    if configured:
        # "nous" (managed subscription) is serviced by firecrawl, routed through the managed Tool Gateway.
        return "firecrawl" if configured == NOUS_MANAGED_PROVIDER else configured
    if read_selection("web") is not None:
        # Shared selection exists (use_gateway) but no shared name: firecrawl, no ladder.
        return "firecrawl"

    # Never-configured install.
    return _autodetect_backend() or _keyless_backend() or "firecrawl"  # default (backward compat)


def _autodetect_backend() -> Optional[str]:
    """Autodetect rungs above the keyless tier, or None. Explicit user credentials beat the managed-gateway
    probe (a Nous OAuth token's tier may not grant web access; the gateway then fails at runtime with no
    fallback). Free tiers trail paid."""
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
    return None


def _keyless_backend() -> Optional[str]:
    """Keyless free-tier backend name, or None. Strictly the last autodetect rung so it never
    pre-empts a keyed backend. Discovery must run first: reachable from contexts that haven't
    loaded plugins (subprocess runs, delegate children)."""
    try:
        _ensure_web_plugins_loaded()
        from agent.web_search_registry import _keyless_preference, _keyless_tier_enabled
        if _keyless_tier_enabled():
            for name in _keyless_preference():
                provider = _registered_web_provider(name)
                if provider is not None and _probe(provider, "is_keyless_available"):
                    return name
    except Exception as exc:
        logger.debug("keyless fallback walk failed: %s", exc)
    return None


def _managed_web_search() -> bool:
    """True when web_search is on the managed Nous route: the stored ``nous`` selection (a
    ``web.search_backend: nous`` pin, else the shared one), or a never-configured install whose
    autodetect lands on the gateway — the entitled Firecrawl gateway, or free Perplexity fast search
    for any Nous identity when nothing else is configured (search-only: the extract ladder is
    untouched). A stored vendor selection never is."""
    search_pin = _configured_backend("search_backend")
    if search_pin:
        return search_pin == NOUS_MANAGED_PROVIDER
    selected = read_selection("web")
    if selected is not None:
        return selected == NOUS_MANAGED_PROVIDER
    backend = _autodetect_backend()
    if backend == "firecrawl":
        return not (_has_env("FIRECRAWL_API_KEY") or _has_env("FIRECRAWL_API_URL")) and _is_tool_gateway_ready()
    from tools.managed_tool_gateway import peek_nous_access_token, resolve_free_search_gateway
    return backend is None and resolve_free_search_gateway(token_reader=peek_nous_access_token) is not None


def _get_search_backend() -> str:
    """Backend for web_search: ``web.search_backend`` (strict, no probe) > ``web.backend`` > autodetect.
    The managed Nous route (a ``nous`` pin or shared selection) serves search from Perplexity (extract
    stays on Firecrawl); managed Firecrawl is the per-call fallback, see ``_memoized_search``."""
    pin = _configured_backend("search_backend")
    if pin and pin != NOUS_MANAGED_PROVIDER:
        return pin
    return "perplexity" if _managed_web_search() else _get_backend()


def _get_extract_backend() -> str:
    """Backend for web_extract: ``web.extract_backend`` (strict, no probe) > ``web.backend`` > autodetect.
    A ``nous`` pin is managed Firecrawl; the client picks the gateway route for that capability.

    Fork: ``web.extract_backend == "local"`` routes to the tiered local fetcher
    (curl_cffi → Scrapling → httpx) with auto-fallback to a cloud provider.
    """
    pin = _configured_backend("extract_backend")
    if pin == "local":
        return "local"
    return "firecrawl" if pin == NOUS_MANAGED_PROVIDER else pin or _get_backend()


def _ddgs_package_importable() -> bool:
    """ddgs is the only backend gated on package presence; single symbol so tests can patch it."""
    try:
        import ddgs
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
    "perplexity": lambda: _has_env("PERPLEXITY_API_KEY") or _managed_web_search(),
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
        "FIRECRAWL_API_URL", "FIRECRAWL_GATEWAY_URL", "PERPLEXITY_GATEWAY_URL", "TOOL_GATEWAY_DOMAIN", "TOOL_GATEWAY_SCHEME",
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


_NCBI_BROWSER_HOSTS = ("pubmed.ncbi.nlm.nih.gov", "pmc.ncbi.nlm.nih.gov")
# Below this much visible text a page that matched no article marker is a title-only shell.
_NCBI_ARTICLE_MIN_VISIBLE_CHARS = 2_500


def _is_ncbi_article_url(url: str) -> bool:
    lowered = url.lower()
    return any(host in lowered for host in _NCBI_BROWSER_HOSTS)


def _visible_text(raw_html: str) -> str:
    text = re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>|<[^>]+>", " ", raw_html, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", text).strip()


def _is_recaptcha_challenge(content_bytes: bytes) -> bool:
    """NCBI interstitials served with a 2xx status instead of the article: the reCAPTCHA Enterprise
    page ("Checking your browser") and the newer proof-of-work shell ("Cookies must be enabled",
    often HTTP 203). The cookie wording counts only on a shell-sized page, so an article that merely
    mentions cookies passes."""
    raw = content_bytes.decode("utf-8", errors="replace").lower()
    if "checking your browser" in raw and "recaptcha" in raw:
        return True
    return "cookies must be enabled" in raw and len(raw) < 20_000


def _has_pubmed_article_content(content_bytes: bytes) -> bool:
    """True when PubMed/PMC HTML carries the article rather than a shell/challenge. Known layout
    markers are accepted directly; because NCBI changes its markup, a non-challenge page with
    substantial visible text also counts (title-only shells have a few hundred characters)."""
    if _is_recaptcha_challenge(content_bytes):
        return False
    raw = content_bytes.decode("utf-8", errors="replace").lower()
    if any(marker in raw for marker in (
        'id="main-content"', 'id="article-container"', "pmc-article-section",
        "article-body", 'class="abstract"', 'section class="abstract"',
        'class="main-article-body"', "pmc-layout", 'aria-label="article content"',
    )):
        return True
    return len(_visible_text(raw)) >= _NCBI_ARTICLE_MIN_VISIBLE_CHARS


def _bookshelf_has_content(content_bytes: bytes) -> bool:
    if _is_recaptcha_challenge(content_bytes):
        return False
    return len(_visible_text(content_bytes.decode("utf-8", errors="replace"))) >= _NCBI_ARTICLE_MIN_VISIBLE_CHARS


async def _page_html_or_empty(page: Any) -> bytes:
    """Rendered DOM, or b"" while the page is navigating (NCBI reloads after its challenge)."""
    try:
        return (await page.content()).encode("utf-8", errors="replace")
    except Exception:
        return b""


async def _settle(page: Any, ms: int) -> None:
    try:
        await page.wait_for_timeout(ms)
    except Exception:
        await asyncio.sleep(ms / 1000)


async def _wait_for_ncbi_content(
    page: Any, has_content: Callable[[bytes], bool], *, label: str,
    poll_ms: int = 100, max_wait_ms: int = 25_000, reload_after_ms: int = 12_000,
) -> bool:
    """Poll the live DOM until *has_content* accepts it. NCBI's interstitial (reCAPTCHA Enterprise or
    the cookie proof-of-work page) solves itself and then calls ``location.reload()``; during that
    navigation every call on the old document raises, so each probe tolerates errors and keeps
    polling. One manual reload is attempted midway in case the interstitial's redirect never fires.
    Returns whether content appeared."""
    waited, reloaded, challenge_seen = 0, False, False
    while waited <= max_wait_ms:
        html = await _page_html_or_empty(page)
        if html and has_content(html):
            if challenge_seen:
                logger.info("%s: challenge cleared, content loaded after %d ms", label, waited)
            return True
        if html and not challenge_seen and _is_recaptcha_challenge(html):
            challenge_seen = True
            logger.info("%s challenge detected — waiting for content", label)
        if waited >= reload_after_ms and not reloaded:
            reloaded = True
            logger.debug("%s: interstitial persists, attempting page.reload()", label)
            try:
                await page.reload(wait_until="domcontentloaded", timeout=15_000)
            except Exception:
                pass
        await _settle(page, poll_ms)
        waited += poll_ms
    logger.debug("%s: content still absent after %d ms", label, waited)
    return False


class _LocalFetchBlocked(Exception):
    """A hop of the local fetch targets a private address or a policy-blocked site. Carries the
    offending URL and, for a policy block, the block metadata; never falls back to a cloud backend."""

    def __init__(self, url: str, message: str, blocked: Optional[dict[str, str]] = None):
        super().__init__(message)
        self.url, self.message, self.blocked = url, message, blocked


_UNSAFE_HOP_MSG = "Blocked: URL targets a private or internal network address"
_MAX_LOCAL_REDIRECTS = 10


async def _guard_hop(url: str) -> None:
    """Re-run the SSRF filter and the website blocklist on a URL the fetch is about to follow
    (or landed on). The tool checked only the model-supplied URL; every redirect hop needs the same
    two gates, the way the firecrawl provider re-checks its post-redirect URL."""
    if not await async_is_safe_url(url):
        raise _LocalFetchBlocked(url, _UNSAFE_HOP_MSG)
    if blocked := check_website_access(url):
        raise _LocalFetchBlocked(url, blocked["message"], blocked)


async def _fetch_raw(url: str, timeout: int = 60) -> tuple[bytes, dict, int, str, str]:
    """
    Fetch URL bytes with tiered fallback strategy:
      1. curl_cffi           — Chrome TLS impersonation, fast, no browser
                               (skipped for Reddit — always needs real browser)
      2. AsyncStealthySession — stealth Playwright (Patchright), handles JS-rendered
                                pages: Reddit comments, Cloudflare, heavy SPAs.
                                solve_cloudflare auto-enabled when CF detected.
      3. httpx               — last resort, no stealth

    Redirects are followed one guarded hop at a time (curl_cffi), through the SSRF-safe transport
    (httpx), or re-checked on the landing URL (Scrapling); a hop into a private address or a
    policy-blocked site raises _LocalFetchBlocked, which no tier swallows.

    Returns (content_bytes, headers_dict, status_code, fetcher_name, final_url)
    """
    from urllib.parse import urljoin

    is_reddit = "reddit.com" in url.lower()

    # PubMed/PMC answer plain clients with a challenge shell — skip curl_cffi, go straight to Scrapling.
    # NCBI Bookshelf keeps curl_cffi (its SSR pages pass _is_content_sufficient) and gets the same
    # browser wait only if it falls through to Scrapling.
    is_pubmed = _is_ncbi_article_url(url)
    is_bookshelf = "ncbi.nlm.nih.gov/books/" in url.lower()
    if is_pubmed:
        logger.debug("PubMed detected — skipping curl_cffi, routing through Scrapling")

    curl_cffi_status: int | None = None
    curl_cffi_content: bytes | None = None

    # ── Tier 1: curl_cffi (Chrome TLS fingerprint, fast, no browser) ──────
    # Skipped for Reddit (JS shell / "prove you are human") and PubMed (reCAPTCHA)
    if HAS_CURL_CPERF and not is_reddit and not is_pubmed:
        try:
            logger.debug("Fetching with curl_cffi: %s", url)
            current = url
            for _hop in range(_MAX_LOCAL_REDIRECTS + 1):
                # Sync client in a thread so a slow origin never blocks the event loop; redirects are
                # followed here, not by curl, so each hop passes the SSRF + policy gates first.
                resp = await asyncio.to_thread(
                    curl_requests.get, current, timeout=timeout, impersonate="chrome", allow_redirects=False,
                )
                location = resp.headers.get("location") if 300 <= resp.status_code < 400 else None
                if not location:
                    break
                current = urljoin(current, location)
                await _guard_hop(current)
            else:
                raise _LocalFetchBlocked(current, f"Too many redirects (>{_MAX_LOCAL_REDIRECTS})")
            curl_cffi_status = resp.status_code
            curl_cffi_content = resp.content
            if resp.status_code < 400 and _is_content_sufficient(resp.content, current):
                return resp.content, dict(resp.headers), resp.status_code, "curl_cffi", current
            # status >= 400 or JS shell → fall through to browser tier
            logger.debug("curl_cffi: status=%d, content insufficient → escalating", resp.status_code)
        except _LocalFetchBlocked:
            raise
        except Exception as e:
            logger.debug("curl_cffi failed: %s", e)

    # ── Tier 2: Scrapling (Playwright-based, handles JS/Cloudflare) ───────
    if HAS_SCRAPLING:
        try:
            solve_cf = _is_cloudflare_protected(curl_cffi_status, curl_cffi_content)
            if solve_cf:
                logger.debug("Cloudflare detected → enabling solve_cloudflare")
            logger.debug("Fetching with Scrapling: %s (cf_solve=%s)", url, solve_cf)

            # NCBI interstitials (reCAPTCHA Enterprise / cookie proof-of-work) solve themselves and
            # reload. Scrapling's response body is the document served BEFORE that, so for NCBI the
            # page action waits for real content and captures the live DOM, which is what we keep.
            _ncbi_has_content: Optional[Callable[[bytes], bool]] = (
                _has_pubmed_article_content if is_pubmed else _bookshelf_has_content if is_bookshelf else None)
            _ncbi_label = "PubMed" if is_pubmed else "Bookshelf"
            _captured: dict[str, Any] = {}

            async def _ncbi_page_action(page):
                _captured["ready"] = await _wait_for_ncbi_content(page, _ncbi_has_content, label=_ncbi_label)
                for _ in range(10):  # the DOM may still be mid-navigation; retry briefly
                    html = await _page_html_or_empty(page)
                    if html:
                        _captured["html"] = html
                        return
                    await _settle(page, 200)

            # NCBI: one retry, but only when the first attempt saw content and lost it in the final
            # capture (a navigation race). A wait that ran out without the challenge clearing is not
            # retried: a second 25 s wait rarely helps, and httpx is next.
            _pubmed_max_attempts = 2 if _ncbi_has_content is not None else 1

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
                    if _ncbi_has_content is not None:
                        _captured.clear()
                        fetch_kwargs["page_action"] = _ncbi_page_action
                    # Hard timeout for the entire scrapling fetch including CF solving.
                    # Scrapling's _cloudflare_solver has unbounded recursion — each attempt
                    # takes ~12s, so without a cap it loops forever on unsolvable challenges.
                    _scrapling_hard_timeout = 45 if solve_cf else 60
                    try:
                        resp = await asyncio.wait_for(
                            session.fetch(**fetch_kwargs),
                            timeout=_scrapling_hard_timeout,
                        )
                    except TimeoutError:
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
                        _rendered = _captured.get("html")
                        if _rendered and len(_rendered) >= len(content) // 2:
                            # Prefer the rendered DOM unless it is suspiciously small next to the
                            # response body (captured mid-navigation).
                            content = _rendered
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
                        if _ncbi_has_content is not None and not _ncbi_has_content(content):
                            if _is_recaptcha_challenge(content):
                                reason = "challenge still present"
                            else:
                                reason = "article content markers absent"
                            if _pubmed_attempt < _pubmed_max_attempts - 1 and _captured.get("ready"):
                                logger.info(
                                    "%s: Scrapling returned %s after attempt %d/%d, retrying…",
                                    _ncbi_label, reason, _pubmed_attempt + 1, _pubmed_max_attempts,
                                )
                                continue
                            logger.warning("%s: Scrapling returned %s after attempt %d, skipping to next tier",
                                           _ncbi_label, reason, _pubmed_attempt + 1)
                            break
                        # The browser followed any redirects itself: re-gate the landing URL.
                        final_url = str(getattr(resp, "url", "") or url)
                        if final_url != url:
                            await _guard_hop(final_url)
                        headers = dict(resp.headers or {})
                        logger.debug("Scrapling fetch succeeded (status=%d)", resp.status)
                        return content, headers, resp.status, "scrapling", final_url
                    logger.debug("Scrapling returned status %d", resp.status if resp else -1)
                finally:
                    await session.close()
        except _LocalFetchBlocked:
            raise
        except Exception as e:
            logger.debug("Scrapling failed: %s", e)

    # ── Tier 3: httpx (last resort, no stealth) ───────────────────────────
    try:
        logger.debug("Fetching with httpx (fallback): %s", url)
        # Connect-time SSRF validation covers every redirect hop; the blocklist is re-checked on
        # the landing URL.
        async with create_ssrf_safe_async_client(follow_redirects=True) as client:
            resp = await client.get(url, timeout=timeout)
        final_url = str(resp.url)
        if final_url != url:
            await _guard_hop(final_url)
        return resp.content, dict(resp.headers), resp.status_code, "httpx", final_url
    except SSRFConnectionBlocked as e:
        raise _LocalFetchBlocked(url, _UNSAFE_HOP_MSG) from e
    except _LocalFetchBlocked:
        raise
    except Exception as e:
        logger.debug("httpx failed: %s", e)

    raise Exception(f"All local fetchers failed for {url}")


async def _fetch_jina(url: str, timeout: int = 30) -> Optional[dict[str, Any]]:
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


async def _fetch_and_process_locally(url: str, timeout: int = 60) -> Optional[dict[str, Any]]:
    """
    Fetch URL using tiered local fetchers and process content.
    
    Returns:
        Dict with url, title, content, raw_content, metadata
        OR None if local fetch should fall back to cloud API.
    
    Raises:
        Exception if fetch succeeds but processing fails.
    """
    try:
        content_bytes, headers, status_code, fetcher, final_url = await _fetch_raw(url, timeout)
    except _LocalFetchBlocked as blocked:
        logger.info("Blocked local web_extract for %s: %s", blocked.url, blocked.message)
        return _blocked_entry(url, blocked.message, blocked.blocked)
    except Exception as e:
        logger.debug("Local fetch failed for %s: %s", url, e)
        return None  # Signal to fall back to cloud API

    # Tiers 1-2 only return < 400; the httpx last resort returns whatever came back. An error page
    # is not an article: let the cloud fallback (or the per-URL error) handle it.
    if status_code >= 400:
        logger.info("Local fetch got HTTP %d for %s via %s; falling back", status_code, url, fetcher)
        return None

    content_type = headers.get("content-type", "text/html")

    # PubMed/PMC answer every raw tier with a 2xx challenge or title-only shell when they are not
    # satisfied. Never process/cache that as a 40-word "success": try Jina Reader, and if it cannot
    # get the article either, fall back like any other local failure. PDFs carry no HTML markers.
    if (
        _is_ncbi_article_url(url)
        and "application/pdf" not in content_type.lower()
        and not _has_pubmed_article_content(content_bytes)
    ):
        logger.warning("PubMed/PMC raw fetch returned non-article HTML via %s; trying Jina fallback", fetcher)
        jina_result = await _fetch_jina(url, timeout=30)
        if jina_result is not None:
            return jina_result
        logger.warning("PubMed/PMC: no local fetcher or Jina returned the article for %s; falling back", url)
        return None

    # Handle PDF
    if "application/pdf" in content_type or url.lower().endswith(".pdf"):
        try:
            text = _extract_pdf_text(content_bytes)
            return {
                "url": url,
                "title": f"PDF: {url.split('/')[-1]}",
                "content": text,
                "raw_content": text,
                "metadata": {"sourceURL": final_url, "content_type": "application/pdf"},
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
            "metadata": {"sourceURL": final_url, "content_type": content_type, "is_image": True},
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
        "metadata": {"sourceURL": final_url, "content_type": content_type},
    }


def _blocked_entry(url: str, message: str, blocked: Optional[dict[str, str]] = None) -> dict[str, Any]:
    """Per-URL entry for a fetch refused by the SSRF filter or the website blocklist. Same shape the
    firecrawl provider returns (``blocked_by_policy`` carries host/rule/source for a policy block)."""
    policy = {"blocked_by_policy": {k: blocked[k] for k in ("host", "rule", "source")}} if blocked else {}
    return {"url": url, "title": "", "content": "", "error": message, **policy}


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
    except Exception as exc:
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
        return _finish_debug("web_search_tool", debug_call_data, f"Error searching web: {e!s}")


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
        except Exception as exc:
            served = _served_after_failure(str(exc), fetch_limit)
            if served is None:
                raise
            return served, True
        if not resp.get("success"):
            served = _served_after_failure(str(resp.get("error", "")), fetch_limit)
            if served is not None:
                return served, True
        return resp, False

    def _served_after_failure(error: str, fetch_limit: int) -> Optional[dict]:
        """Managed Firecrawl for a failed managed Perplexity call, else the one-shot keyless rescue when
        eligible; None means the vendor's own failure stands."""
        fallback = _managed_search_fallback(provider, error, query, fetch_limit)
        if fallback is not None:
            return fallback
        return _rescue_search(provider.name, error, query, fetch_limit) if _rescue_eligible(provider) else None

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


async def web_extract_tool(urls: list[Any], format: str | None = None, char_limit: Optional[int] = None) -> str:
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
                from tools.web_tools_local_deps import ensure_chromium, ensure_local_fetcher_stack

                # First use on this machine: PM installs the web-local extra (recorded in its
                # ledger, so every later venv rebuild keeps it), then patchright fetches the
                # Chromium build the stealth tier needs. Both are no-ops once present. Off the
                # event loop: a venv sync or a browser download takes minutes.
                await asyncio.to_thread(ensure_local_fetcher_stack, globals())
                if HAS_SCRAPLING:
                    await asyncio.to_thread(ensure_chromium)

                # Phase 1: local fetch — position-aligned so upstream's
                # input-order reconstruction (by_index via safe_indices)
                # assigns each result to the correct URL.
                results = [None] * len(safe_urls)
                failed_positions = []  # (position, url) for local failures
                for pos, u in enumerate(safe_urls):
                    if _is_interrupted():
                        results[pos] = {"url": u, "error": "Interrupted", "title": ""}
                        continue
                    # Website blocklist runs before any fetch, as it does inside the cloud providers;
                    # a blocked URL is a final per-URL error, never a cloud-fallback candidate.
                    if blocked := check_website_access(u):
                        logger.info("Blocked web_extract for %s by rule %s", blocked["host"], blocked["rule"])
                        results[pos] = _blocked_entry(u, blocked["message"], blocked)
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
                        # Same capped dispatch as the cloud path: web.extract_timeout bounds the
                        # vendor call and a timed-out batch gets the one-shot keyless rescue.
                        fallback_results = await _dispatch_extract(fb_provider, failed_urls, format)
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
            else:
                _ensure_web_plugins_loaded()
                provider, error_json = _resolve_extract_provider(backend)
                if error_json is not None:
                    return error_json
                results = await _extract_safe_urls(provider, safe_urls, format)

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
        return _finish_debug("web_extract_tool", debug_call_data, f"Error extracting content: {e!s}")


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


# Credential probes that back other tools but serve no registered web backend: ``xai`` is
# probed via has_xai_credentials() for TTS/media only, so it must not light this gate. A
# stored ``web.backend: xai`` still counts since _get_backend returns a configured
# selection as-is and dispatch surfaces the honest "unknown provider" error.
_WEB_CHECK_SKIP = frozenset({"xai"})


def check_web_api_key() -> bool:
    """``check_fn`` gate for web_search / web_extract: is any web backend available?

    A plugin-registered provider reporting ``is_available()`` must light the tools up even with no
    built-in credentials; resolution funnels through :func:`_is_backend_available`.

    See #28651, #31873.
    """
    # Boolean OR over configured + built-ins — probe order is irrelevant here.
    candidates = ([c for c in (_configured_backend(),) if c]
                  + [b for b in _LEGACY_WEB_BACKENDS if b not in _WEB_CHECK_SKIP])
    if any(_is_backend_available(backend) for backend in candidates):
        return True
    # Plugin path. Discovery must run first: check_fn fires at tool-registration time, before any dispatch.
    try:
        _ensure_web_plugins_loaded()
        from agent.web_search_registry import get_active_search_provider, get_active_extract_provider
        for provider in (get_active_search_provider(), get_active_extract_provider()):
            if provider is not None and getattr(provider, "name", None) in _WEB_CHECK_SKIP:
                # The registry's single-eligible / legacy walk picked a built-in that _get_backend
                # never autodetects (the explicit-config case was handled above): the dispatcher
                # would route to the keyless tier instead, so gate on exactly that.
                if _keyless_backend() is not None:
                    return True
                continue
            if _provider_is_ready(provider):
                return True
        return False
    except Exception as exc:
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
