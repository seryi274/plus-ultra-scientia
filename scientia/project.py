"""Research projects: a folder whose project.toml declares the topics, their queries,
research questions, review profiles and citation-expansion steps. `scientia run <folder>`
brings the library up to date with it, skipping what has already been done, and writes the
project's outputs (reading lists, evidence tables, inventory) to <folder>/outputs/.

The project folder holds the research (versionable, shareable); the shared library under
SCIENTIA_HOME holds the data (papers, PDFs, database), which is never versioned.

    name = "axspa"
    description = "..."
    max_review_cost = 15.0          # USD per run, all LLM stages and topics together

    [vars]                          # ${NAME} in any query is replaced by its value
    AXSPA = '("ankylosing spondylitis" OR "axial spondyloarthritis")'

    [defaults]                      # per-topic settings fall back to these
    sources = ["openalex", "arxiv", "europepmc"]
    limit = 100                     # results per query and source

    [[topics]]
    name = "axspa-treatment"
    question = "..."                # what triage and extraction judge papers against
    profile = "clinical"            # clinical | genetics | general | schemas/<file>.json
    triage_hints = "..."            # optional project guidance appended to the prompts
    extract_hints = "..."
    fetch = true                    # download open-access full text (default true)

    [topics.queries]                # label = query
    il17 = '${AXSPA} AND (secukinumab OR ixekizumab OR bimekizumab)'

    [[topics.expand]]               # one table per citation-expansion step, run once each
    direction = "both"              # also: seeds, min_links, max_new, since, skip_types
"""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from string import Template

from scientia.config import Config
from scientia.evidence import write_evidence
from scientia.inventory import format_counts, write_inventory
from scientia.net import HttpClient
from scientia.pipeline import (
    SKIPPED_TYPES, SOURCES, Log, expand_params, expanded_before, refresh_oa_status, rescore, review_spend, run_expand,
    run_fetch, run_review, run_search, search_clients, searched_before, write_reading_list,
)
from scientia.review import PROFILES
from scientia.store import Store

STEPS = ("search", "expand", "triage", "fetch", "extract", "evidence", "report", "inventory")
DEFAULT_STEPS = tuple(s for s in STEPS if s != "extract")  # extraction is the costly step: opt in


class ProjectError(ValueError):
    pass


@dataclass
class TopicSpec:
    name: str
    question: str | None
    profile: str | None
    schema: dict | None
    hints: dict[str, str]
    queries: dict[str, str]
    sources: list[str]
    limit: int
    expand: list[dict]
    fetch: bool = True


@dataclass
class Project:
    root: Path
    name: str
    description: str
    max_review_cost: float | None
    topics: list[TopicSpec] = field(default_factory=list)

    @property
    def outputs(self) -> Path:
        return self.root / "outputs"


def load_project(path: Path) -> Project:
    root = path if path.is_dir() else path.parent
    file = root / "project.toml" if path.is_dir() else path
    if not file.exists():
        raise ProjectError(f"no project.toml in {root}")
    try:
        data = tomllib.loads(file.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ProjectError(f"{file}: {exc}") from None

    variables = {k: str(v) for k, v in data.get("vars", {}).items()}
    defaults = data.get("defaults", {})
    project = Project(root=root.resolve(), name=data.get("name", root.name),
                      description=data.get("description", ""), max_review_cost=data.get("max_review_cost"))
    seen = set()
    for t in data.get("topics", []):
        name = t.get("name") or _fail("every [[topics]] entry needs a name")
        if name in seen:
            _fail(f"topic {name!r} is declared twice")
        seen.add(name)
        queries = {}
        for label, query in t.get("queries", {}).items():
            try:
                queries[label] = Template(query).substitute(variables)
            except KeyError as exc:
                _fail(f"topic {name!r}, query {label!r}: unknown variable ${{{exc.args[0]}}}")
        sources = t.get("sources", defaults.get("sources", list(SOURCES)))
        if unknown := set(sources) - set(SOURCES):
            _fail(f"topic {name!r}: unknown source(s) {', '.join(sorted(unknown))}")
        profile, schema = t.get("profile"), None
        if profile and profile.endswith(".json"):
            schema_file = root / profile
            if not schema_file.exists():
                _fail(f"topic {name!r}: schema file {schema_file} not found")
            schema = json.loads(schema_file.read_text(encoding="utf-8"))
            profile = schema_file.stem
        elif profile and profile not in PROFILES:
            _fail(f"topic {name!r}: profile must be one of {', '.join(PROFILES)} or a schemas/*.json file")
        steps = []
        for step in t.get("expand", []):
            unknown_keys = set(step) - {"seeds", "direction", "min_links", "max_new", "since", "skip_types"}
            if unknown_keys:
                _fail(f"topic {name!r}, expand step: unknown setting(s) {', '.join(sorted(unknown_keys))}")
            steps.append(expand_params(**step))
        project.topics.append(TopicSpec(
            name=name, question=t.get("question"), profile=profile, schema=schema,
            hints={k: t[f"{k}_hints"] for k in ("triage", "extract") if t.get(f"{k}_hints")},
            queries=queries, sources=sources, limit=t.get("limit", defaults.get("limit", 50)),
            expand=steps, fetch=t.get("fetch", defaults.get("fetch", True)),
        ))
    if not project.topics:
        _fail(f"{file} declares no [[topics]]")
    return project


def _fail(message: str):
    raise ProjectError(message)


def sync_project(project: Project, store: Store, log: Log = print) -> None:
    """Make the library's topics match the project file: queries and review settings.
    Queries in the library but no longer in the file are reported, not deleted."""
    for spec in project.topics:
        topic_id = store.upsert_topic(spec.name)
        existing = {q["label"]: q["query"] for q in store.queries(topic_id)}
        for label, query in spec.queries.items():
            if existing.get(label) != query:
                store.add_query(topic_id, query, label)
                log(f"[{spec.name}] {'updated' if label in existing else 'added'} query '{label}'")
        if extra := set(existing) - set(spec.queries):
            log(f"[{spec.name}] in the library but not the project file: {', '.join(sorted(extra))} "
                f"(remove with `scientia queries {spec.name} --remove <label>`)")
        store.set_review_settings(topic_id, spec.question, spec.profile, spec.schema, spec.hints)
    store.commit()


def run_project(project: Project, store: Store, http: HttpClient, config: Config, *,
                steps: tuple[str, ...] = DEFAULT_STEPS, only: list[str] | None = None, refresh: bool = False,
                wait: bool = False, dry_run: bool = False, log: Log = print) -> int:
    specs = [t for t in project.topics if not only or t.name in only]
    if only and (missing := set(only) - {t.name for t in specs}):
        raise ProjectError(f"not in the project: {', '.join(sorted(missing))}")
    sync_project(project, store, log)
    clients = search_clients(http, config)
    budget = project.max_review_cost
    planned = 0.0

    for spec in specs:
        topic_id = store.topic_id(spec.name)
        log(f"\n=== {spec.name}")

        if "search" in steps:
            for label, query in spec.queries.items():
                todo = [s for s in spec.sources if refresh or not searched_before(store, topic_id, s, query)]
                if not todo:
                    continue
                if dry_run:
                    log(f"would search [{label}] on {', '.join(todo)}")
                else:
                    run_search(store, clients, topic_id, label, query, todo, limit=spec.limit, log=log)
            if not dry_run:
                rescore(store, topic_id)
                store.commit()

        if "expand" in steps:
            for params in spec.expand:
                if not refresh and expanded_before(store, topic_id, params):
                    continue
                if dry_run:
                    log(f"would expand: {_describe(params)}")
                else:
                    log(f"expanding: {_describe(params)}")
                    run_expand(store, http, config, topic_id, params, log=log)

        for stage in ("triage", "extract"):
            if stage not in steps or not spec.question:
                continue
            remaining = None if budget is None else max(0.0, budget - planned)
            cost = run_review(store, http, config, topic_id, spec.name, stage, wait=wait, dry_run=dry_run,
                              max_cost=remaining, log=log)
            if cost is None:
                return 2
            planned += cost

        if "fetch" in steps and spec.fetch:
            if dry_run:
                pending = len(store.topic_papers(topic_id, status="new"))
                log(f"would try to download {pending} papers not yet attempted")
            else:
                run_fetch(store, http, config, topic_id, retry=refresh, log=log)

        out = project.outputs / spec.name
        if "evidence" in steps and not dry_run and store.reviews(topic_id, "extract"):
            settings = store.review_settings(topic_id)
            md, _ = write_evidence(store, topic_id, spec.name, settings["profile"] or "general",
                                   settings["question"] or "", out)
            log(f"wrote {md}")
        if "report" in steps and not dry_run:
            log(f"wrote {write_reading_list(store, topic_id, spec.name, out, config.inbox_dir)}")

    if "inventory" in steps and not dry_run:
        names = [t.name for t in project.topics]
        refresh_oa_status(store, http, config, [store.topic_id(n) for n in names], log=log)
        path = project.outputs / "inventory.csv"
        log("\n" + format_counts(write_inventory(store, names, path)))
        log(f"wrote {path}")

    spent = review_spend(store, [store.topic_id(t.name) for t in project.topics])
    verb = "would cost" if dry_run else "submitted"
    log(f"\nLLM review: {verb} ~${planned:.2f} this run; ${spent:.2f} recorded for the project so far.")
    return 0


def _describe(params: dict) -> str:
    extras = [f"since {params['since']}"] if params["since"] else []
    added_skips = sorted(set(params["skip_types"]) - set(SKIPPED_TYPES))
    if added_skips:
        extras.append(f"skipping {', '.join(added_skips)}")
    return (f"{params['direction']}, {params['seeds']} seeds, min {params['min_links']} links, "
            f"max {params['max_new']} new" + (f" ({'; '.join(extras)})" if extras else ""))
