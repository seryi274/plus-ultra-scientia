"""Evidence reports built from a topic's LLM reviews: a Markdown overview for reading and a
CSV with one row per extracted paper for analysis. Everything in them comes from abstracts,
as extracted by the model, and should be checked against the paper before it is relied on.
"""

from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

from scientia.store import Store


def write_evidence(store: Store, topic_id: int, topic_name: str, profile: str, question: str,
                   out_dir: Path) -> tuple[Path, Path]:
    papers = {p.id: p for p in store.topic_papers(topic_id)}
    triage = {pid: json.loads(r["result"]) for pid, r in store.reviews(topic_id, "triage").items()
              if r["status"] == "ok" and pid in papers}
    extracts = {pid: json.loads(r["result"]) for pid, r in store.reviews(topic_id, "extract").items()
                if r["status"] == "ok" and pid in papers}
    spent = store.conn.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM reviews WHERE topic_id = ?",
                               (topic_id,)).fetchone()[0]

    def ref(pid: int) -> str:
        p = papers[pid]
        title = p.title if len(p.title) <= 90 else p.title[:87] + "…"
        return f"{title} ({p.year or 'n.d.'}, id {pid})"

    md = [f"# Evidence: {topic_name}",
          f"*Generated {date.today().isoformat()} from abstracts by LLM review; verify against the papers.*",
          f"**Question:** {question}",
          "## Triage",
          f"{len(triage)} of {len(papers)} papers triaged, {len(extracts)} extracted. "
          f"Review cost so far: ${spent:.2f}.",
          _counts_table("Relevance", Counter(t["relevance"] for t in triage.values())),
          _counts_table("Study design", Counter(t["study_design"] for t in triage.values()
                                                if t["relevance"] in ("central", "relevant")))]

    if profile == "clinical":
        md += _clinical_sections(extracts, triage, ref)
    elif profile == "genetics":
        md += _genetics_sections(extracts, triage, ref)
    else:
        md += _general_sections(extracts, ref)

    open_questions = [(pid, q) for pid, e in extracts.items() for q in e.get("open_questions", [])]
    if open_questions:
        md.append(f"## Open questions stated in abstracts ({len(open_questions)})")
        md.append("\n".join(f"- {q} — *{ref(pid)}*" for pid, q in open_questions))

    out_dir.mkdir(parents=True, exist_ok=True)
    md_path, csv_path = out_dir / "evidence.md", out_dir / "evidence.csv"
    md_path.write_text("\n\n".join(md) + "\n", encoding="utf-8")
    _write_csv(csv_path, papers, triage, extracts)
    return md_path, csv_path


def _clinical_sections(extracts: dict, triage: dict, ref) -> list[str]:
    by_class: dict[str, list[int]] = defaultdict(list)
    for pid, e in extracts.items():
        classes = {i["drug_class"].strip() for i in e.get("interventions", []) if i.get("drug_class", "").strip()}
        for c in classes or {"(no intervention)"}:
            by_class[c].append(pid)

    sections = ["## Evidence by drug class"]
    for drug_class, pids in sorted(by_class.items(), key=lambda kv: -len(kv[1])):
        rows = [f"### {drug_class} ({len(pids)})",
                "| Paper | Design | Population | n | Comparator | Main finding |",
                "|---|---|---|---|---|---|"]
        for pid in sorted(pids, key=lambda p: -_relevance_rank(triage.get(p))):
            e = extracts[pid]
            rows.append("| " + " | ".join(_cell(x) for x in (
                ref(pid), triage.get(pid, {}).get("study_design", ""), e["population"], e["sample_size"],
                e["comparator"], e["main_finding"])) + " |")
        sections.append("\n".join(rows))

    predictors = [(pid, p) for pid, e in extracts.items() for p in e.get("response_predictors", [])]
    if predictors:
        rows = [f"## Reported predictors of treatment response ({len(predictors)})",
                "| Factor | Association | Detail | Paper |", "|---|---|---|---|"]
        for pid, p in sorted(predictors, key=lambda t: t[1]["factor"].lower()):
            rows.append("| " + " | ".join(_cell(x) for x in (p["factor"], p["association"].replace("_", " "),
                                                              p["detail"], ref(pid))) + " |")
        sections.append("\n".join(rows))
    return sections


def _genetics_sections(extracts: dict, triage: dict, ref) -> list[str]:
    genes = Counter(g.strip() for e in extracts.values() for g in e.get("genes_variants", []) if g.strip())
    pathways = Counter(x.strip() for e in extracts.values() for x in e.get("pathways", []) if x.strip())
    sections = [_counts_table("Most studied genes / variants", Counter(dict(genes.most_common(25)))),
                _counts_table("Most studied pathways", Counter(dict(pathways.most_common(15))))]

    rows = ["## Key findings", "| Paper | Data types | Genes / variants | Main finding |", "|---|---|---|---|"]
    for pid in sorted(extracts, key=lambda p: -_relevance_rank(triage.get(p))):
        e = extracts[pid]
        rows.append("| " + " | ".join(_cell(x) for x in (
            ref(pid), ", ".join(e.get("data_types", [])), ", ".join(e.get("genes_variants", [])[:6]),
            e["main_finding"])) + " |")
    sections.append("\n".join(rows))

    datasets = [(pid, d) for pid, e in extracts.items() for d in e.get("public_datasets", [])]
    if datasets:
        sections.append(f"## Public datasets mentioned ({len(datasets)})\n" + "\n".join(
            f"- **{d['name']}** {d['identifier']} — *{ref(pid)}*" for pid, d in datasets))
    implications = [(pid, t) for pid, e in extracts.items() for t in e.get("therapeutic_implications", [])]
    if implications:
        sections.append(f"## Therapeutic implications stated ({len(implications)})\n" + "\n".join(
            f"- {t} — *{ref(pid)}*" for pid, t in implications))
    return sections


def _general_sections(extracts: dict, ref) -> list[str]:
    rows = ["## Main findings", "| Paper | Methods | Main finding |", "|---|---|---|"]
    for pid, e in extracts.items():
        rows.append("| " + " | ".join(_cell(x) for x in (ref(pid), e.get("methods", ""), e.get("main_finding", ""))) + " |")
    return ["\n".join(rows)]


def _write_csv(path: Path, papers: dict, triage: dict, extracts: dict) -> None:
    fields = sorted({k for e in extracts.values() for k in e})
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["paper_id", "title", "year", "doi", "relevance", "study_design", "key_terms", *fields])
        for pid in sorted(triage):
            p, t, e = papers[pid], triage[pid], extracts.get(pid, {})
            writer.writerow([pid, p.title, p.year, p.doi, t["relevance"], t["study_design"],
                             "; ".join(t["key_terms"]),
                             *(v if isinstance(v := e.get(f, ""), str) else json.dumps(v, ensure_ascii=False)
                               for f in fields)])


def _counts_table(title: str, counts: Counter) -> str:
    if not counts:
        return f"**{title}:** none yet"
    rows = [f"**{title}**", "", "| | n |", "|---|---|"]
    rows += [f"| {_cell(k.replace('_', ' '))} | {n} |" for k, n in counts.most_common()]
    return "\n".join(rows)


def _relevance_rank(t: dict | None) -> int:
    return {"central": 3, "relevant": 2, "tangential": 1}.get((t or {}).get("relevance"), 0)


def _cell(value) -> str:
    return " ".join(str(value or "").split()).replace("|", "\\|")
