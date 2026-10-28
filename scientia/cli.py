"""Command-line interface.

    scientia search <topic> "<query>" [--label il17] [--since 2023-01-01] [--limit 50]
    scientia search <topic>                  (re-run all of the topic's saved queries)
    scientia queries <topic> [--remove <label>]
    scientia list   <topic> [--top 20] [--status new]
    scientia expand <topic> [--seeds 25] [--direction both] [--min-links 2]
    scientia fetch  <topic> [--top 10] [--min-score 0.5] [--allow-paid]
    scientia import [pdfs or folders] [--topic <topic>] [--move]
    scientia report <topic>
    scientia mark   <topic> <paper> read|skipped|new
    scientia topics
    scientia info
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import textwrap
from datetime import date
from pathlib import Path

from scientia import ranking
from scientia.acquire import Acquirer
from scientia.config import Config
from scientia.expand import expand
from scientia.importer import Importer, pdf_files
from scientia.net import HttpClient
from scientia.sources.arxiv import Arxiv
from scientia.sources.europepmc import EuropePMC
from scientia.sources.openalex import OpenAlex
from scientia.store import Store, StoredPaper

SOURCES = ("openalex", "arxiv", "europepmc")
# OpenAlex work types that citation expansion never adds.
SKIPPED_TYPES = ("editorial", "letter", "erratum", "paratext", "retraction", "peer-review")
# Per-source spelling of the --sort choices; None means the source's relevance order.
_SORT = {
    "openalex": {"recent": "publication_date:desc", "cited": "cited_by_count:desc"},
    "arxiv": {"recent": "recent"},
    "europepmc": {"recent": "recent", "cited": "cited"},
}


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = _parser().parse_args(argv)
    config = Config.from_env()
    store = Store(config.db_path)
    try:
        with HttpClient(email=config.email) as http:
            return args.handler(args, config, store, http) or 0
    except KeyError as exc:
        print(f"error: {exc.args[0]}", file=sys.stderr)
        return 2
    finally:
        store.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="scientia", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(required=True, metavar="command")

    p = sub.add_parser("search", help="discover papers for a topic and add them to the library")
    p.add_argument("topic", help="short topic name, e.g. neural-operators")
    p.add_argument("query", nargs="?",
                   help="query to add to the topic and run; omit to re-run all saved queries")
    p.add_argument("--label", help="name for the query (default: derived from its text); "
                                   "without a query, run only the saved query with this label")
    p.add_argument("--since", type=_iso_date, help="earliest publication date, YYYY-MM-DD")
    p.add_argument("--until", type=_iso_date, help="latest publication date, YYYY-MM-DD")
    p.add_argument("--limit", type=int, default=50, help="results per source (default 50)")
    p.add_argument("--sources", default=",".join(SOURCES),
                   help=f"comma-separated subset of {','.join(SOURCES)}")
    p.add_argument("--sort", choices=("relevance", "recent", "cited"), default="relevance")
    p.add_argument("--show", type=int, default=15, help="how many top results to print")
    p.set_defaults(handler=cmd_search)

    p = sub.add_parser("queries", help="show or remove a topic's saved queries")
    p.add_argument("topic")
    p.add_argument("--remove", metavar="LABEL", help="remove the query with this label")
    p.set_defaults(handler=cmd_queries)

    p = sub.add_parser("list", help="show a topic's papers, best first")
    p.add_argument("topic")
    p.add_argument("--top", type=int, default=20)
    p.add_argument("--status", choices=("new", "downloaded", "unavailable", "read", "skipped"))
    p.add_argument("--sort", choices=("score", "foundational", "frontier", "recent"), default="score",
                   help="foundational = most cited by the topic's papers; frontier = recent papers "
                        "citing the most topic papers; recent = newest first")
    p.set_defaults(handler=cmd_list)

    p = sub.add_parser("expand", help="follow citations from the topic's best papers (OpenAlex)")
    p.add_argument("topic")
    p.add_argument("--seeds", type=int, default=25, help="top-scoring papers to expand from (default 25)")
    p.add_argument("--direction", choices=("both", "backward", "forward"), default="both",
                   help="backward = what seeds cite (foundations); forward = what cites them (frontier)")
    p.add_argument("--min-links", type=int, default=2,
                   help="add a paper only if linked to at least this many seeds/topic papers (default 2)")
    p.add_argument("--max-new", type=int, default=50, help="cap on papers added per direction (default 50)")
    p.add_argument("--since", type=_iso_date, help="forward only: citing papers published from this date")
    p.add_argument("--skip-types", default="",
                   help="extra OpenAlex work types to leave out, comma-separated; e.g. 'review' to add "
                        f"only primary research. Always left out: {','.join(SKIPPED_TYPES)}")
    p.add_argument("--show", type=int, default=15)
    p.set_defaults(handler=cmd_expand)

    p = sub.add_parser("fetch", help="download open-access full text for the top papers")
    p.add_argument("topic")
    p.add_argument("--top", type=int, default=10, help="how many papers to attempt")
    p.add_argument("--min-score", type=float, default=0.0)
    p.add_argument("--retry", action="store_true", help="also retry papers marked unavailable")
    p.add_argument("--allow-paid", action="store_true",
                   help="allow OpenAlex cached-PDF downloads ($0.01 each, from your daily budget)")
    p.add_argument("-v", "--verbose", action="store_true", help="show every route tried")
    p.set_defaults(handler=cmd_fetch)

    p = sub.add_parser("import", help="file PDFs you downloaded yourself (e.g. via the UNED library)")
    p.add_argument("paths", nargs="*", type=Path,
                   help="PDF files or folders (default: the library's inbox/ folder)")
    p.add_argument("--topic", help="also add matched papers to this topic, looking up unknown DOIs")
    p.add_argument("--move", action="store_true", help="move files instead of copying them")
    p.add_argument("--force", action="store_true", help="replace a PDF the paper already has")
    p.set_defaults(handler=cmd_import)

    p = sub.add_parser("report", help="write a Markdown reading list for a topic")
    p.add_argument("topic")
    p.add_argument("--pending", type=int, default=30, help="not-yet-fetched papers to include")
    p.set_defaults(handler=cmd_report)

    p = sub.add_parser("mark", help="set a paper's status within a topic")
    p.add_argument("topic")
    p.add_argument("paper", help="row id (from `list`), DOI, arXiv id, PMID, PMCID or OpenAlex id")
    p.add_argument("status", choices=("read", "skipped", "new"))
    p.add_argument("--note")
    p.set_defaults(handler=cmd_mark)

    p = sub.add_parser("topics", help="list topics")
    p.set_defaults(handler=cmd_topics)

    p = sub.add_parser("info", help="show the library location and which API keys are set")
    p.set_defaults(handler=cmd_info)
    return parser


# -- commands -------------------------------------------------------------------

def cmd_search(args, config: Config, store: Store, http: HttpClient) -> int:
    if args.query:
        topic_id = store.upsert_topic(args.topic)
        label = store.add_query(topic_id, args.query, args.label)
        to_run = [(label, args.query)]
    else:
        topic_id = store.topic_id(args.topic)
        to_run = [(q["label"], q["query"]) for q in store.queries(topic_id)
                  if not args.label or q["label"] == args.label]
        if not to_run:
            what = f"query labelled {args.label!r}" if args.label else "saved queries"
            print(f"error: topic '{args.topic}' has no {what}", file=sys.stderr)
            return 2

    clients = {
        "openalex": OpenAlex(http, config.openalex_api_key),
        "arxiv": Arxiv(http),
        "europepmc": EuropePMC(http),
    }
    wanted = [s.strip() for s in args.sources.split(",") if s.strip()]
    if unknown := set(wanted) - set(SOURCES):
        print(f"error: unknown source(s): {', '.join(sorted(unknown))}", file=sys.stderr)
        return 2
    if "openalex" in wanted and not config.openalex_api_key:
        print("note: OPENALEX_API_KEY is not set; sharing the anonymous $0.10/day budget.")

    new_total = 0
    for label, query in to_run:
        print(f"[{label}] {query}")
        for name in wanted:
            params = {"limit": args.limit, "since": args.since, "until": args.until,
                      "sort": _SORT[name].get(args.sort), "label": label}
            try:
                papers = clients[name].search(query, **{k: v for k, v in params.items() if k != "label"})
            except Exception as exc:  # one source being down should not sink the others
                store.log_search(topic_id, name, query, params, 0, error=str(exc))
                store.commit()
                print(f"  {name:<10} failed: {exc}")
                continue
            new_here = 0
            for paper in papers:
                paper_id, _ = store.add_paper(paper)
                new_here += store.link(topic_id, paper_id)
            store.log_search(topic_id, name, query, params, len(papers))
            store.commit()
            new_total += new_here
            print(f"  {name:<10} {len(papers):>4} results, {new_here:>4} new to topic")

    _rescore(store, topic_id)
    store.commit()
    print(f"\n{new_total} new papers in '{args.topic}'.")
    if args.show > 0:
        print(f"Top {args.show}:\n")
        _print_table(store, topic_id, store.topic_papers(topic_id, limit=args.show))
    return 0


def cmd_queries(args, config: Config, store: Store, http: HttpClient) -> int:
    topic_id = store.topic_id(args.topic)
    if args.remove:
        if not store.remove_query(topic_id, args.remove):
            print(f"error: no query labelled {args.remove!r}", file=sys.stderr)
            return 2
        _rescore(store, topic_id)
        store.commit()
        print(f"Removed [{args.remove}]. Papers it found stay in the topic.")
    rows = store.queries(topic_id)
    if not rows:
        print(f"'{args.topic}' has no queries. Add one with: scientia search {args.topic} \"<query>\"")
    for q in rows:
        print(f"  {q['label']:<32} {q['query']}")
    return 0


def cmd_list(args, config: Config, store: Store, http: HttpClient) -> int:
    topic_id = store.topic_id(args.topic)
    _rescore(store, topic_id)  # cheap, and keeps scores in step with citation data
    store.commit()
    papers = store.topic_papers(topic_id, status=args.status)
    if args.sort != "score":
        papers = _view(store, topic_id, papers, args.sort)
    _print_table(store, topic_id, papers[: args.top])
    return 0


def cmd_expand(args, config: Config, store: Store, http: HttpClient) -> int:
    topic_id = store.topic_id(args.topic)
    openalex = OpenAlex(http, config.openalex_api_key)
    params = {"seeds": args.seeds, "direction": args.direction, "min_links": args.min_links,
              "max_new": args.max_new, "since": args.since,
              "skip_types": [*SKIPPED_TYPES, *(t.strip() for t in args.skip_types.split(",") if t.strip())]}
    # Score with current citation links first, so the seeds are the topic's best papers.
    _rescore(store, topic_id)
    try:
        result = expand(store, openalex, topic_id, **params)
    except Exception as exc:
        store.commit()  # keep whatever was resolved before the failure
        store.log_search(topic_id, "openalex-expand", "citation expansion", params, 0, error=str(exc))
        store.commit()
        print(f"error: expansion failed: {exc}", file=sys.stderr)
        return 1
    added = result.added["backward"] + result.added["forward"]
    store.log_search(topic_id, "openalex-expand", "citation expansion", params, len(added))
    _rescore(store, topic_id)
    store.commit()

    print(f"Matched {result.resolved} more topic papers to OpenAlex"
          + (f" ({result.unresolved} have no OpenAlex record)" if result.unresolved else "")
          + f"; refreshed references of {result.refreshed} records.")
    print(f"Expanded from {result.seeds} seeds (min {args.min_links} links):")
    if args.direction in ("both", "backward"):
        print(f"  backward  {result.backward_candidates:>4} candidates, {len(result.added['backward']):>3} added")
    if args.direction in ("both", "forward"):
        print(f"  forward   {result.forward_candidates:>4} candidates, {len(result.added['forward']):>3} added")
    print(f"OpenAlex cost: ${openalex.cost_usd:.4f}")
    if added and args.show > 0:
        newly = [p for p in store.topic_papers(topic_id) if p.id in set(added)][: args.show]
        print(f"\nTop new papers:\n")
        _print_table(store, topic_id, newly, notes=True)
    return 0


def cmd_fetch(args, config: Config, store: Store, http: HttpClient) -> int:
    topic_id = store.topic_id(args.topic)
    candidates = store.topic_papers(topic_id, status="new")
    if args.retry:
        candidates += store.topic_papers(topic_id, status="unavailable")
        candidates.sort(key=lambda p: p.score, reverse=True)
    candidates = [p for p in candidates if p.score >= args.min_score][: args.top]
    if not candidates:
        print("Nothing to fetch.")
        return 0

    acquirer = Acquirer(config, store, http, allow_paid=args.allow_paid)
    got = 0
    for i, paper in enumerate(candidates, 1):
        print(f"[{i}/{len(candidates)}] {_clip(paper.title, 80)}")
        outcome = acquirer.fetch(paper)
        if outcome.ok:
            got += 1
            kinds = " + ".join(k for k, v in (("pdf", outcome.pdf), ("xml", outcome.xml)) if v)
            store.set_status(topic_id, paper.id, "downloaded", note=None if outcome.pdf else "xml only")
            print(f"    ok: {kinds} -> {(outcome.pdf or outcome.xml).parent}")
        else:
            store.set_status(topic_id, paper.id, "unavailable", note="no open-access copy found")
            print(f"    no open-access copy; manual: {paper.landing_url or '-'}")
        if args.verbose or not outcome.ok:
            for line in outcome.attempts:
                print(f"      - {line}")
        store.commit()
    print(f"\nDownloaded {got} of {len(candidates)}. Library: {config.papers_dir}")
    return 0


def cmd_import(args, config: Config, store: Store, http: HttpClient) -> int:
    topic_id = store.topic_id(args.topic) if args.topic else None
    config.inbox_dir.mkdir(parents=True, exist_ok=True)
    files = pdf_files(args.paths or [config.inbox_dir])
    if not files:
        where = ", ".join(map(str, args.paths)) if args.paths else str(config.inbox_dir)
        print(f"No PDFs found in {where}.")
        return 0

    importer = Importer(config, store, OpenAlex(http, config.openalex_api_key))
    counts: dict[str, int] = {}
    for path in files:
        result = importer.import_pdf(path, topic_id=topic_id, move=args.move, force=args.force)
        store.commit()
        counts[result.status] = counts.get(result.status, 0) + 1
        target = ""
        if result.paper_id is not None:
            target = f" -> [{result.paper_id}] {_clip(store.get_paper(result.paper_id).title, 60)}"
        print(f"  {result.status:<12} {path.name}{target}")
        print(f"  {'':<12} {result.detail}")
    print("\n" + ", ".join(f"{n} {status}" for status, n in sorted(counts.items())))
    if counts.get("unmatched"):
        hint = " --topic <topic>" if not args.topic else ""
        print(f"Unmatched files were left in place. Run `scientia search` for them first, or retry with{hint}"
              " if they carry a DOI.")
    return 0


def cmd_report(args, config: Config, store: Store, http: HttpClient) -> int:
    topic_id = store.topic_id(args.topic)
    out_dir = config.topics_dir / _slug(args.topic)
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
                rel = _relative(store.resolve(row["path"]), out_dir)
                links.append(f"[{kind.upper()}]({rel})")
        if p.landing_url:
            links.append(f"[source]({p.landing_url})")
        if links:
            lines.append(" · ".join(links))
        if p.note:
            lines.append(f"*{p.note}*")
        if p.abstract:
            lines.append("> " + _clip(p.abstract, 600))
        return "\n".join(lines)

    sections = [
        ("Downloaded", store.topic_papers(topic_id, status="downloaded"), True),
        ("Needs manual retrieval (download via the UNED library into "
         f"`{config.inbox_dir}`, then run `scientia import --move`)",
         store.topic_papers(topic_id, status="unavailable"), False),
        ("Not fetched yet", store.topic_papers(topic_id, status="new", limit=args.pending), False),
        ("Read", store.topic_papers(topic_id, status="read"), True),
    ]
    queries = "\n".join(f"- **{q['label']}**: `{q['query']}`" for q in store.queries(topic_id))
    parts = [f"# {args.topic}", f"Generated {date.today().isoformat()} from these queries:\n\n{queries}"]
    core = [p for p in _view(store, topic_id, store.topic_papers(topic_id), "foundational")[:10]
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
    print(f"Wrote {path}")
    return 0


def cmd_mark(args, config: Config, store: Store, http: HttpClient) -> int:
    topic_id = store.topic_id(args.topic)
    paper = store.find_paper(topic_id, args.paper)
    if not paper:
        print(f"error: no paper {args.paper!r} in topic '{args.topic}'", file=sys.stderr)
        return 2
    store.set_status(topic_id, paper.id, args.status, note=args.note)
    store.commit()
    print(f"{_clip(paper.title, 70)} -> {args.status}")
    return 0


def cmd_info(args, config: Config, store: Store, http: HttpClient) -> int:
    source = "SCIENTIA_HOME" if os.environ.get("SCIENTIA_HOME") else "default; set SCIENTIA_HOME to change"
    papers = store.conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
    files = store.conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    print(f"Library:  {config.home}  ({source})")
    print(f"Inbox:    {config.inbox_dir}")
    print(f"Contents: {len(store.topics())} topics, {papers} papers, {files} files")
    for name, value in (("OPENALEX_API_KEY", config.openalex_api_key), ("SCIENTIA_EMAIL", config.email),
                        ("S2_API_KEY", config.s2_api_key), ("CORE_API_KEY", config.core_api_key)):
        print(f"{name + ':':<18}{'set' if value else 'not set'}")
    return 0


def cmd_topics(args, config: Config, store: Store, http: HttpClient) -> int:
    print(f"Library: {config.home}\n")
    rows = store.topics()
    if not rows:
        print("No topics yet. Start one with: scientia search <topic> \"<query>\"")
    for r in rows:
        print(f"{r['name']:<28} {r['papers']:>5} papers  {r['downloaded'] or 0:>4} downloaded  "
              f"{r['queries']:>2} queries")
    return 0


# -- helpers --------------------------------------------------------------------

def _rescore(store: Store, topic_id: int) -> None:
    terms = ranking.term_sets([q["query"] for q in store.queries(topic_id)])
    links = store.topic_links(topic_id)
    max_links = max((ranking.weighted_links(*l) for l in links.values()), default=0.0)
    for p in store.topic_papers(topic_id):
        cited_in, cites_out = links.get(p.id, (0, 0))
        store.set_score(topic_id, p.id, ranking.score(terms, p.title, p.abstract, p.year, p.cited_by,
                                                      cited_in=cited_in, cites_out=cites_out,
                                                      max_links=max_links))


def _view(store: Store, topic_id: int, papers: list[StoredPaper], view: str) -> list[StoredPaper]:
    """Reorder papers for one reading purpose instead of the blended score."""
    links = store.topic_links(topic_id)
    if view == "foundational":
        return sorted(papers, key=lambda p: (-links.get(p.id, (0, 0))[0], -p.score))
    if view == "frontier":
        # Papers from the last three years, most connected to the topic first.
        horizon = date.today().year - 3
        recent = [p for p in papers if (p.year or 0) >= horizon]
        return sorted(recent, key=lambda p: (-links.get(p.id, (0, 0))[1], -p.score))
    if view == "recent":
        return sorted(papers, key=lambda p: (p.published or str(p.year or ""), p.score), reverse=True)
    return papers


def _print_table(store: Store, topic_id: int, papers: list[StoredPaper], notes: bool = False) -> None:
    """`links` is in/out: topic papers citing this one / topic papers it cites."""
    if not papers:
        print("(no papers)")
        return
    links = store.topic_links(topic_id)
    print(f"{'id':>5}  {'score':>5}  {'year':>4}  {'cites':>6}  {'links':>5}  {'status':<11} {'full text':<9}  title")
    for p in papers:
        access = "arXiv" if p.arxiv_id else ("OA" if p.is_oa else ("cached" if p.openalex_has_pdf else "-"))
        cited_in, cites_out = links.get(p.id, (0, 0))
        title = f"{p.tags} {p.title}" if p.tags else p.title
        print(f"{p.id:>5}  {p.score:>5.2f}  {p.year or '':>4}  {p.cited_by or 0:>6}  {f'{cited_in}/{cites_out}':>5}  "
              f"{p.status:<11} {access:<9}  {_clip(title, 70)}")
        if notes and p.note:
            print(f"{'':>56}{p.note}")


def _clip(text: str, width: int) -> str:
    return textwrap.shorten(text, width=width, placeholder="…")


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9.\-]+", "-", text).strip("-").lower() or "topic"


def _relative(path: str, base) -> str:
    return os.path.relpath(path, base).replace(os.sep, "/")


def _iso_date(value: str) -> str:
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got {value!r}") from None
