"""A glossary of a project's vocabulary, for readers new to the field.

The terms are the key_terms that triage records for every paper, counted by how many
papers (rated central, relevant or tangential) use them. Spelling variants of a term
(case, plurals, hyphens, HLA-style asterisks, Greek letters) are merged mechanically;
Claude Haiku then defines the frequent terms in plain language, in the project's
language, and names the synonyms that spelling alone cannot catch ("anti-TNF" and
"TNF inhibitor"), which are merged into one entry.

Definitions are stored in the library per language, so a later run only defines terms
that are new. Like everything model-written they can be wrong: entries the model was
unsure of are marked.
"""

from __future__ import annotations

import json
import math
import os
import re
import unicodedata
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import anthropic

from scientia.pipeline import Log, _link, clip
from scientia.review import PRICES, TRIAGE_MODEL, _enum, _list, _obj, _str, message_cost, triage_results
from scientia.store import Store, StoredPaper

MODEL = TRIAGE_MODEL
CHUNK = 20  # terms per request
_OUTPUT_PER_TERM = 250  # tokens, for cost estimates
_THINKING_PER_REQUEST = 2000
_RANK = {"central": 3, "relevant": 2, "tangential": 1}

CATEGORIES = (
    "condition", "anatomy_or_tissue", "cell_or_organism", "molecule_or_pathway", "gene_or_variant",
    "drug_or_intervention", "measure_or_criteria", "method_or_statistic", "dataset_or_study", "other",
)

GLOSSARY_SCHEMA = _obj(entries=_list(_obj(
    term=_str("the term exactly as given"),
    name_in_language=_str("its usual name in the reader's language, or the term itself"),
    expansion=_str("what an abbreviation stands for, in full; empty if not an abbreviation"),
    same_as=_str("a term from the full list meaning exactly the same; empty if none"),
    category=_enum(CATEGORIES),
    definition=_str("two or three plain sentences in the reader's language"),
    in_this_field=_str("one sentence in the reader's language"),
    confident={"type": "boolean"},
)))

_LANGUAGES = {"en": "English", "es": "Spanish", "fr": "French", "de": "German", "it": "Italian",
              "pt": "Portuguese", "ca": "Catalan"}

_SYSTEM = """You write a glossary for a reader who is new to a research field. They studied science at secondary school, have no university training in this field, and want to understand its papers.

The field, as the research questions of the reader's project:
{questions}

Every term in the glossary, with the number of papers that use it:
{term_list}

You will get some of these terms, each with the titles of papers that use it. For each, write:
- term: the term exactly as given
- name_in_language: its usual name in {language}; the term itself if it is used unchanged
- expansion: what an abbreviation stands for, in full; empty if the term is not an abbreviation
- same_as: only if the term means exactly the same thing as another term in the list above (an abbreviation, synonym, spelling or plural of it), that term as written in the list; otherwise empty. Not for a member of a class, a subtype or a related concept: a specific drug is not the same as its drug class, and IL-17A is not the same as IL-17.
- category: what kind of thing it is
- definition: two or three plain sentences in {language}: what it is, then what it does or how it is used. Explain or avoid any technical word you need.
- in_this_field: one sentence in {language} on why it matters for the research questions above
- confident: false if you are not sure what the term refers to here, for example an identifier you cannot place; then say only what the titles show.

Be accurate: the reader cannot check you. Prefer a simpler true statement to a detailed one you are unsure of."""

_LABELS = {
    "en": {
        "title": "Glossary", "papers": "{n} papers", "also": "also", "field": "In this field",
        "examples": "Examples", "check": "⚠ check",
        "intro": "Generated {date} by {model} from the key terms of {papers} papers ({terms} terms used in "
                 "at least {min} papers). The definitions are model-written: those marked ⚠ the model was "
                 "unsure of, and any of them may contain mistakes.",
        "categories": {
            "condition": "Conditions and symptoms", "anatomy_or_tissue": "Anatomy and tissues",
            "cell_or_organism": "Cells and organisms", "molecule_or_pathway": "Molecules and pathways",
            "gene_or_variant": "Genes and variants", "drug_or_intervention": "Drugs and interventions",
            "measure_or_criteria": "Measures, scores and criteria", "method_or_statistic": "Methods and statistics",
            "dataset_or_study": "Datasets, cohorts and named studies", "other": "Other",
        },
    },
    "es": {
        "title": "Glosario", "papers": "{n} artículos", "also": "también", "field": "En este campo",
        "examples": "Ejemplos", "check": "⚠ comprobar",
        "intro": "Generado el {date} por {model} a partir de los términos clave de {papers} artículos "
                 "({terms} términos que aparecen en al menos {min} artículos). Las definiciones las escribe "
                 "un modelo: las marcadas con ⚠ son las que el modelo no tenía claras, y cualquiera puede "
                 "contener errores.",
        "categories": {
            "condition": "Enfermedades y síntomas", "anatomy_or_tissue": "Anatomía y tejidos",
            "cell_or_organism": "Células y organismos", "molecule_or_pathway": "Moléculas y vías",
            "gene_or_variant": "Genes y variantes", "drug_or_intervention": "Fármacos e intervenciones",
            "measure_or_criteria": "Medidas, escalas y criterios", "method_or_statistic": "Métodos y estadística",
            "dataset_or_study": "Bases de datos, cohortes y estudios", "other": "Otros",
        },
    },
}

_GREEK = {"α": "alpha", "β": "beta", "γ": "gamma", "δ": "delta", "κ": "kappa", "ε": "epsilon"}


def term_key(term: str) -> str:
    """One key for the spelling variants of a term: 'TNF-α inhibitors' and 'TNFalpha inhibitor'
    share 'tnfalphainhibitor'; 'HLA-B*27' and 'HLA-B27' share 'hlab27'."""
    text = unicodedata.normalize("NFKC", term).casefold()
    for letter, name in _GREEK.items():
        text = text.replace(letter, name)
    words = re.split(r"[\s\-/_.,:;*'’()\[\]]+", text)
    return "".join(_singular(w) for w in words if w)


def _singular(word: str) -> str:
    return word[:-1] if len(word) > 3 and word.endswith("s") and not word.endswith("ss") else word


def language_name(language: str) -> str:
    return _LANGUAGES.get(language.lower(), language)


@dataclass
class Term:
    key: str
    forms: Counter = field(default_factory=Counter)  # spelling as written -> occurrences
    papers: dict[int, int] = field(default_factory=dict)  # paper id -> best relevance rank

    @property
    def label(self) -> str:
        return self.forms.most_common(1)[0][0]


def collect_terms(store: Store, topic_ids: list[int]) -> dict[str, Term]:
    terms: dict[str, Term] = {}
    for topic_id in topic_ids:
        for pid, result in triage_results(store, topic_id).items():
            rank = _RANK.get(result["relevance"], 0)
            if not rank:
                continue
            for raw in result.get("key_terms", []):
                form = " ".join(raw.split())
                if not (key := term_key(form)):
                    continue
                term = terms.setdefault(key, Term(key))
                term.forms[form] += 1
                term.papers[pid] = max(term.papers.get(pid, 0), rank)
    return terms


@dataclass
class DefineResult:
    defined: int = 0
    failed: int = 0
    cost: float = 0.0


class Glossary:
    def __init__(self, store: Store, topic_ids: list[int], questions: list[str], language: str = "en",
                 client: anthropic.Anthropic | None = None):
        self.store = store
        self.questions = [q for q in questions if q]
        self.language = language
        self.terms = collect_terms(store, topic_ids)
        self.papers: dict[int, StoredPaper] = {p.id: p for t in topic_ids for p in store.topic_papers(t)}
        self._client = client

    @property
    def client(self) -> anthropic.Anthropic:
        if self._client is None:
            self._client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
        return self._client

    def candidates(self, min_papers: int = 3, max_terms: int = 400) -> list[Term]:
        """The terms the glossary covers, most used first."""
        ranked = sorted((t for t in self.terms.values() if len(t.papers) >= min_papers),
                        key=lambda t: (-len(t.papers), t.label.casefold()))
        return ranked[:max_terms]

    def pending(self, candidates: list[Term]) -> list[Term]:
        done = self.store.glossary(self.language)
        return [t for t in candidates if t.key not in done]

    def examples(self, term: Term, n: int = 3) -> list[StoredPaper]:
        """The term's most relevant, then most cited, papers."""
        ids = sorted((pid for pid in term.papers if pid in self.papers),
                     key=lambda pid: (-term.papers[pid], -(self.papers[pid].cited_by or 0)))
        return [self.papers[pid] for pid in ids[:n]]

    # -- requests ------------------------------------------------------------------

    def _system(self, candidates: list[Term]) -> str:
        questions = "\n".join(f"- {q}" for q in self.questions) or "- (not stated)"
        term_list = "\n".join(f"{t.label} ({len(t.papers)})" for t in candidates)
        return _SYSTEM.format(questions=questions, term_list=term_list, language=language_name(self.language))

    def _term_text(self, term: Term) -> str:
        titles = "; ".join(f'"{clip(p.title, 150)}" ({p.year or "n.d."})' for p in self.examples(term, 2))
        return f"- {term.label}\n  used in: {titles}"

    def request_params(self, chunk: list[Term], candidates: list[Term]) -> dict[str, Any]:
        return {
            "model": MODEL,
            "max_tokens": 16000,  # thinking counts toward max_tokens
            # The same for every chunk of a run, so it is cached across requests.
            "system": [{"type": "text", "text": self._system(candidates), "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": "Terms:\n" + "\n".join(self._term_text(t) for t in chunk)}],
            "output_config": {"effort": "medium", "format": {"type": "json_schema", "schema": GLOSSARY_SCHEMA}},
        }

    def estimate_cost(self, terms: list[Term], candidates: list[Term]) -> float:
        requests = math.ceil(len(terms) / CHUNK)
        input_tokens = (requests * len(self._system(candidates)) + sum(len(self._term_text(t)) for t in terms)) / 3.5
        output_tokens = _OUTPUT_PER_TERM * len(terms) + _THINKING_PER_REQUEST * requests
        price_in, price_out = PRICES[MODEL]
        return (input_tokens * price_in + output_tokens * price_out) / 1e6

    def define(self, terms: list[Term], candidates: list[Term], workers: int = 4, log: Log = print) -> DefineResult:
        """Define `terms` in chunks, a few requests at a time. Terms whose request fails stay
        undefined and are tried again on the next run."""
        result = DefineResult()
        chunks = [terms[i:i + CHUNK] for i in range(0, len(terms), CHUNK)]
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(self.client.messages.create, **self.request_params(c, candidates)): c
                       for c in chunks}
            for future in as_completed(futures):  # results are stored from this thread only
                chunk = futures[future]
                try:
                    self._record(chunk, future.result(), result)
                except anthropic.APIError as exc:
                    log(f"glossary request failed: {exc}")
                    result.failed += len(chunk)
                self.store.commit()
        return result

    def _record(self, chunk: list[Term], message: Any, result: DefineResult) -> None:
        cost = message_cost(message.usage, MODEL)
        result.cost += cost
        text = next((b.text for b in message.content if b.type == "text"), "")
        try:
            entries = json.loads(text)["entries"] if message.stop_reason not in ("refusal", "max_tokens") else []
        except (json.JSONDecodeError, KeyError, TypeError):
            entries = []
        by_key = {t.key: t for t in chunk}
        defined = {}
        for entry in entries:
            if (key := term_key(entry.get("term", ""))) in by_key:
                defined.setdefault(key, entry)
        for key, entry in defined.items():
            self.store.save_glossary_entry(key, self.language, by_key[key].label, entry, MODEL,
                                           cost / len(defined))
        result.defined += len(defined)
        result.failed += len(chunk) - len(defined)

    # -- output --------------------------------------------------------------------

    def entries(self, candidates: list[Term]) -> list[tuple[Term, dict, list[str]]]:
        """(merged term, definition, aliases) per glossary entry. Terms the model called the
        same are merged; the most used one gives the entry its name and definition."""
        stored = self.store.glossary(self.language)
        by_key = {t.key: t for t in candidates}
        parent = {k: k for k in by_key}

        def root(k: str) -> str:
            while parent[k] != k:
                parent[k] = parent[parent[k]]
                k = parent[k]
            return k

        for key in by_key:
            other = term_key(stored.get(key, {}).get("same_as", ""))
            if other in by_key and other != key:
                parent[root(key)] = root(other)
        groups: dict[str, list[Term]] = defaultdict(list)
        for key, term in by_key.items():
            groups[root(key)].append(term)

        out = []
        for members in groups.values():
            members.sort(key=lambda t: -len(t.papers))
            head = next((t for t in members if t.key in stored), None)
            if head is None:
                continue
            merged = Term(head.key, Counter(head.forms), dict(head.papers))
            for t in members:
                for pid, rank in t.papers.items():
                    merged.papers[pid] = max(merged.papers.get(pid, 0), rank)
            aliases = sorted((t.label for t in members if t is not head), key=str.casefold)
            out.append((merged, stored[head.key], aliases))
        return out

    def write(self, path: Path, title: str, candidates: list[Term], min_papers: int) -> Path:
        labels = _LABELS.get(self.language, _LABELS["en"])
        entries = self.entries(candidates)
        by_category: dict[str, list] = defaultdict(list)
        for item in entries:
            by_category[item[1].get("category", "other")].append(item)
        all_papers = {pid for t in candidates for pid in t.papers}

        parts = [f"# {labels['title']}: {title}",
                 "*" + labels["intro"].format(date=date.today().isoformat(), model=MODEL, papers=len(all_papers),
                                              terms=len(candidates), min=min_papers) + "*"]
        for category in sorted(by_category, key=lambda c: CATEGORIES.index(c) if c in CATEGORIES else len(CATEGORIES)):
            items = sorted(by_category[category], key=lambda item: item[0].label.casefold())
            parts.append(f"## {labels['categories'].get(category, category)} ({len(items)})")
            parts.extend(self._entry(term, entry, aliases, labels, path.parent) for term, entry, aliases in items)

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n\n".join(parts) + "\n", encoding="utf-8")
        return path

    def _entry(self, term: Term, entry: dict, aliases: list[str], labels: dict, base: Path) -> str:
        heading = f"### {term.label}" + ("" if entry.get("confident", True) else f" {labels['check']}")
        facts = []
        name = entry.get("name_in_language", "").strip()
        if name and term_key(name) != term.key:
            facts.append(f"**{name}**")
        if expansion := entry.get("expansion", "").strip():
            facts.append(f"*{expansion}*")
        facts.append(labels["papers"].format(n=len(term.papers)))
        if aliases:
            facts.append(f"{labels['also']}: {', '.join(aliases)}")
        lines = [heading, " · ".join(facts), entry.get("definition", "").strip()]
        if field_note := entry.get("in_this_field", "").strip():
            lines.append(f"*{labels['field']}:* {field_note}")
        examples = []
        for p in self.examples(term):
            text = f"{clip(p.title, 70)} ({p.year or 'n.d.'}, id {p.id})"
            files = self.store.files(p.id)
            row = files.get("pdf") or files.get("xml")
            examples.append(f"[{text}]({_link(self.store.resolve(row['path']), base)})" if row else text)
        if examples:
            lines.append(f"{labels['examples']}: " + " · ".join(examples))
        return "\n".join(lines)


def run_glossary(store: Store, topic_ids: list[int], questions: list[str], out_path: Path, *, title: str,
                 language: str = "en", min_papers: int = 3, max_terms: int = 400, dry_run: bool = False,
                 max_cost: float | None = None, client: anthropic.Anthropic | None = None,
                 log: Log = print) -> float | None:
    """Define the terms not yet defined, then write the glossary. Returns the estimated cost
    of the new definitions, or None when the API key is missing."""
    glossary = Glossary(store, topic_ids, questions, language, client)
    candidates = glossary.candidates(min_papers, max_terms)
    todo = glossary.pending(candidates)
    cost = glossary.estimate_cost(todo, candidates) if todo else 0.0
    if max_cost is not None and cost > max_cost:
        keep = max(0, math.floor(len(todo) * max_cost / cost))
        log(f"Estimated ${cost:.2f} exceeds the ${max_cost:.2f} cap: defining the {keep} most used of {len(todo)} terms.")
        todo = todo[:keep]
        cost = glossary.estimate_cost(todo, candidates) if todo else 0.0
    if todo:
        log(f"glossary: {len(todo)} new terms of {len(candidates)} to define with {MODEL} in "
            f"{language_name(language)}, estimated ${cost:.2f}")
    else:
        log(f"glossary: all {len(candidates)} terms are defined")
    if dry_run:
        return cost
    if todo:
        if not client and not os.environ.get("ANTHROPIC_API_KEY"):
            log("error: ANTHROPIC_API_KEY is not set in this environment")
            return None
        result = glossary.define(todo, candidates, log=log)
        log(f"Defined {result.defined} terms for ${result.cost:.3f}"
            + (f"; {result.failed} failed and will be retried next run" if result.failed else ""))
    log(f"wrote {glossary.write(out_path, title, candidates, min_papers)}")
    return cost
