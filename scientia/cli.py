"""Command-line interface.

    scientia search <topic> "<query>" [--since 2023-01-01] [--limit 50] [--sources ...]
    scientia list   <topic> [--top 20] [--status new]
    scientia fetch  <topic> [--top 10] [--min-score 0.5] [--allow-paid]
    scientia import [pdfs or folders] [--topic <topic>] [--move]
    scientia report <topic>
    scientia mark   <topic> <paper> read|skipped|new
    scientia topics
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
from scientia.importer import Importer, pdf_files
from scientia.net import HttpClient
from scientia.sources.arxiv import Arxiv
from scientia.sources.europepmc import EuropePMC
from scientia.sources.openalex import OpenAlex
from scientia.store import Store, StoredPaper

SOURCES = ("openalex", "arxiv", "europepmc")
# Per-source spelling of the --sort choices; None means the source's relevance order.
_SORT = {
    "openalex": {"recent": "-publication_date", "cited": "-cited_by_count"},
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
    p.add_argument("query", nargs="?", help="search query (defaults to the topic's saved query)")
    p.add_argument("--since", type=_iso_date, help="earliest publication date, YYYY-MM-DD")
    p.add_argument("--until", type=_iso_date, help="latest publication date, YYYY-MM-DD")
    p.add_argument("--limit", type=int, default=50, help="results per source (default 50)")
    p.add_argument("--sources", default=",".join(SOURCES),
                   help=f"comma-separated subset of {','.join(SOURCES)}")
    p.add_argument("--sort", choices=("relevance", "recent", "cited"), default="relevance")
    p.add_argument("--show", type=int, default=15, help="how many top results to print")
    p.set_defaults(handler=cmd_search)

    p = sub.add_parser("list", help="show a topic's papers, best first")
    p.add_argument("topic")
    p.add_argument("--top", type=int, default=20)
    p.add_argument("--status", choices=("new", "downloaded", "unavailable", "read", "skipped"))
    p.set_defaults(handler=cmd_list)

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
    return parser


# -- commands -------------------------------------------------------------------

def cmd_search(args, config: Config, store: Store, http: HttpClient) -> int:
    query = args.query
    if not query:
        row = store.conn.execute("SELECT query FROM topics WHERE name = ?", (args.topic,)).fetchone()
        if not row:
            print("error: a new topic needs a query", file=sys.stderr)
            return 2
        query = row["query"]
    topic_id = store.upsert_topic(args.topic, query)

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
    for name in wanted:
        params = {"limit": args.limit, "since": args.since, "until": args.until,
                  "sort": _SORT[name].get(args.sort)}
        try:
            papers = clients[name].search(query, **params)
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

    _rescore(store, topic_id, query)
    store.commit()
    print(f"\n{new_total} new papers in '{args.topic}'. Top {args.show}:\n")
    _print_table(store.topic_papers(topic_id, limit=args.show))
    return 0


def cmd_list(args, config: Config, store: Store, http: HttpClient) -> int:
    _print_table(store.topic_papers(store.topic_id(args.topic), status=args.status, limit=args.top))
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
    query = store.conn.execute("SELECT query FROM topics WHERE id = ?", (topic_id,)).fetchone()["query"]
    out_dir = config.topics_dir / _slug(args.topic)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "reading-list.md"

    def entry(p: StoredPaper, with_file: bool) -> str:
        authors = ", ".join(p.authors[:3]) + (" et al." if len(p.authors) > 3 else "")
        lines = [f"### {p.title}",
                 f"{authors or 'Unknown authors'} · {p.year or 'n.d.'} · {p.venue or '-'} · "
                 f"cited {p.cited_by or 0} · score {p.score:.2f} · id {p.id}"]
        links = []
        if with_file:
            for kind, row in store.files(p.id).items():
                rel = _relative(row["path"], out_dir)
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
    parts = [f"# {args.topic}", f"Query: `{query}` · generated {date.today().isoformat()}"]
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


def cmd_topics(args, config: Config, store: Store, http: HttpClient) -> int:
    rows = store.topics()
    if not rows:
        print("No topics yet. Start one with: scientia search <topic> \"<query>\"")
    for r in rows:
        print(f"{r['name']:<28} {r['papers']:>5} papers  {r['downloaded'] or 0:>4} downloaded   {r['query']}")
    return 0


# -- helpers --------------------------------------------------------------------

def _rescore(store: Store, topic_id: int, query: str) -> None:
    terms = ranking.query_terms(query)
    for p in store.topic_papers(topic_id):
        store.set_score(topic_id, p.id, ranking.score(terms, p.title, p.abstract, p.year, p.cited_by))


def _print_table(papers: list[StoredPaper]) -> None:
    if not papers:
        print("(no papers)")
        return
    print(f"{'id':>5}  {'score':>5}  {'year':>4}  {'cites':>6}  {'status':<11} {'full text':<9}  title")
    for p in papers:
        access = "arXiv" if p.arxiv_id else ("OA" if p.is_oa else ("cached" if p.openalex_has_pdf else "-"))
        print(f"{p.id:>5}  {p.score:>5.2f}  {p.year or '':>4}  {p.cited_by or 0:>6}  "
              f"{p.status:<11} {access:<9}  {_clip(p.title, 70)}")


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
