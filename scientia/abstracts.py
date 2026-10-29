"""Fill in missing abstracts before an LLM review, from free sources.

OpenAlex lacks abstracts for some publishers. Most biomedical papers are in PubMed, whose
abstracts Europe PMC serves by PMID (its article endpoint, one call per paper); for papers
known only by DOI, Semantic Scholar's batch endpoint takes up to 500 ids per call.
Each paper is tried once: `abstract_checked` records the attempt, found or not.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from scientia.net import HttpClient
from scientia.store import Store

_EUROPEPMC_ARTICLE = "https://www.ebi.ac.uk/europepmc/webservices/rest/article/MED/{pmid}"
_S2_BATCH = "https://api.semanticscholar.org/graph/v1/paper/batch"
_TAG = re.compile(r"<[^>]+>")
MIN_ABSTRACT_CHARS = 200  # shorter "abstracts" are usually a truncated first sentence


@dataclass
class BackfillResult:
    checked: int = 0
    found_europepmc: int = 0
    found_semanticscholar: int = 0


def backfill_abstracts(store: Store, http: HttpClient, topic_id: int, s2_api_key: str | None = None) -> BackfillResult:
    result = BackfillResult()
    rows = store.conn.execute(
        f"""SELECT p.id, p.pmid, p.doi FROM topic_papers tp JOIN papers p ON p.id = tp.paper_id
            WHERE tp.topic_id = ? AND p.abstract_checked IS NULL
            AND (p.abstract IS NULL OR length(p.abstract) <= {MIN_ABSTRACT_CHARS})""",
        (topic_id,),
    ).fetchall()

    for row in (r for r in rows if r["pmid"]):
        response = http.get(_EUROPEPMC_ARTICLE.format(pmid=row["pmid"]),
                            params={"format": "json", "resultType": "core"})
        text = None
        if response.status_code == 200:
            text = _clean((response.json().get("result") or {}).get("abstractText"))
        if text:
            store.set_abstract(row["id"], text, "europepmc")
            result.found_europepmc += 1
        else:
            store.mark_abstract_checked(row["id"])
        result.checked += 1

    doi_only = [r for r in rows if not r["pmid"] and r["doi"]]
    headers = {"x-api-key": s2_api_key} if s2_api_key else {}
    for i in range(0, len(doi_only), 500):
        chunk = doi_only[i:i + 500]
        response = http.post(_S2_BATCH, params={"fields": "abstract"}, headers=headers,
                             json={"ids": [f"DOI:{r['doi']}" for r in chunk]})
        records = response.json() if response.status_code == 200 else [None] * len(chunk)
        for row, record in zip(chunk, records):  # results come back in request order, null if unknown
            text = _clean((record or {}).get("abstract"))
            if text:
                store.set_abstract(row["id"], text, "semanticscholar")
                result.found_semanticscholar += 1
            elif response.status_code == 200:  # leave unchecked on a failed call, to retry later
                store.mark_abstract_checked(row["id"])
            result.checked += 1
    return result


def _clean(text: str | None) -> str | None:
    if not text:
        return None
    text = " ".join(_TAG.sub(" ", text).split())
    return text if len(text) > MIN_ABSTRACT_CHARS else None
