"""Screener.in company-page fetch client with conditional-request support."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlparse

import httpx


@dataclass(frozen=True, slots=True)
class ScreenerFetchResult:
    requested_symbol: str
    company_slug: str
    statement_scope: str
    source_url: str
    fetched_at: str
    etag: str | None
    last_modified: str | None
    not_modified: bool
    html: str


class ScreenerClient:
    def __init__(self, *, base_url: str = "https://www.screener.in", timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/135.0.0.0 Safari/537.36"
            ),
        }

    async def fetch_company_page(
        self,
        symbol: str,
        *,
        statement_scope: str = "consolidated",
        if_none_match: str | None = None,
        if_modified_since: str | None = None,
    ) -> ScreenerFetchResult:
        candidate_urls = []
        normalized_scope = statement_scope.strip().lower()
        if normalized_scope == "consolidated":
            candidate_urls.append(f"{self.base_url}/company/{symbol}/consolidated/")
        candidate_urls.append(f"{self.base_url}/company/{symbol}/")

        async with httpx.AsyncClient(timeout=self.timeout, headers=self.headers, follow_redirects=True) as client:
            last_error: Exception | None = None
            for url in candidate_urls:
                try:
                    request_headers = {}
                    if if_none_match:
                        request_headers["If-None-Match"] = if_none_match
                    if if_modified_since:
                        request_headers["If-Modified-Since"] = if_modified_since
                    response = await client.get(url, headers=request_headers or None)
                    source_url = str(response.url)
                    company_slug, scope = _extract_page_identity(source_url, symbol)
                    if response.status_code == 304:
                        return ScreenerFetchResult(
                            requested_symbol=symbol,
                            company_slug=company_slug,
                            statement_scope=scope,
                            source_url=source_url,
                            fetched_at=datetime.now(UTC).isoformat(),
                            etag=response.headers.get("etag"),
                            last_modified=response.headers.get("last-modified"),
                            not_modified=True,
                            html="",
                        )
                    if response.status_code == 404:
                        continue
                    response.raise_for_status()
                except Exception as exc:
                    last_error = exc
                    continue

                html = response.text
                if "Page Not Found" in html and "Resolver404" in html:
                    continue

                return ScreenerFetchResult(
                    requested_symbol=symbol,
                    company_slug=company_slug,
                    statement_scope=scope,
                    source_url=source_url,
                    fetched_at=datetime.now(UTC).isoformat(),
                    etag=response.headers.get("etag"),
                    last_modified=response.headers.get("last-modified"),
                    not_modified=False,
                    html=html,
                )

        if last_error is not None:
            raise RuntimeError(f"Unable to fetch Screener page for {symbol}: {last_error}") from last_error
        raise RuntimeError(f"Unable to resolve Screener page for {symbol}")


def _extract_page_identity(source_url: str, fallback_symbol: str) -> tuple[str, str]:
    path_parts = [part for part in urlparse(source_url).path.split("/") if part]
    company_slug = fallback_symbol
    if len(path_parts) >= 2 and path_parts[0] == "company":
        company_slug = path_parts[1]
    scope = "consolidated" if "consolidated" in path_parts else "standalone"
    return company_slug, scope
