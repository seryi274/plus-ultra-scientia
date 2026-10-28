"""arXiv: preprints in maths, physics, CS/AI, statistics, q-bio and q-fin.

The API terms ask for at most one request every three seconds (enforced in net.py).
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

from scientia.models import Paper, normalize_arxiv_id
from scientia.net import HttpClient

API = "https://export.arxiv.org/api/query"
_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
    "opensearch": "http://a9.com/-/spec/opensearch/1.1/",
}
_PAGE = 100


class ArxivError(RuntimeError):
    pass


class Arxiv:
    name = "arxiv"

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
        """`sort` may be 'relevance' (default) or 'recent' (newest submissions first)."""
        search_query = build_query(query, since, until)
        papers: list[Paper] = []
        start = 0
        while len(papers) < limit:
            params = {
                "search_query": search_query,
                "start": start,
                "max_results": min(_PAGE, limit - len(papers)),
                "sortBy": "submittedDate" if sort == "recent" else "relevance",
                "sortOrder": "descending",
            }
            response = self._http.get(API, params=params)
            if response.status_code != 200:
                raise ArxivError(f"arXiv {response.status_code}: {response.text[:300]}")
            batch = parse_feed(response.text)
            papers.extend(batch)
            if len(batch) < params["max_results"]:
                break
            start += len(batch)
        return papers[:limit]


def build_query(query: str, since: str | None = None, until: str | None = None) -> str:
    """Translate the query syntax shared with OpenAlex and Europe PMC (words, "quoted
    phrases", AND / OR / NOT, parentheses; adjacent terms are AND-ed) into arXiv's, where
    every term needs a field prefix and NOT is spelled ANDNOT. A query that already uses
    arXiv field syntax (e.g. 'ti:transformer AND cat:cs.LG') is passed through."""
    if re.search(r"\b(ti|au|abs|co|jr|cat|rn|id|all):", query):
        q = query
    else:
        parts: list[str] = []
        after_term = False  # an implicit AND goes between two terms
        for token in re.findall(r'"[^"]+"|[()]|[^\s()"]+', query):
            if token in ("AND", "OR", "NOT", "ANDNOT"):
                parts.append("ANDNOT" if token == "NOT" else token)
                after_term = False
            elif token == ")":
                parts.append(token)
                after_term = True
            else:
                if after_term:
                    parts.append("AND")
                parts.append(token if token == "(" else f"all:{token}")
                after_term = token != "("
        q = " ".join(parts).replace("( ", "(").replace(" )", ")")
    if since or until:
        lo = (since or "1991-01-01").replace("-", "") + "0000"
        hi = (until or "2999-12-31").replace("-", "") + "2359"
        q = f"({q}) AND submittedDate:[{lo} TO {hi}]"
    return q


def parse_feed(xml_text: str) -> list[Paper]:
    root = ET.fromstring(xml_text)
    papers = []
    for entry in root.findall("atom:entry", _NS):
        arxiv_id = normalize_arxiv_id(_text(entry, "atom:id"))
        title = _text(entry, "atom:title")
        if not arxiv_id or not title:
            continue  # the API returns a single titleless entry for query errors
        published = (_text(entry, "atom:published") or "")[:10] or None
        papers.append(Paper(
            title=title,
            source="arxiv",
            abstract=" ".join((_text(entry, "atom:summary") or "").split()) or None,
            authors=[
                name for a in entry.findall("atom:author", _NS)
                if (name := _text(a, "atom:name"))
            ],
            year=int(published[:4]) if published else None,
            published=published,
            venue=_text(entry, "arxiv:journal_ref") or "arXiv",
            doi=_text(entry, "arxiv:doi"),
            arxiv_id=arxiv_id,
            is_oa=True,
            pdf_urls=[f"https://arxiv.org/pdf/{arxiv_id}"],
            landing_url=f"https://arxiv.org/abs/{arxiv_id}",
        ))
    return papers


def _text(node: ET.Element, path: str) -> str | None:
    found = node.find(path, _NS)
    if found is None or found.text is None:
        return None
    return " ".join(found.text.split()) or None
