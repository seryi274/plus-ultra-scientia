"""Citation expansion ("snowballing") through OpenAlex.

Keyword search misses foundational papers that use older terminology and recent ones that
use new terminology. Following citations from the topic's best papers ("seeds") finds both:

  backward  works cited by at least `min_links` seeds        -> foundational candidates
  forward   works citing at least `min_links` topic papers   -> frontier candidates

Thresholds matter: one seed citing a paper says little; several citing it says a lot.
Before expanding, every topic paper is matched to its OpenAlex record(s) and its reference
list is refreshed, so the topic-link counts used for ranking cover the whole topic.

Cost: list calls are $0.0001 each. A refresh is one call per 100 topic papers, backward
expansion one call per 100 new papers, forward expansion one call per OpenAlex version of
each seed. A 25-seed run on a 200-paper topic costs about half a cent.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from scientia.sources.openalex import OpenAlex
from scientia.store import Store

_MAX_VERSIONS_PER_SEED = 3  # OpenAlex versions of one paper queried for citing works


@dataclass
class ExpandResult:
    resolved: int = 0       # topic papers newly matched to OpenAlex
    unresolved: int = 0     # topic papers OpenAlex has no record for (no usable id)
    refreshed: int = 0      # OpenAlex records whose references were reloaded
    seeds: int = 0
    backward_candidates: int = 0
    forward_candidates: int = 0
    added: dict[str, list[int]] = field(default_factory=lambda: {"backward": [], "forward": []})


def expand(store: Store, openalex: OpenAlex, topic_id: int, *, seeds: int = 25,
           direction: str = "both", min_links: int = 2, max_new: int = 50,
           since: str | None = None, skip_types: list[str] = ()) -> ExpandResult:
    """Each run is one snowball step outwards from the current best papers; running it
    again on the grown topic reaches further. `max_new` bounds the growth per step."""
    result = ExpandResult()
    skip = set(skip_types)
    _resolve(store, openalex, topic_id, result)
    _refresh_references(store, openalex, topic_id, result)

    seed_versions = _seed_versions(store, topic_id, seeds)
    result.seeds = len(seed_versions)
    if direction in ("both", "backward"):
        _backward(store, openalex, topic_id, seed_versions, min_links, max_new, skip, result)
    if direction in ("both", "forward"):
        _forward(store, openalex, topic_id, seed_versions, min_links, max_new, since, skip, result)
    return result


def _resolve(store: Store, openalex: OpenAlex, topic_id: int, result: ExpandResult) -> None:
    """Find OpenAlex records for topic papers that came from arXiv or Europe PMC only."""
    rows = store.conn.execute(
        """SELECT p.id, p.doi, p.arxiv_id, p.pmid FROM topic_papers tp JOIN papers p ON p.id = tp.paper_id
           WHERE tp.topic_id = ? AND NOT EXISTS (SELECT 1 FROM openalex_aliases a WHERE a.paper_id = p.id)""",
        (topic_id,),
    ).fetchall()
    if not rows:
        return
    dois = [r["doi"] or f"10.48550/arxiv.{r['arxiv_id']}" for r in rows if r["doi"] or r["arxiv_id"]]
    pmids = [r["pmid"] for r in rows if not (r["doi"] or r["arxiv_id"]) and r["pmid"]]
    for paper in openalex.resolve(dois=dois, pmids=pmids):
        store.add_paper(paper)
    still_missing = store.conn.execute(
        """SELECT COUNT(*) FROM topic_papers tp WHERE tp.topic_id = ? AND NOT EXISTS
           (SELECT 1 FROM openalex_aliases a WHERE a.paper_id = tp.paper_id)""",
        (topic_id,),
    ).fetchone()[0]
    result.unresolved = still_missing
    result.resolved = len(rows) - still_missing


def _refresh_references(store: Store, openalex: OpenAlex, topic_id: int, result: ExpandResult) -> None:
    ids = sorted(store.aliases(topic_id=topic_id))
    for paper in openalex.get_many(ids):
        store.add_paper(paper)
        result.refreshed += 1


def _seed_versions(store: Store, topic_id: int, seeds: int) -> dict[int, list[str]]:
    """Top-scoring topic papers (not skipped) -> their OpenAlex ids."""
    versions: dict[int, list[str]] = defaultdict(list)
    for openalex_id, paper_id in store.aliases(topic_id=topic_id).items():
        versions[paper_id].append(openalex_id)
    ranked = [p.id for p in store.topic_papers(topic_id) if p.status != "skipped" and p.id in versions]
    return {pid: sorted(versions[pid]) for pid in ranked[:seeds]}


def _backward(store: Store, openalex: OpenAlex, topic_id: int, seed_versions: dict[int, list[str]],
              min_links: int, max_new: int, skip: set[str], result: ExpandResult) -> None:
    in_topic = set(store.aliases(topic_id=topic_id).values())
    refs = store.references_of([v for vs in seed_versions.values() for v in vs])

    # Count distinct seeds citing each work. Works already in the library are counted per
    # paper, so two OpenAlex versions of one paper are not counted twice.
    all_cited = sorted({c for cited in refs.values() for c in cited})
    known = store.aliases(openalex_ids=all_cited)
    citers: dict[str | int, set[int]] = defaultdict(set)
    for seed, versions in seed_versions.items():
        for version in versions:
            for cited in refs.get(version, []):
                citers[known.get(cited, cited)].add(seed)

    candidates = sorted(
        ((key, len(s)) for key, s in citers.items() if len(s) >= min_links and key not in in_topic),
        key=lambda kv: -kv[1],
    )
    result.backward_candidates = len(candidates)

    # Papers already in the library just join the topic; the rest are fetched in batches.
    # Work types are only known after fetching, so fetch a little beyond the cap.
    chosen = candidates[: max_new * 2 if skip else max_new]
    to_fetch = [key for key, _ in chosen if isinstance(key, str)]
    fetched = {p.openalex_id: p for p in openalex.get_many(to_fetch)}
    for key, n in chosen:
        if len(result.added["backward"]) >= max_new:
            break
        if isinstance(key, int):
            if store.get_paper(key).work_type in skip:
                continue
            paper_id = key
        elif key in fetched:
            if fetched[key].work_type in skip:
                continue
            paper_id, _ = store.add_paper(fetched[key])
        else:
            continue  # OpenAlex merged or withdrew the work
        if store.link(topic_id, paper_id):
            store.set_note(topic_id, paper_id, f"backward: cited by {n} of {len(seed_versions)} seeds")
            result.added["backward"].append(paper_id)


def _forward(store: Store, openalex: OpenAlex, topic_id: int, seed_versions: dict[int, list[str]],
             min_links: int, max_new: int, since: str | None, skip: set[str], result: ExpandResult) -> None:
    topic_aliases = store.aliases(topic_id=topic_id)
    citing = {}
    for versions in seed_versions.values():
        for version in versions[:_MAX_VERSIONS_PER_SEED]:
            for paper in openalex.citing(version, since=since):
                citing.setdefault(paper.openalex_id, paper)

    scored = []
    for openalex_id, paper in citing.items():
        if openalex_id in topic_aliases or paper.work_type in skip:
            continue
        links = {topic_aliases[r] for r in paper.references or [] if r in topic_aliases}
        if len(links) >= min_links:
            scored.append((len(links), paper.cited_by or 0, paper))
    scored.sort(key=lambda t: (-t[0], -t[1]))
    result.forward_candidates = len(scored)

    for n, _, paper in scored[:max_new]:
        paper_id, _ = store.add_paper(paper)
        if store.link(topic_id, paper_id):
            store.set_note(topic_id, paper_id, f"forward: cites {n} topic papers")
            result.added["forward"].append(paper_id)
