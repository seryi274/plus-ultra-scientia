"""Command-line interface.

    scientia run <project folder> [--steps ...] [--topic <name>] [--with-extract] [--wait] [--dry-run]

    scientia search <topic> "<query>" [--label il17] [--since 2023-01-01] [--limit 50]
    scientia search <topic>                  (re-run all of the topic's saved queries)
    scientia queries <topic> [--remove <label>]
    scientia list   <topic> [--top 20] [--status new] [--sort foundational|frontier|recent]
    scientia expand <topic> [--seeds 25] [--direction both] [--min-links 2]
    scientia fetch  <topic> [--top 10] [--min-score 0.5] [--allow-paid]
    scientia import [pdfs or folders] [--topic <topic>] [--move]
    scientia review <topic> --question "..." --profile clinical|genetics|general
    scientia review <topic> --stage extract [--wait]
    scientia evidence <topic>
    scientia inventory <topic> [<topic> ...]
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
from datetime import date
from pathlib import Path

from scientia.config import Config
from scientia.evidence import write_evidence
from scientia.importer import Importer, pdf_files
from scientia.inventory import format_counts, write_inventory
from scientia.net import HttpClient
from scientia.pipeline import (
    SKIPPED_TYPES, SOURCES, clip, expand_params, refresh_oa_status, rescore, run_expand, run_fetch,
    run_review, run_search, search_clients, view, write_reading_list,
)
from scientia.project import DEFAULT_STEPS, STEPS, ProjectError, load_project, run_project
from scientia.review import PROFILES, triage_results
from scientia.sources.openalex import OpenAlex
from scientia.store import Store, StoredPaper


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = _parser().parse_args(argv)
    config = Config.from_env()
    store = Store(config.db_path)
    try:
        with HttpClient(email=config.email) as http:
            return args.handler(args, config, store, http) or 0
    except (KeyError, ProjectError) as exc:
        print(f"error: {exc.args[0]}", file=sys.stderr)
        return 2
    finally:
        store.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="scientia", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(required=True, metavar="command")

    p = sub.add_parser("run", help="bring a research project (folder with project.toml) up to date")
    p.add_argument("project", type=Path, help="project folder, or its project.toml")
    p.add_argument("--steps", default=",".join(DEFAULT_STEPS),
                   help=f"comma-separated subset of {','.join(STEPS)} (default: all but extract)")
    p.add_argument("--with-extract", action="store_true", help="also run Opus evidence extraction (costly)")
    p.add_argument("--topic", action="append", help="only this topic (repeatable)")
    p.add_argument("--refresh", action="store_true",
                   help="re-run searches and expansions that already ran, and retry failed downloads")
    p.add_argument("--wait", action="store_true", help="wait for LLM review batches to finish")
    p.add_argument("--dry-run", action="store_true",
                   help="apply the project file, then only show what would run and what it would cost")
    p.set_defaults(handler=cmd_run)

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
    p.add_argument("--workers", type=int, default=6,
                   help="papers fetched in parallel (each site's rate limit still applies; default 6)")
    p.set_defaults(handler=cmd_fetch)

    p = sub.add_parser("import", help="file PDFs you downloaded yourself (e.g. via the UNED library)")
    p.add_argument("paths", nargs="*", type=Path,
                   help="PDF files or folders (default: the library's inbox/ folder)")
    p.add_argument("--topic", help="also add matched papers to this topic, looking up unknown DOIs")
    p.add_argument("--move", action="store_true", help="move files instead of copying them")
    p.add_argument("--force", action="store_true", help="replace a PDF the paper already has")
    p.set_defaults(handler=cmd_import)

    p = sub.add_parser("review", help="LLM review of abstracts: Haiku triage, then Opus evidence extraction")
    p.add_argument("topic")
    p.add_argument("--question", help="the research question papers are judged against (saved on the topic)")
    p.add_argument("--profile", choices=tuple(PROFILES),
                   help="extraction schema (saved on the topic): clinical, genetics or general")
    p.add_argument("--stage", choices=("triage", "extract"), default="triage",
                   help="triage = relevance and design for every paper (Haiku); "
                        "extract = evidence from central/relevant papers (Opus)")
    p.add_argument("--include-tangential", action="store_true", help="extract: also papers rated tangential")
    p.add_argument("--limit", type=int, help="review at most this many papers")
    p.add_argument("--max-cost", type=float, help="cap the estimated cost (USD); reviews the top papers that fit")
    p.add_argument("--sync", action="store_true",
                   help="send requests one at a time instead of a batch (full price; for small runs)")
    p.add_argument("--wait", action="store_true", help="wait for submitted batches to finish and collect them")
    p.add_argument("--dry-run", action="store_true", help="show what would be reviewed and the cost estimate")
    p.set_defaults(handler=cmd_review)

    p = sub.add_parser("evidence", help="write evidence tables (Markdown + CSV) from a topic's reviews")
    p.add_argument("topic")
    p.set_defaults(handler=cmd_evidence)

    p = sub.add_parser("inventory", help="full paper list with relevance and access status, as CSV")
    p.add_argument("topics", nargs="+", help="one or more topics; a paper in several appears once")
    p.add_argument("--out", type=Path, help="CSV path (default: <library>/topics/inventory-<topics>.csv)")
    p.add_argument("--no-refresh", action="store_true",
                   help="don't fetch missing open-access statuses from OpenAlex (about $0.0001 per 100 papers)")
    p.set_defaults(handler=cmd_inventory)

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

def cmd_run(args, config: Config, store: Store, http: HttpClient) -> int:
    steps = tuple(s.strip() for s in args.steps.split(",") if s.strip())
    if unknown := set(steps) - set(STEPS):
        print(f"error: unknown step(s): {', '.join(sorted(unknown))}", file=sys.stderr)
        return 2
    if args.with_extract and "extract" not in steps:
        steps += ("extract",)
    project = load_project(args.project)
    print(f"Project: {project.name} ({project.root})" + (f" - {project.description}" if project.description else ""))
    return run_project(project, store, http, config, steps=tuple(s for s in STEPS if s in steps),
                       only=args.topic, refresh=args.refresh, wait=args.wait, dry_run=args.dry_run)


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

    wanted = [s.strip() for s in args.sources.split(",") if s.strip()]
    if unknown := set(wanted) - set(SOURCES):
        print(f"error: unknown source(s): {', '.join(sorted(unknown))}", file=sys.stderr)
        return 2
    if "openalex" in wanted and not config.openalex_api_key:
        print("note: OPENALEX_API_KEY is not set; sharing the anonymous $0.10/day budget.")

    clients = search_clients(http, config)
    new_total = sum(run_search(store, clients, topic_id, label, query, wanted, limit=args.limit,
                               since=args.since, until=args.until, sort=args.sort)
                    for label, query in to_run)
    rescore(store, topic_id)
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
        rescore(store, topic_id)
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
    rescore(store, topic_id)  # cheap, and keeps scores in step with citation data
    store.commit()
    papers = store.topic_papers(topic_id, status=args.status)
    if args.sort != "score":
        papers = view(store, topic_id, papers, args.sort)
    _print_table(store, topic_id, papers[: args.top])
    return 0


def cmd_expand(args, config: Config, store: Store, http: HttpClient) -> int:
    topic_id = store.topic_id(args.topic)
    params = expand_params(args.seeds, args.direction, args.min_links, args.max_new, args.since,
                           [t.strip() for t in args.skip_types.split(",") if t.strip()])
    result = run_expand(store, http, config, topic_id, params)
    if result is None:
        return 1
    added = result.added["backward"] + result.added["forward"]
    if added and args.show > 0:
        newly = [p for p in store.topic_papers(topic_id) if p.id in set(added)][: args.show]
        print("\nTop new papers:\n")
        _print_table(store, topic_id, newly, notes=True)
    return 0


def cmd_fetch(args, config: Config, store: Store, http: HttpClient) -> int:
    topic_id = store.topic_id(args.topic)
    run_fetch(store, http, config, topic_id, top=args.top, retry=args.retry, min_score=args.min_score,
              workers=args.workers, allow_paid=args.allow_paid, verbose=args.verbose)
    print(f"For the full list with reasons: scientia inventory {args.topic}")
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
            target = f" -> [{result.paper_id}] {clip(store.get_paper(result.paper_id).title, 60)}"
        print(f"  {result.status:<12} {path.name}{target}")
        print(f"  {'':<12} {result.detail}")
    print("\n" + ", ".join(f"{n} {status}" for status, n in sorted(counts.items())))
    if counts.get("unmatched"):
        hint = " --topic <topic>" if not args.topic else ""
        print(f"Unmatched files were left in place. Run `scientia search` for them first, or retry with{hint}"
              " if they carry a DOI.")
    return 0


def cmd_review(args, config: Config, store: Store, http: HttpClient) -> int:
    topic_id = store.topic_id(args.topic)
    store.set_review_settings(topic_id, args.question, args.profile)
    store.commit()
    cost = run_review(store, http, config, topic_id, args.topic, args.stage, limit=args.limit,
                      include_tangential=args.include_tangential, sync=args.sync, wait=args.wait,
                      dry_run=args.dry_run, max_cost=args.max_cost)
    return 2 if cost is None else 0


def cmd_evidence(args, config: Config, store: Store, http: HttpClient) -> int:
    topic_id = store.topic_id(args.topic)
    settings = store.review_settings(topic_id)
    if not settings["question"]:
        print(f"error: '{args.topic}' has not been reviewed yet; run `scientia review` first", file=sys.stderr)
        return 2
    md, csv_path = write_evidence(store, topic_id, args.topic, settings["profile"] or "general",
                                  settings["question"], config.topics_dir / _slug(args.topic))
    print(f"Wrote {md}\nWrote {csv_path}")
    return 0


def cmd_inventory(args, config: Config, store: Store, http: HttpClient) -> int:
    topic_ids = [store.topic_id(t) for t in args.topics]
    if not args.no_refresh:
        refresh_oa_status(store, http, config, topic_ids)
    out = args.out or config.topics_dir / f"inventory-{'+'.join(_slug(t) for t in args.topics)}.csv"
    print("\n" + format_counts(write_inventory(store, args.topics, out)))
    print(f"\nWrote {out}")
    return 0


def cmd_report(args, config: Config, store: Store, http: HttpClient) -> int:
    topic_id = store.topic_id(args.topic)
    path = write_reading_list(store, topic_id, args.topic, config.topics_dir / _slug(args.topic),
                              config.inbox_dir, pending=args.pending)
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
    print(f"{clip(paper.title, 70)} -> {args.status}")
    return 0


def cmd_info(args, config: Config, store: Store, http: HttpClient) -> int:
    source = "SCIENTIA_HOME" if os.environ.get("SCIENTIA_HOME") else "default; set SCIENTIA_HOME to change"
    papers = store.conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
    files = store.conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    print(f"Library:  {config.home}  ({source})")
    print(f"Inbox:    {config.inbox_dir}")
    print(f"Contents: {len(store.topics())} topics, {papers} papers, {files} files")
    for name, value in (("OPENALEX_API_KEY", config.openalex_api_key), ("SCIENTIA_EMAIL", config.email),
                        ("UNPAYWALL_EMAIL", config.unpaywall_email if config.unpaywall_email != config.email else None),
                        ("S2_API_KEY", config.s2_api_key), ("CORE_API_KEY", config.core_api_key),
                        ("ANTHROPIC_API_KEY", os.environ.get("ANTHROPIC_API_KEY"))):
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

def _print_table(store: Store, topic_id: int, papers: list[StoredPaper], notes: bool = False) -> None:
    """`links` is in/out: topic papers citing this one / topic papers it cites."""
    if not papers:
        print("(no papers)")
        return
    links = store.topic_links(topic_id)
    triage = triage_results(store, topic_id)
    print(f"{'id':>5}  {'score':>5}  {'year':>4}  {'cites':>6}  {'links':>5}  {'rel':<3} {'status':<11} "
          f"{'full text':<9}  title")
    for p in papers:
        access = "arXiv" if p.arxiv_id else ("OA" if p.is_oa else ("cached" if p.openalex_has_pdf else "-"))
        cited_in, cites_out = links.get(p.id, (0, 0))
        title = f"{p.tags} {p.title}" if p.tags else p.title
        rel = {"central": "C", "relevant": "R", "tangential": "t", "off_topic": "x"}.get(
            triage.get(p.id, {}).get("relevance"), "")
        print(f"{p.id:>5}  {p.score:>5.2f}  {p.year or '':>4}  {p.cited_by or 0:>6}  {f'{cited_in}/{cites_out}':>5}  "
              f"{rel:<3} {p.status:<11} {access:<9}  {clip(title, 70)}")
        if notes and p.note:
            print(f"{'':>56}{p.note}")


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9.\-]+", "-", text).strip("-").lower() or "topic"


def _iso_date(value: str) -> str:
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got {value!r}") from None
