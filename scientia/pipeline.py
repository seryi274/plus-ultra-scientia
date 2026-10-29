"""The operations behind the CLI commands, reusable by `scientia run` (project.py).

Each function does one step for one topic, writes to the store, and reports progress
through `log` (print by default) instead of printing directly.
"""

from __future__ import annotations

import json
import math
import os
import textwrap
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from scientia import ranking
from scientia.abstracts import backfill_abstracts
from scientia.acquire import Acquirer
from scientia.config import Config
from scientia.expand import ExpandResult, expand
from scientia.net import HttpClient
from scientia.review import Reviewer, triage_results
from scientia.sources.arxiv import Arxiv
from scientia.sources.europepmc import EuropePMC
from scientia.sources.openalex import OpenAlex
from scientia.store import Store, StoredPaper

Log = Callable[[str], None]

SOURCES = ("openalex", "arxiv", "europepmc")
# OpenAlex work types that citation expansion never adds.
SKIPPED_TYPES = ("editorial", "letter", "erratum", "paratext", "retraction", "peer-review")
# Per-source spelling of the sort choices; None means the source's relevance order.
_SORT = {
    "openalex": {"recent": "publication_date:desc", "cited": "cited_by_count:desc"},
    "arxiv": {"recent": "recent"},
    "europepmc": {"recent": "recent", "cited": "cited"},
}


# -- ranking ------------------------------------------------------------------------

def rescore(store: Store, topic_id: int) -> None:
    terms = ranking.term_sets([q["query"] for q in store.queries(topic_id)])
    links = store.topic_links(topic_id)
    triage = triage_results(store, topic_id)
    max_links = max((ranking.weighted_links(*l) for l in links.values()), default=0.0)
    for p in store.topic_papers(topic_id):
        cited_in, cites_out = links.get(p.id, (0, 0))
        store.set_score(topic_id, p.id, ranking.score(
            terms, p.title, p.abstract, p.year, p.cited_by, cited_in=cited_in, cites_out=cites_out,
            max_links=max_links, llm_relevance=triage.get(p.id, {}).get("relevance")))


def view(store: Store, topic_id: int, papers: list[StoredPaper], kind: str) -> list[StoredPaper]:
    """Reorder papers for one reading purpose instead of the blended score."""
    links = store.topic_links(topic_id)
    if kind == "foundational":
        return sorted(papers, key=lambda p: (-links.get(p.id, (0, 0))[0], -p.score))
    if kind == "frontier":
        # Papers from the last three years, most connected to the topic first.
        horizon = date.today().year - 3
        recent = [p for p in papers if (p.year or 0) >= horizon]
        return sorted(recent, key=lambda p: (-links.get(p.id, (0, 0))[1], -p.score))
    if kind == "recent":
        return sorted(papers, key=lambda p: (p.published or str(p.year or ""), p.score), reverse=True)
    return papers


# -- search -------------------------------------------------------------------------

def search_clients(http: HttpClient, config: Config) -> dict:
    return {"openalex": OpenAlex(http, config.openalex_api_key), "arxiv": Arxiv(http), "europepmc": EuropePMC(http)}


def run_search(store: Store, clients: dict, topic_id: int, label: str, query: str, sources: list[str], *,
               limit: int = 50, since: str | None = None, until: str | None = None, sort: str = "relevance",
               log: Log = print) -> int:
    """Run one query on each source; returns how many papers were new to the topic."""
    log(f"[{label}] {query}")
    new_total = 0
    for name in sources:
        params = {"limit": limit, "since": since, "until": until, "sort": _SORT[name].get(sort), "label": label}
        try:
            papers = clients[name].search(query, **{k: v for k, v in params.items() if k != "label"})
        except Exception as exc:  # one source being down should not sink the others
            store.log_search(topic_id, name, query, params, 0, error=str(exc))
            store.commit()
            log(f"  {name:<10} failed: {exc}")
            continue
        new_here = 0
        for paper in papers:
            paper_id, _ = store.add_paper(paper)
            new_here += store.link(topic_id, paper_id)
        store.log_search(topic_id, name, query, params, len(papers))
        store.commit()
        new_total += new_here
        log(f"  {name:<10} {len(papers):>4} results, {new_here:>4} new to topic")
    return new_total


def searched_before(store: Store, topic_id: int, source: str, query: str) -> bool:
    return store.conn.execute(
        "SELECT 1 FROM searches WHERE topic_id = ? AND source = ? AND query = ? AND error IS NULL",
        (topic_id, source, query),
    ).fetchone() is not None


# -- expansion ----------------------------------------------------------------------

def expand_params(seeds: int = 25, direction: str = "both", min_links: int = 2, max_new: int = 50,
                  since: str | None = None, skip_types: list[str] = ()) -> dict:
    return {"seeds": seeds, "direction": direction, "min_links": min_links, "max_new": max_new,
            "since": since, "skip_types": sorted({*SKIPPED_TYPES, *skip_types})}


def expanded_before(store: Store, topic_id: int, params: dict) -> bool:
    for row in store.conn.execute(
        "SELECT params FROM searches WHERE topic_id = ? AND source = 'openalex-expand' AND error IS NULL",
        (topic_id,),
    ):
        logged = json.loads(row["params"])
        logged["skip_types"] = sorted(set(logged.get("skip_types", [])))
        if logged == params:
            return True
    return False


def run_expand(store: Store, http: HttpClient, config: Config, topic_id: int, params: dict,
               log: Log = print) -> ExpandResult | None:
    openalex = OpenAlex(http, config.openalex_api_key)
    rescore(store, topic_id)  # so the seeds are the topic's current best papers
    try:
        result = expand(store, openalex, topic_id, **params)
    except Exception as exc:
        store.commit()  # keep whatever was resolved before the failure
        store.log_search(topic_id, "openalex-expand", "citation expansion", params, 0, error=str(exc))
        store.commit()
        log(f"error: expansion failed: {exc}")
        return None
    added = result.added["backward"] + result.added["forward"]
    store.log_search(topic_id, "openalex-expand", "citation expansion", params, len(added))
    rescore(store, topic_id)
    store.commit()
    log(f"Matched {result.resolved} more topic papers to OpenAlex"
        + (f" ({result.unresolved} have no OpenAlex record)" if result.unresolved else "")
        + f"; refreshed references of {result.refreshed} records.")
    log(f"Expanded from {result.seeds} seeds (min {params['min_links']} links):")
    if params["direction"] in ("both", "backward"):
        log(f"  backward  {result.backward_candidates:>4} candidates, {len(result.added['backward']):>3} added")
    if params["direction"] in ("both", "forward"):
        log(f"  forward   {result.forward_candidates:>4} candidates, {len(result.added['forward']):>3} added")
    log(f"OpenAlex cost: ${openalex.cost_usd:.4f}")
    return result


# -- LLM review ---------------------------------------------------------------------

def make_reviewer(store: Store, topic_id: int) -> Reviewer | None:
    settings = store.review_settings(topic_id)
    if not settings["question"] or not settings["profile"]:
        return None
    return Reviewer(store, topic_id, settings["question"], settings["profile"],
                    schema=settings["schema"], hints=settings["hints"])


def run_review(store: Store, http: HttpClient, config: Config, topic_id: int, topic_name: str, stage: str, *,
               limit: int | None = None, include_tangential: bool = False, sync: bool = False,
               wait: bool = False, dry_run: bool = False, max_cost: float | None = None,
               log: Log = print) -> float | None:
    """Collect finished batches, then submit what still needs reviewing. Returns the
    estimated cost of what was submitted (0 if nothing), or None on a setup error
    (no question or profile, no API key)."""
    reviewer = make_reviewer(store, topic_id)
    if reviewer is None:
        log(f"error: '{topic_name}' needs a review question and profile first")
        return None
    if not dry_run and not os.environ.get("ANTHROPIC_API_KEY"):
        log("error: ANTHROPIC_API_KEY is not set in this environment (restart the terminal after setx)")
        return None

    if not dry_run:
        while True:
            collected = reviewer.collect()
            if collected.finished:
                summary = ", ".join(f"{n} {s}" for s, n in sorted(collected.recorded.items()))
                log(f"Collected {collected.finished} finished batch(es): {summary}")
                rescore(store, topic_id)
                store.commit()
            for line in collected.still_running:
                log(f"Still running: {line}")
            if not (wait and collected.still_running):
                break
            time.sleep(30)
        if collected.still_running:
            log("Run again later (or with --wait) to collect the results.")
            return 0.0

    if stage == "triage" and not dry_run:
        filled = backfill_abstracts(store, http, topic_id, config.s2_api_key)
        store.commit()
        if filled.checked:
            log(f"Looked up {filled.checked} missing abstracts: {filled.found_europepmc} from Europe PMC, "
                f"{filled.found_semanticscholar} from Semantic Scholar.")

    papers = reviewer.candidates(stage, include_tangential)[:limit]
    if not papers:
        log(f"Nothing left to {stage} in '{topic_name}'.")
        return 0.0
    cost = reviewer.estimate_cost(stage, papers, batch=not sync)
    if max_cost is not None and cost > max_cost:
        keep = max(0, math.floor(len(papers) * max_cost / cost))
        log(f"Estimated ${cost:.2f} exceeds the ${max_cost:.2f} cap: reviewing the top {keep} of {len(papers)}.")
        papers = papers[:keep]
        cost = reviewer.estimate_cost(stage, papers, batch=not sync)
        if not papers:
            return 0.0
    log(f"{stage}: {len(papers)} papers with {reviewer.model(stage)}, estimated ${cost:.2f}"
        f"{'' if sync else ' (batch price)'}")
    if dry_run:
        return cost
    if sync:
        counts = reviewer.run_sync(stage, papers)
        log("Done: " + ", ".join(f"{n} {s}" for s, n in sorted(counts.items())))
        rescore(store, topic_id)
        store.commit()
        return cost
    batch_id = reviewer.submit_batch(stage, papers)
    log(f"Submitted batch {batch_id}. Most finish within an hour.")
    if wait:
        run_review(store, http, config, topic_id, topic_name, stage, limit=0, wait=True, log=log)
    return cost


def review_spend(store: Store, topic_ids: list[int]) -> float:
    marks = ",".join("?" * len(topic_ids))
    return store.conn.execute(
        f"SELECT COALESCE(SUM(cost_usd), 0) FROM reviews WHERE topic_id IN ({marks})", topic_ids
    ).fetchone()[0] if topic_ids else 0.0


# -- full text ----------------------------------------------------------------------

@dataclass
class FetchSummary:
    attempted: int = 0
    downloaded: int = 0
    disabled_routes: tuple[str, ...] = ()


def run_fetch(store: Store, http: HttpClient, config: Config, topic_id: int, *, top: int | None = None,
              retry: bool = False, min_score: float = 0.0, workers: int = 6, allow_paid: bool = False,
              verbose: bool = False, log: Log = print) -> FetchSummary:
    candidates = store.topic_papers(topic_id, status="new")
    if retry:
        candidates += store.topic_papers(topic_id, status="unavailable")
        candidates.sort(key=lambda p: p.score, reverse=True)
    candidates = [p for p in candidates if p.score >= min_score][:top]
    summary = FetchSummary(attempted=len(candidates))
    if not candidates:
        log("Nothing to fetch.")
        return summary

    acquirer = Acquirer(config, store, http, allow_paid=allow_paid)
    if len(candidates) > 20:
        log(f"Semantic Scholar lists open-access copies for {acquirer.prefetch(candidates)} "
            f"of {len(candidates)} papers.")
    done = {"n": 0}

    def report(paper: StoredPaper, outcome) -> None:
        done["n"] += 1
        log(f"[{done['n']}/{len(candidates)}] {clip(paper.title, 80)}")
        if outcome.ok:
            summary.downloaded += 1
            kinds = " + ".join(k for k, v in (("pdf", outcome.pdf), ("xml", outcome.xml)) if v)
            store.set_status(topic_id, paper.id, "downloaded", note=None if outcome.pdf else "xml only")
            log(f"    ok: {kinds} -> {(outcome.pdf or outcome.xml).parent}")
        else:
            store.set_status(topic_id, paper.id, "unavailable", note="no open-access copy downloaded")
            log(f"    not downloaded; manual: {paper.landing_url or '-'}")
        if verbose or not outcome.ok:
            for line in outcome.attempts:
                log(f"      - {line}")
        store.commit()

    acquirer.fetch_many(candidates, workers=workers, on_done=report)
    summary.disabled_routes = tuple(acquirer.disabled_routes)
    if summary.disabled_routes:
        log(f"Routes switched off after repeated failures: {', '.join(summary.disabled_routes)}")
    log(f"Downloaded {summary.downloaded} of {len(candidates)}. Library: {config.papers_dir}")
    return summary


def refresh_oa_status(store: Store, http: HttpClient, config: Config, topic_ids: list[int],
                      log: Log = print) -> None:
    """Fetch OpenAlex open-access status for papers that lack it (~$0.0001 per 100)."""
    missing = sorted({oid for tid in topic_ids for oid, pid in store.aliases(topic_id=tid).items()
                      if store.get_paper(pid).oa_status is None})
    if missing:
        openalex = OpenAlex(http, config.openalex_api_key)
        for paper in openalex.get_many(missing):
            store.add_paper(paper)
        store.commit()
        log(f"Fetched open-access status for {len(missing)} OpenAlex records (${openalex.cost_usd:.4f}).")


# -- reading list -------------------------------------------------------------------

def write_reading_list(store: Store, topic_id: int, topic_name: str, out_dir: Path, inbox: Path,
                       pending: int = 30) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "reading-list.md"
    topic_links = store.topic_links(topic_id)

    def entry(p: StoredPaper, with_file: bool) -> str:
        authors = ", ".join(p.authors[:3]) + (" et al." if len(p.authors) > 3 else "")
        cited_in, cites_out = topic_links.get(p.id, (0, 0))
        lines = [f"### {p.tags + ' ' if p.tags else ''}{p.title}",
                 f"{authors or 'Unknown authors'} · {p.year or 'n.d.'} · {p.venue or '-'} · "
                 f"cited {p.cited_by or 0} · cited by {cited_in} / cites {cites_out} topic papers · "
                 f"score {p.score:.2f} · id {p.id}"]
        links = []
        if with_file:
            for kind, row in store.files(p.id).items():
                links.append(f"[{kind.upper()}]({_link(store.resolve(row['path']), out_dir)})")
        if p.landing_url:
            links.append(f"[source]({p.landing_url})")
        if links:
            lines.append(" · ".join(links))
        if p.note:
            lines.append(f"*{p.note}*")
        if p.abstract:
            lines.append("> " + clip(p.abstract, 600))
        return "\n".join(lines)

    sections = [
        ("Downloaded", store.topic_papers(topic_id, status="downloaded"), True),
        (f"Needs manual retrieval (download via the UNED library into `{inbox}`, then run "
         "`scientia import --move`)", store.topic_papers(topic_id, status="unavailable"), False),
        ("Not fetched yet", store.topic_papers(topic_id, status="new", limit=pending), False),
        ("Read", store.topic_papers(topic_id, status="read"), True),
    ]
    queries = "\n".join(f"- **{q['label']}**: `{q['query']}`" for q in store.queries(topic_id))
    parts = [f"# {topic_name}", f"Generated {date.today().isoformat()} from these queries:\n\n{queries}"]
    core = [p for p in view(store, topic_id, store.topic_papers(topic_id), "foundational")[:10]
            if topic_links.get(p.id, (0, 0))[0] > 0]
    if core:
        parts.append("## Core papers (most cited within this topic)")
        parts.append("\n".join(
            f"{i}. {p.tags + ' ' if p.tags else ''}{p.title} ({p.year or 'n.d.'}) — cited by "
            f"{topic_links[p.id][0]} topic papers · id {p.id} · {p.status}"
            for i, p in enumerate(core, 1)
        ))
    for title, papers, with_file in sections:
        if papers:
            parts.append(f"## {title} ({len(papers)})")
            parts.extend(entry(p, with_file) for p in papers)
    path.write_text("\n\n".join(parts) + "\n", encoding="utf-8")
    return path


def clip(text: str, width: int) -> str:
    return textwrap.shorten(text, width=width, placeholder="…")


def _link(path: Path, base: Path) -> str:
    """Relative link when possible (same drive), else an absolute file URI."""
    try:
        return os.path.relpath(path, base).replace(os.sep, "/")
    except ValueError:
        return path.resolve().as_uri()
