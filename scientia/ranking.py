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
# An LLM triage rating, when there is one, replaces the keyword match as the relevance term:
# it knows that liraglutide is a GLP-1 receptor agonist and that axSpA includes AS.
LLM_RELEVANCE = {"central": 1.0, "relevant": 0.7, "tangential": 0.25, "off_topic": 0.0}


# A parsed query is a list of AND-ed groups; a group is a list of OR-ed alternatives;
# an alternative is the list of terms that must all appear (one word, or a phrase's words).
Group = list[list[str]]
Query = list[Group]
_TOKEN = re.compile(r'"[^"]+"|[()]|[^\s()"]+')


def term_sets(queries: list[str]) -> list[Query]:
    """Parse a topic's queries for relevance scoring, one Query per alternative the topic
    is looking for: its queries, and the parts of each query OR-ed at the top level.

    '(ankylosing spondylitis OR axial spondyloarthritis) AND (secukinumab OR ixekizumab)'
    becomes two groups, and a paper on ixekizumab in axial spondyloarthritis matches both
    in full, instead of matching 3 of the query's 6 words. Terms after NOT are ignored."""
    parsed = []
    for query in queries:
        for alternative in _split(_TOKEN.findall(query), "OR", top_level=True):
            if groups := _groups(alternative):
                parsed.append(groups)
    return parsed


def _split(tokens: list[str], op: str, top_level: bool = False) -> list[list[str]]:
    parts: list[list[str]] = [[]]
    depth = 0
    for token in tokens:
        depth += {"(": 1, ")": -1}.get(token, 0)
        if token == op and (depth == 0 or not top_level):
            parts.append([])
        else:
            parts[-1].append(token)
    return [p for p in parts if p]


def _groups(tokens: list[str]) -> Query:
    groups: Query = []
    i, negate = 0, False
    while i < len(tokens):
        token = tokens[i]
        if token in ("AND", ")"):
            i += 1
            continue
        if token in ("NOT", "ANDNOT"):
            negate, i = True, i + 1
            continue
        if token == "(":
            depth, j = 1, i + 1
            while j < len(tokens) and depth:
                depth += {"(": 1, ")": -1}.get(tokens[j], 0)
                j += 1
            inner = [t for t in tokens[i + 1:j] if t not in ("(", ")", "AND")]  # nesting flattened
            group = [terms for alt in _split(inner, "OR") if (terms := query_terms(" ".join(alt)))]
            i = j
        else:
            group = [terms] if (terms := query_terms(token)) else []
            i += 1
        if group and not negate:
            groups.append(group)
        negate = False
    return groups


def query_terms(query: str) -> list[str]:
    query = re.sub(r"\b[a-z_]+:", " ", query, flags=re.IGNORECASE)  # drop field prefixes
    words = re.findall(r"[a-z0-9][a-z0-9\-]+", query.lower())
    return [_stem(w) for w in words if w not in _STOPWORDS]


def weighted_links(cited_in: int, cites_out: int) -> float:
    return cited_in + _CITES_OUT_WEIGHT * cites_out


def score(queries: list[Query], title: str, abstract: str | None, year: int | None,
          cited_by: int | None, today: date | None = None, *,
          cited_in: int = 0, cites_out: int = 0, max_links: float = 20.0,
          llm_relevance: str | None = None) -> float:
    """`queries`: from term_sets(); relevance is the best match among them.
    `cited_in`: topic papers citing this one. `cites_out`: topic papers it cites.
    `max_links`: the topic's highest weighted_links, so the link term is relative to the
    topic's best-connected paper (in-degrees of 10 and 80 are both "core" in different topics)."""
    today = today or date.today()
    if llm_relevance in LLM_RELEVANCE:
        relevance = LLM_RELEVANCE[llm_relevance]
    else:
        relevance = max(
            (0.6 * _coverage(q, title) + 0.4 * _coverage(q, abstract or title) for q in queries),
            default=0.0,
        )

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


def _coverage(query: Query, text: str) -> float:
    """Share of the query's AND-ed groups that the text satisfies."""
    if not query:
        return 0.0
    words = {_stem(w) for w in re.findall(r"[a-z0-9][a-z0-9\-]+", text.lower())}
    return sum(any(all(t in words for t in alt) for alt in group) for group in query) / len(query)


def _stem(word: str) -> str:
    """Just enough to match plurals: 'operators' ~ 'operator', 'studies' ~ 'study'."""
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word
