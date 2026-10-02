"""NCBI (PubMed/PMC) handling in the local fetcher (fork).

Contracts: the cookie proof-of-work shell counts as a challenge; the Scrapling tier keeps the DOM
rendered after the challenge clears, not the pre-challenge response body; and no tier's shell or
error page is ever returned as an article.
"""
from __future__ import annotations

import pytest

from tools import web_tools as wt

PMC = "https://pmc.ncbi.nlm.nih.gov/articles/PMC7241411/"
COOKIE_SHELL = b"<html><body><p>Cookies must be enabled to view this page.</p><script>challengeId=1</script></body></html>"
ARTICLE = b'<html><body><main id="main-content"><section class="abstract">' + b"Pneumonia overview. " * 300 + b"</section></main></body></html>"


@pytest.mark.parametrize(("html", "challenge", "article"), [
    (COOKIE_SHELL, True, False),
    (b"<p>Checking your browser</p><script src=recaptcha/enterprise.js></script>", True, False),
    (ARTICLE, False, True),
    # an article that merely mentions cookies is not a challenge
    (b"<div class='pmc-layout'>" + b"cookies must be enabled " + b"x" * 25_000 + b"</div>", False, True),
    # unknown markup but substantial visible text still counts as an article
    (b"<div>" + b"Real article text. " * 200 + b"</div>", False, True),
    (b"<html><head><title>Pneumonia - PMC</title></head><body></body></html>", False, False),
])
def test_ncbi_detectors(html, challenge, article):
    assert wt._is_recaptcha_challenge(html) is challenge
    assert wt._has_pubmed_article_content(html) is article


class _FakePage:
    def __init__(self, frames):
        self.frames, self.reloads = list(frames), 0

    async def content(self):
        frame = self.frames.pop(0) if len(self.frames) > 1 else self.frames[0]
        if isinstance(frame, Exception):
            raise frame
        return frame

    async def wait_for_timeout(self, ms):
        return None

    async def reload(self, **kwargs):
        self.reloads += 1


@pytest.mark.asyncio
async def test_wait_survives_the_challenge_reload_and_reloads_once_when_stuck():
    page = _FakePage([COOKIE_SHELL.decode(), RuntimeError("navigating"), ARTICLE.decode()])
    assert await wt._wait_for_ncbi_content(page, wt._has_pubmed_article_content, label="PubMed", poll_ms=10, max_wait_ms=200) is True

    stuck = _FakePage([COOKIE_SHELL.decode()])
    assert await wt._wait_for_ncbi_content(stuck, wt._has_pubmed_article_content, label="PubMed",
                                           poll_ms=10, max_wait_ms=100, reload_after_ms=50) is False
    assert stuck.reloads == 1


@pytest.mark.asyncio
async def test_scrapling_tier_returns_the_rendered_dom_not_the_pre_challenge_body(monkeypatch):
    calls = []

    class _Resp:
        status, headers, url, body = 200, {"content-type": "text/html"}, PMC, COOKIE_SHELL

    class _Session:
        def __init__(self, **kwargs):
            pass

        async def start(self):
            pass

        async def close(self):
            pass

        async def fetch(self, **kwargs):
            calls.append(kwargs)
            await kwargs["page_action"](_FakePage([COOKIE_SHELL.decode(), ARTICLE.decode()]))
            return _Resp()

    monkeypatch.setattr(wt, "HAS_CURL_CPERF", False)
    monkeypatch.setattr(wt, "HAS_SCRAPLING", True)
    monkeypatch.setattr(wt, "AsyncStealthySession", _Session, raising=False)
    content, _headers, status, fetcher, final_url = await wt._fetch_raw(PMC, timeout=10)
    assert (fetcher, status, final_url) == ("scrapling", 200, PMC)
    assert content == ARTICLE
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "body", "ctype"), [
    (203, COOKIE_SHELL, "text/html"),      # challenge shell from the httpx last resort
    (404, ARTICLE, "text/html"),           # error page from httpx is never an article
])
async def test_shells_and_error_pages_fall_back_instead_of_returning_content(monkeypatch, status, body, ctype):
    async def _raw(url, timeout=60):
        return body, {"content-type": ctype}, status, "httpx", url

    async def _no_jina(url, timeout=30):
        return None

    monkeypatch.setattr(wt, "_fetch_raw", _raw)
    monkeypatch.setattr(wt, "_fetch_jina", _no_jina)
    assert await wt._fetch_and_process_locally(PMC, timeout=5) is None


@pytest.mark.asyncio
async def test_ncbi_pdf_is_not_judged_by_html_markers(monkeypatch):
    async def _raw(url, timeout=60):
        return b"%PDF-1.7 ...", {"content-type": "application/pdf"}, 200, "httpx", url

    async def _jina_must_not_run(url, timeout=30):
        raise AssertionError("PDF must not be routed to Jina")

    monkeypatch.setattr(wt, "_fetch_raw", _raw)
    monkeypatch.setattr(wt, "_fetch_jina", _jina_must_not_run)
    monkeypatch.setattr(wt, "_extract_pdf_text", lambda data: "--- Page 1 ---\nfull text")
    entry = await wt._fetch_and_process_locally(PMC + "pdf/x.pdf", timeout=5)
    assert entry["content"] == "--- Page 1 ---\nfull text"
