import asyncio

import fundamentals.screener_client as screener_client_module
from fundamentals.screener_client import ScreenerClient, _extract_page_identity


class FakeAsyncClient:
    """httpx.AsyncClient double that answers from a scripted handler."""

    def __init__(self, handler, **kwargs):
        self._handler = handler
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, headers=None):
        return self._handler(url, headers or {})


def test_fetch_returns_304_result_without_html(monkeypatch):
    captured = {}

    def handler(url, headers):
        captured["url"] = url
        captured["headers"] = headers
        import httpx

        return httpx.Response(304, headers={"etag": '"v2"'}, request=httpx.Request("GET", url))

    monkeypatch.setattr(screener_client_module.httpx, "AsyncClient", lambda **kwargs: FakeAsyncClient(handler, **kwargs))

    result = asyncio.run(ScreenerClient().fetch_company_page("RELIANCE", if_none_match='"v1"'))

    assert result.not_modified is True
    assert result.html == ""
    assert result.etag == '"v2"'
    # Conditional request goes to the consolidated URL first and carries the stored ETag.
    assert captured["url"] == "https://www.screener.in/company/RELIANCE/consolidated/"
    assert captured["headers"]["If-None-Match"] == '"v1"'


def test_fetch_sends_if_modified_since_and_returns_html(monkeypatch):
    captured = {}

    def handler(url, headers):
        captured["url"] = url
        captured["headers"] = headers
        import httpx

        return httpx.Response(
            200,
            text="<html><h1>Reliance Industries</h1></html>",
            headers={"etag": '"v9"', "last-modified": "Wed, 02 Sep 2026 10:00:00 GMT"},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(screener_client_module.httpx, "AsyncClient", lambda **kwargs: FakeAsyncClient(handler, **kwargs))

    result = asyncio.run(ScreenerClient().fetch_company_page("RELIANCE", if_modified_since="Tue, 01 Sep 2026 10:00:00 GMT"))

    assert result.not_modified is False
    assert "Reliance Industries" in result.html
    assert result.etag == '"v9"'
    assert result.last_modified == "Wed, 02 Sep 2026 10:00:00 GMT"
    assert result.company_slug == "RELIANCE"
    assert result.statement_scope == "consolidated"
    assert captured["headers"]["If-Modified-Since"] == "Tue, 01 Sep 2026 10:00:00 GMT"


def test_fetch_falls_back_to_standalone_url_when_consolidated_is_missing(monkeypatch):
    seen_urls = []

    def handler(url, headers):
        import httpx

        seen_urls.append(url)
        if url.endswith("/consolidated/"):
            return httpx.Response(404, request=httpx.Request("GET", url))
        return httpx.Response(
            200,
            text="<html>standalone page</html>",
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(screener_client_module.httpx, "AsyncClient", lambda **kwargs: FakeAsyncClient(handler, **kwargs))

    result = asyncio.run(ScreenerClient().fetch_company_page("RELIANCE"))

    assert seen_urls == [
        "https://www.screener.in/company/RELIANCE/consolidated/",
        "https://www.screener.in/company/RELIANCE/",
    ]
    assert result.not_modified is False
    assert result.statement_scope == "standalone"


def test_fetch_raises_after_all_candidates_fail(monkeypatch):
    def handler(url, headers):
        import httpx

        return httpx.Response(500, request=httpx.Request("GET", url))

    monkeypatch.setattr(screener_client_module.httpx, "AsyncClient", lambda **kwargs: FakeAsyncClient(handler, **kwargs))

    try:
        asyncio.run(ScreenerClient().fetch_company_page("BAD"))
    except RuntimeError as exc:
        assert "Unable to fetch Screener page for BAD" in str(exc)
    else:
        raise AssertionError("expected RuntimeError after failed candidates")


def test_extract_page_identity():
    assert _extract_page_identity("https://www.screener.in/company/RELIANCE/consolidated/", "RELIANCE") == ("RELIANCE", "consolidated")
    assert _extract_page_identity("https://www.screener.in/company/RELIANCE/", "RELIANCE") == ("RELIANCE", "standalone")
    assert _extract_page_identity("https://www.screener.in/company/INFY-BE/", "INFY") == ("INFY-BE", "standalone")
