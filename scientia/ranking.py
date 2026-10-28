"""Stage-A ranking: a cheap metadata score in [0, 1], computed for every candidate.

It mixes how well the title/abstract match the query, citation velocity and recency.
It is deliberately simple and transparent; embedding similarity and LLM judgement are
meant to be layered on top of it, for the shortlist only.
"""

from __future__ import annotations

import math
import re
from datetime import date

_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "by", "for", "from", "in", "into", "is", "of",
    "on", "or", "not", "the", "to", "with", "via", "using", "based", "its", "their",
}
_WEIGHTS = {"relevance": 0.6, "citations": 0.25, "recency": 0.15}


def query_terms(query: str) -> list[str]:
    query = re.sub(r"\b[a-z_]+:", " ", query, flags=re.IGNORECASE)  # drop field prefixes
    words = re.findall(r"[a-z0-9][a-z0-9\-]+", query.lower())
    return [_stem(w) for w in words if w not in _STOPWORDS]


def score(terms: list[str], title: str, abstract: str | None, year: int | None,
          cited_by: int | None, today: date | None = None) -> float:
    today = today or date.today()
    relevance = 0.6 * _coverage(terms, title) + 0.4 * _coverage(terms, abstract or title)

    age = max(0, today.year - year) if year else 10
    per_year = (cited_by or 0) / (age + 1)
    citations = min(1.0, math.log1p(per_year) / math.log1p(100))  # 100 cites/year saturates

    recency = math.exp(-age / 5)

    return round(
        _WEIGHTS["relevance"] * relevance
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
