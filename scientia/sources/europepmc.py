"""Europe PMC: PubMed/MEDLINE, PMC full text and life-science preprints (bioRxiv, medRxiv...).

For articles in the open-access subset, Europe PMC serves the full text as JATS XML
through its REST API, which is the approved route for automated retrieval of PMC content.
"""

from __future__ import annotations

import re
from typing import Any

from scientia.models import Paper
from scientia.net import HttpClient

API = "https://www.ebi.ac.uk/europepmc/webservices/rest"
_PAGE = 100
_TAG = re.compile(r"<[^>]+>")


class EuropePMCError(RuntimeError):
    pass


class EuropePMC:
    name = "europepmc"

    def __init__(self, http: HttpClient):
        self._http = http

    def search(
        self,
        query: str,
        limit: int = 50,
        since: str | None = None,
        until: str | None = None,
        sort: str | None = None,
    ) -> list[Paper]:
        """`query` accepts Europe PMC search syntax. `sort` may be 'recent' or 'cited';
        the default is relevance."""
        q = query
        if since or until:
            q = f"({q}) AND FIRST_PDATE:[{since or '1800-01-01'} TO {until or '2999-12-31'}]"
        params: dict[str, Any] = {
            "query": q,
            "format": "json",
            "resultType": "core",
            "pageSize": min(_PAGE, limit),
            "cursorMark": "*",
        }
        if sort == "recent":
            params["sort"] = "P_PDATE_D desc"
        elif sort == "cited":
            params["sort"] = "CITED desc"

        papers: list[Paper] = []
        while len(papers) < limit:
            response = self._http.get(f"{API}/search", params=params)
            if response.status_code != 200:
                raise EuropePMCError(f"Europe PMC {response.status_code}: {response.text[:300]}")
            data = response.json()
            results = (data.get("resultList") or {}).get("result", [])
            papers.extend(parse_result(r) for r in results if r.get("title"))
            next_cursor = data.get("nextCursorMark")
            if not results or not next_cursor or next_cursor == params["cursorMark"]:
                break
            params["cursorMark"] = next_cursor
        return papers[:limit]

    @staticmethod
    def fulltext_xml_url(pmcid: str) -> str:
        return f"{API}/{pmcid}/fullTextXML"


def parse_result(r: dict[str, Any]) -> Paper:
    authors = [
        a["fullName"]
        for a in (r.get("authorList") or {}).get("author", [])
        if a.get("fullName")
    ]
    if not authors and r.get("authorString"):
        authors = [a.strip() for a in r["authorString"].rstrip(".").split(",") if a.strip()]

    pdf_urls = []
    for link in (r.get("fullTextUrlList") or {}).get("fullTextUrl", []):
        if link.get("documentStyle") == "pdf" and link.get("availabilityCode") in ("OA", "F"):
            pdf_urls.append(link["url"])

    journal = ((r.get("journalInfo") or {}).get("journal") or {}).get("title")
    if r.get("source") == "PPR":
        journal = (r.get("bookOrReportDetails") or {}).get("publisher") or "Preprint"

    abstract = r.get("abstractText")
    year = r.get("pubYear")
    return Paper(
        title=_TAG.sub("", r["title"]),
        source="europepmc",
        abstract=" ".join(_TAG.sub(" ", abstract).split()) if abstract else None,
        authors=authors,
        year=int(year) if year and str(year).isdigit() else None,
        published=r.get("firstPublicationDate"),
        venue=journal,
        doi=r.get("doi"),
        pmid=r.get("pmid"),
        pmcid=r.get("pmcid"),
        cited_by=r.get("citedByCount"),
        is_oa=r.get("isOpenAccess") == "Y",
        license=r.get("license"),
        pdf_urls=pdf_urls,
        landing_url=f"https://europepmc.org/article/{r.get('source')}/{r.get('id')}",
    )
