"""Stage-A ranking: a cheap metadata score in [0, 1], computed for every candidate.

It mixes how well the title/abstract match the query, how tightly the paper is linked
by citations to the rest of the topic, citation velocity and recency. It is deliberately
simple and transparent; embedding similarity and LLM judgement are meant to be layered
on top of it, for the shortlist only.

The topic-link term is what lets citation expansion work: a foundational paper that
uses older terminology scores low on the query match but is cited by many of the
topic's papers, and a new paper using different words cites many of them. Being cited
by the topic counts more than citing it: a review cites everything, but only the work
the field builds on is cited by everything.
"""

from __future__ import annotations

import math
import re
from datetime import date

_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "by", "for", "from", "in", "into", "is", "of",
    "on", "or", "not", "the", "to", "with", "via", "using", "based", "its", "their",
}
_WEIGHTS = {"relevance": 0.45, "topic_links": 0.25, "citations": 0.15, "recency": 0.15}
_CITES_OUT_WEIGHT = 0.3  # one outgoing link to the topic is worth 0.3 incoming ones
_MIN_SATURATION = 5.0  # small topics: don't let 2 links count as "maximally connected"


def query_terms(query: str) -> list[str]:
    query = re.sub(r"\b[a-z_]+:", " ", query, flags=re.IGNORECASE)  # drop field prefixes
    words = re.findall(r"[a-z0-9][a-z0-9\-]+", query.lower())
    return [_stem(w) for w in words if w not in _STOPWORDS]


def weighted_links(cited_in: int, cites_out: int) -> float:
    return cited_in + _CITES_OUT_WEIGHT * cites_out


def score(terms: list[str], title: str, abstract: str | None, year: int | None,
          cited_by: int | None, today: date | None = None, *,
          cited_in: int = 0, cites_out: int = 0, max_links: float = 20.0) -> float:
    """`cited_in`: topic papers citing this one. `cites_out`: topic papers it cites.
    `max_links`: the topic's highest weighted_links, so the link term is relative to the
    topic's best-connected paper (in-degrees of 10 and 80 are both "core" in different topics)."""
    today = today or date.today()
    relevance = 0.6 * _coverage(terms, title) + 0.4 * _coverage(terms, abstract or title)

    saturation = max(_MIN_SATURATION, max_links)
    topic_links = min(1.0, math.log1p(weighted_links(cited_in, cites_out)) / math.log1p(saturation))

    age = max(0, today.year - year) if year else 10
    per_year = (cited_by or 0) / (age + 1)
    citations = min(1.0, math.log1p(per_year) / math.log1p(100))  # 100 cites/year saturates

    recency = math.exp(-age / 5)

    return round(
        _WEIGHTS["relevance"] * relevance
        + _WEIGHTS["topic_links"] * topic_links
        + _WEIGHTS["citations"] * citations
        + _WEIGHTS["recency"] * recency,
        4,
    )


def _coverage(terms: list[str], text: str) -> float:
    if not terms:
        return 0.0
    words = {_stem(w) for w in re.findall(r"[a-z0-9][a-z0-9\-]+", text.lower())}
    return sum(t in words for t in terms) / len(terms)


def _stem(word: str) -> str:
    """Just enough to match plurals: 'operators' ~ 'operator', 'studies' ~ 'study'."""
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word
