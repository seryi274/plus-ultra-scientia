"""Semantic Scholar: used here as a lookup for open-access PDF links (`openAccessPdf`).

Without a key, requests share a public pool and are often throttled (HTTP 429); a free
key from https://www.semanticscholar.org/product/api raises that to 1 request/second.
"""

from __future__ import annotations

from scientia.models import title_key
from scientia.net import HttpClient

API = "https://api.semanticscholar.org/graph/v1"
_FIELDS = "title,openAccessPdf"


class SemanticScholar:
    name = "semanticscholar"

    def __init__(self, http: HttpClient, api_key: str | None = None):
        self._http = http
        self._headers = {"x-api-key": api_key} if api_key else {}

    def open_pdf(self, doi: str | None = None, pmid: str | None = None,
                 title: str | None = None) -> tuple[str, str | None] | None:
        """(pdf_url, license) for the paper, or None. Identifiers are tried before the
        title, and a title match is only accepted if it is the same title."""
        for ref in (f"DOI:{doi}" if doi else None, f"PMID:{pmid}" if pmid else None):
            if not ref:
                continue
            response = self._http.get(f"{API}/paper/{ref}", params={"fields": _FIELDS},
                                      headers=self._headers, retries=1)
            if response.status_code == 200:
                return _pdf(response.json())
            if response.status_code != 404:
                raise RuntimeError(f"Semantic Scholar HTTP {response.status_code}")
        if title:
            response = self._http.get(f"{API}/paper/search/match",
                                      params={"query": title, "fields": _FIELDS},
                                      headers=self._headers, retries=1)
            if response.status_code == 200:
                for hit in response.json().get("data", []):
                    if title_key(hit.get("title") or "") == title_key(title):
                        return _pdf(hit)
            elif response.status_code != 404:
                raise RuntimeError(f"Semantic Scholar HTTP {response.status_code}")
        return None

    def open_pdfs_batch(self, refs: list[str]) -> list[tuple[str, str | None] | None] | None:
        """open_pdf() for many 'DOI:...' / 'PMID:...' refs, 500 per call, in input order.
        Returns None if a call fails, so callers can fall back to single lookups."""
        found: list[tuple[str, str | None] | None] = []
        for i in range(0, len(refs), 500):
            chunk = refs[i:i + 500]
            response = self._http.post(f"{API}/paper/batch", params={"fields": "openAccessPdf"},
                                       headers=self._headers, json={"ids": chunk})
            if response.status_code != 200:
                return None
            found += [_pdf(record) if record else None for record in response.json()]
        return found


def _pdf(record: dict) -> tuple[str, str | None] | None:
    pdf = record.get("openAccessPdf") or {}
    return (pdf["url"], pdf.get("license")) if pdf.get("url") else None
