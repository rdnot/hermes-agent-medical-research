"""SearXNG search — plugin form.

Subclasses :class:`agent.web_search_provider.WebSearchProvider`. Same JSON
API call (``/search?format=json``), same result normalization. The legacy
in-tree module ``tools.web_providers.searxng`` was removed in the same
commit that moved this code under ``plugins/``; this file is now the
canonical implementation.

Search-only — SearXNG aggregates results from upstream engines but does not
fetch/extract arbitrary URLs. ``supports_extract()`` returns False.

Config keys this provider responds to::

    web:
      search_backend: "searxng"     # explicit per-capability
      backend: "searxng"            # shared fallback

Env var::

    SEARXNG_URL=http://localhost:8080
    SEARXNG_URL=http://user:password@searxng.example.com:8080
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional, Tuple
from urllib.parse import unquote, urlunparse, urlparse

from agent.web_search_provider import WebSearchProvider

logger = logging.getLogger(__name__)


def _searxng_url() -> str:
    """Return SEARXNG_URL from Hermes config-aware env, falling back to process env."""
    try:
        from hermes_cli.config import get_env_value

        val = get_env_value("SEARXNG_URL")
    except Exception:
        val = None
    if val is None:
        val = os.getenv("SEARXNG_URL", "")
    return (val or "").strip()


def _parse_searxng_url(raw_url: str) -> Tuple[str, Optional[Tuple[str, str]]]:
    """Split a SearXNG URL into a clean URL and an optional Basic Auth tuple.

    Supports URLs that embed credentials such as
    ``http://user:password@searxng.example.com:8080/``. The credentials are returned
    as ``(username, password)`` for ``httpx``'s ``auth`` parameter, and the
    request URL is stripped of them so that they do not leak into logs or the
    ``params`` dict.
    """
    parsed = urlparse(raw_url)
    auth: Optional[Tuple[str, str]] = None
    if parsed.username is not None:
        # percent-decode so encoded characters (e.g. pa%40ss -> pa@ss) are
        # sent to httpx as the literal credentials, not the raw encoding.
        auth = (unquote(parsed.username), unquote(parsed.password or ""))
        # Rebuild the URL without the netloc credentials.
        netloc = parsed.hostname or ""
        if parsed.port is not None:
            netloc = f"{netloc}:{parsed.port}"
        parsed = parsed._replace(netloc=netloc)
    return urlunparse(parsed), auth


class SearXNGWebSearchProvider(WebSearchProvider):
    """Search via a user-hosted SearXNG instance."""

    @property
    def name(self) -> str:
        return "searxng"

    @property
    def display_name(self) -> str:
        return "SearXNG"

    def is_available(self) -> bool:
        """Return True when ``SEARXNG_URL`` is set."""
        return bool(_searxng_url())

    def supports_search(self) -> bool:
        return True

    def supports_extract(self) -> bool:
        return False

    def search(self, query: str, limit: int = 5) -> Dict[str, Any]:
        """Execute a search against the configured SearXNG instance."""
        import httpx

        raw_url = _searxng_url()
        if not raw_url:
            return {"success": False, "error": "SEARXNG_URL is not set"}

        base_url, auth = _parse_searxng_url(raw_url)
        base_url = base_url.rstrip("/")

        params: Dict[str, Any] = {
            "q": query,
            "format": "json",
            "pageno": 1,
        }

        try:
            resp = httpx.get(
                f"{base_url}/search",
                params=params,
                auth=auth,
                timeout=15,
                headers={"Accept": "application/json"},
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.warning("SearXNG HTTP error: %s", exc)
            return {
                "success": False,
                "error": f"SearXNG returned HTTP {exc.response.status_code}",
            }
        except httpx.RequestError as exc:
            logger.warning("SearXNG request error: %s", exc)
            return {
                "success": False,
                "error": f"Could not reach SearXNG at {base_url}: {exc}",
            }

        try:
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning("SearXNG response parse error: %s", exc)
            return {
                "success": False,
                "error": "Could not parse SearXNG response as JSON",
            }

        raw_results = data.get("results", [])

        # SearXNG may return a score field; sort descending and cap to limit.
        sorted_results = sorted(
            raw_results,
            key=lambda r: float(r.get("score", 0)),
            reverse=True,
        )[:limit]

        web_results = [
            {
                "title": str(r.get("title", "")),
                "url": str(r.get("url", "")),
                "description": str(r.get("content", "")),
                "position": i + 1,
            }
            for i, r in enumerate(sorted_results)
        ]

        logger.info(
            "SearXNG search '%s': %d results (from %d raw, limit %d)",
            query,
            len(web_results),
            len(raw_results),
            limit,
        )

        return {"success": True, "data": {"web": web_results}}

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "SearXNG",
            "badge": "free · self-hosted",
            "tag": "Free, privacy-respecting metasearch. Point SEARXNG_URL at your instance.",
            "env_vars": [
                {
                    "key": "SEARXNG_URL",
                    "prompt": "SearXNG instance URL (e.g. http://localhost:8080, or http://user:password@host:port for authenticated instances)",
                    "url": "https://searx.space/",
                },
            ],
        }
