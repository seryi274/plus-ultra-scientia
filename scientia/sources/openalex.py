"""OpenAlex: the cross-disciplinary index and primary discovery source.

Pricing (2026): searches cost $0.001 each, list/filter calls $0.0001, single-record
lookups are free, and cached-PDF downloads from the content API cost $0.01 each.
A free API key gives $1/day; without one, your IP shares $0.10/day with everyone on it.
"""

from __future__ import annotations

from typing import Any

from scientia.models import Paper, arxiv_id_from_url
from scientia.net import HttpClient

API = "https://api.openalex.org/works"
CONTENT_API = "https://content.openalex.org/works"
_FIELDS = ",".join([
    "id", "doi", "display_name", "ids", "publication_year", "publication_date",
    "authorships", "primary_location", "locations", "best_oa_location",
    "open_access", "cited_by_count", "abstract_inverted_index", "has_content",
    "type", "is_retracted", "referenced_works",
])
_BATCH = 100  # OpenAlex accepts up to 100 OR-ed values in one filter


class OpenAlexError(RuntimeError):
    pass


class OpenAlex:
    name = "openalex"

    def __init__(self, http: HttpClient, api_key: str | None = None):
        self._http = http
        self._api_key = api_key
        self.cost_usd = 0.0  # running total, as reported by the API in meta.cost_usd

    def search(
        self,
        query: str,
        limit: int = 50,
        since: str | None = None,
        until: str | None = None,
        sort: str | None = None,
    ) -> list[Paper]:
        """Search titles, abstracts and keywords. `sort` is an OpenAlex sort key such
        as 'cited_by_count:desc'; the default is relevance."""
        filters = []
        if since:
            filters.append(f"from_publication_date:{since}")
        if until:
            filters.append(f"to_publication_date:{until}")

        papers: list[Paper] = []
        page = 1
        while len(papers) < limit:
            params: dict[str, Any] = {
                "search.title_abstract_keywords": query,
                "per_page": min(100, limit),  # one 100-result call costs the same as one 10-result call
                "page": page,
                "select": _FIELDS,
            }
            if filters:
                params["filter"] = ",".join(filters)
            if sort:
                params["sort"] = sort
            if self._api_key:
                params["api_key"] = self._api_key

            response = self._http.get(API, params=params)
            if response.status_code != 200:
                raise OpenAlexError(f"OpenAlex {response.status_code}: {response.text[:300]}")
            data = response.json()
            self._charge(data)
            results = data.get("results", [])
            papers.extend(parse_work(w) for w in results if w.get("display_name"))
            if len(results) < params["per_page"]:
                break
            page += 1
        return papers[:limit]

    def get_many(self, openalex_ids: list[str]) -> list[Paper]:
        """Full records for many works, 100 per call ($0.0001 each)."""
        papers = []
        for i in range(0, len(openalex_ids), _BATCH):
            papers += self._list(f"openalex:{'|'.join(openalex_ids[i:i + _BATCH])}")
        return papers

    def resolve(self, dois: list[str] = (), pmids: list[str] = ()) -> list[Paper]:
        """OpenAlex records for papers known only by DOI or PMID, batched."""
        papers = []
        for column, values in (("doi", list(dois)), ("pmid", list(pmids))):
            for i in range(0, len(values), 50):  # DOIs are long; keep URLs modest
                papers += self._list(f"{column}:{'|'.join(values[i:i + 50])}")
        return papers

    def citing(self, openalex_id: str, limit: int = 100, since: str | None = None) -> list[Paper]:
        """Works citing this one, most cited first. One call per 100 results: the
        `cites` filter does not accept OR-ed ids."""
        flt = f"cites:{openalex_id}" + (f",from_publication_date:{since}" if since else "")
        papers: list[Paper] = []
        page = 1
        while len(papers) < limit:
            batch = self._list(flt, sort="cited_by_count:desc", page=page)
            papers += batch
            if len(batch) < _BATCH:
                break
            page += 1
        return papers[:limit]

    def _list(self, flt: str, **extra: Any) -> list[Paper]:
        params: dict[str, Any] = {"filter": flt, "per_page": _BATCH, "select": _FIELDS, **extra}
        if self._api_key:
            params["api_key"] = self._api_key
        response = self._http.get(API, params=params)
        if response.status_code != 200:
            raise OpenAlexError(f"OpenAlex {response.status_code}: {response.text[:300]}")
        data = response.json()
        self._charge(data)
        return [parse_work(w) for w in data.get("results", []) if w.get("display_name")]

    def _charge(self, data: dict[str, Any]) -> None:
        self.cost_usd += float((data.get("meta") or {}).get("cost_usd") or 0)

    def get_by_doi(self, doi: str) -> Paper | None:
        """Single-record lookup (free of charge)."""
        params: dict[str, Any] = {"select": _FIELDS}
        if self._api_key:
            params["api_key"] = self._api_key
        response = self._http.get(f"{API}/doi:{doi}", params=params)
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise OpenAlexError(f"OpenAlex {response.status_code}: {response.text[:300]}")
        return parse_work(response.json())

    def content_url(self, openalex_id: str) -> str | None:
        """Metered ($0.01) download URL of OpenAlex's cached PDF. Needs an API key."""
        if not self._api_key:
            return None
        return f"{CONTENT_API}/{openalex_id}.pdf?api_key={self._api_key}"


def parse_work(work: dict[str, Any]) -> Paper:
    ids = work.get("ids") or {}
    locations = work.get("locations") or []
    best = work.get("best_oa_location") or {}
    primary = work.get("primary_location") or {}

    pdf_urls = []
    arxiv_id = None
    for loc in [best, *locations]:
        if loc.get("is_oa") and loc.get("pdf_url") and loc["pdf_url"] not in pdf_urls:
            pdf_urls.append(loc["pdf_url"])
        arxiv_id = arxiv_id or arxiv_id_from_url(loc.get("landing_page_url")) or arxiv_id_from_url(loc.get("pdf_url"))

    return Paper(
        title=work["display_name"],
        source="openalex",
        abstract=_rebuild_abstract(work.get("abstract_inverted_index")),
        authors=[
            a["author"]["display_name"]
            for a in work.get("authorships") or []
            if a.get("author", {}).get("display_name")
        ],
        year=work.get("publication_year"),
        published=work.get("publication_date"),
        venue=_venue(primary, locations),
        doi=work.get("doi"),
        arxiv_id=arxiv_id,
        pmid=_last_path_segment(ids.get("pmid")),
        pmcid=_last_path_segment(ids.get("pmcid")),
        openalex_id=_last_path_segment(work.get("id")),
        cited_by=work.get("cited_by_count"),
        is_oa=(work.get("open_access") or {}).get("is_oa"),
        license=best.get("license"),
        pdf_urls=pdf_urls,
        openalex_has_pdf=bool((work.get("has_content") or {}).get("pdf")),
        landing_url=work.get("doi") or primary.get("landing_page_url"),
        work_type=work.get("type"),
        is_retracted=work.get("is_retracted"),
        references=(
            [r for r in map(_last_path_segment, work["referenced_works"]) if r]
            if "referenced_works" in work else None
        ),
    )


def _venue(primary: dict[str, Any], locations: list[dict[str, Any]]) -> str | None:
    """Prefer the journal or conference over a repository that merely hosts a copy."""
    sources = [loc.get("source") or {} for loc in [primary, *locations]]
    for source in sources:
        if source.get("type") in ("journal", "conference", "book series") and source.get("display_name"):
            return source["display_name"]
    return next((s["display_name"] for s in sources if s.get("display_name")), None)


def _rebuild_abstract(inverted: dict[str, list[int]] | None) -> str | None:
    """OpenAlex ships abstracts as {word: [positions]}; put the words back in order."""
    if not inverted:
        return None
    positions = {pos: word for word, places in inverted.items() for pos in places}
    return " ".join(positions[i] for i in sorted(positions))


def _last_path_segment(url: str | None) -> str | None:
    if not url:
        return None
    return url.rstrip("/").rsplit("/", 1)[-1] or None
