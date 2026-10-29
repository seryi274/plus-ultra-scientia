"""CORE (core.ac.uk): full text aggregated from thousands of institutional and subject
repositories. Often holds the accepted manuscript of a paywalled journal article.

Works without a key at a low rate limit; a free key from https://core.ac.uk/services/api
raises it. CORE's own download links (core.ac.uk/download/...) sit behind a Cloudflare bot
check that scripts cannot pass, so we download from the original repositories CORE lists,
plus CORE's API download endpoint when a key is configured.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from scientia.models import normalize_doi, title_key
from scientia.net import HttpClient

API = "https://api.core.ac.uk/v3/search/works/"
_API_HOST = "api.core.ac.uk"


class Core:
    name = "core"

    def __init__(self, http: HttpClient, api_key: str | None = None):
        self._http = http
        self._api_key = api_key
        self._headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    def headers_for(self, url: str) -> dict[str, str]:
        """Auth headers needed to download `url` (only CORE's own API needs them)."""
        return self._headers if urlsplit(url).hostname == _API_HOST else {}

    def open_pdfs(self, doi: str | None = None, title: str | None = None) -> list[str]:
        """Download URLs of repository copies of this paper. Title hits must match exactly
        (ignoring case and punctuation), since CORE's title search is fuzzy."""
        urls: list[str] = []
        if doi:
            for work in self._search(f'doi:"{doi}"'):
                if normalize_doi(work.get("doi")) == doi:
                    urls += self._download_urls(work)
        if not urls and title:
            key = title_key(title)
            for work in self._search(f'title:"{title.replace(chr(34), "")}"'):
                if title_key(work.get("title") or "") == key:
                    urls += self._download_urls(work)
        return list(dict.fromkeys(urls))

    def _search(self, query: str) -> list[dict[str, Any]]:
        response = self._http.get(API, params={"q": query, "limit": 5}, headers=self._headers, retries=1)
        if response.status_code != 200:
            raise RuntimeError(f"CORE HTTP {response.status_code}")
        return response.json().get("results", [])

    def _download_urls(self, work: dict[str, Any]) -> list[str]:
        urls = [
            u for u in work.get("sourceFulltextUrls") or []
            if not (urlsplit(u).hostname or "").endswith("core.ac.uk")
        ]
        if self._api_key:
            urls += [f"{output.rstrip('/')}/download" for output in work.get("outputs") or []]
        return urls
