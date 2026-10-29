"""The full paper list of one or more topics, with relevance and full-text access status,
as a CSV that opens directly in Excel (UTF-8 with BOM).

Access categories:
  downloaded (PDF)            a PDF is in the library
  downloaded (XML only)       Europe PMC full-text XML, but no PDF
  free copy, download failed  a legal free copy is known (OpenAlex open-access status, or a
                              link some route found) but every download attempt failed
  free copy, OpenAlex cache   the only copy found is OpenAlex's cached PDF ($0.01, --allow-paid)
  licence needed              no free copy is known: subscription or UNED library access
  not attempted yet           `scientia fetch` has not tried this paper
"""

from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path

from scientia.store import Store

OPEN_STATUSES = {"gold", "green", "hybrid", "bronze", "diamond"}
_DOWNLOAD_VIAS = {"arxiv", "oa-link", "unpaywall", "semanticscholar", "core", "europepmc-xml"}
_REASONS = {
    "not_pdf": "link returned a web page (publisher block, login or captcha) instead of the PDF",
    "not_xml": "Europe PMC returned no full-text XML",
    "http_403": "publisher refused automated download (HTTP 403)",
    "http_404": "link broken (HTTP 404)",
    "http_429": "rate-limited by the site (HTTP 429)",
    "network_error": "connection error",
    "too_large": "file over 100 MiB",
}

ACCESS_ORDER = ("downloaded (PDF)", "downloaded (XML only)", "free copy, download failed",
                "free copy, OpenAlex cache", "licence needed", "not attempted yet")


def classify(paper, files: dict, log) -> tuple[str, str]:
    """(access category, detail) for one paper."""
    if "pdf" in files:
        return "downloaded (PDF)", f"via {files['pdf']['via']}"
    if "xml" in files:
        return "downloaded (XML only)", "Europe PMC full-text XML"
    if log is None:
        return "not attempted yet", ""
    attempts = json.loads(log["attempts"])
    failed_links = [a for a in attempts if a["via"] in _DOWNLOAD_VIAS and a["url"] and a["result"] != "ok"]
    if failed_links:
        reasons = Counter(_REASONS.get(a["result"], a["result"].replace("_", " ")) for a in failed_links)
        sites = sorted({a["via"] for a in failed_links})
        return ("free copy, download failed",
                "; ".join(reasons) + f" (tried: {', '.join(sites)})")
    if any(a["result"] == "skipped_paid" for a in attempts):
        return "free copy, OpenAlex cache", "only copy found is OpenAlex's cached PDF ($0.01 with --allow-paid)"
    if paper.oa_status in OPEN_STATUSES:
        return "free copy, download failed", f"OpenAlex marks it {paper.oa_status} open access, but no route found a downloadable copy"
    return "licence needed", f"no free copy found (OpenAlex: {paper.oa_status or 'unknown'})"


def write_inventory(store: Store, topic_names: list[str], path: Path) -> dict[tuple[str, str], int]:
    """Write the CSV; returns counts by (relevance group, access category)."""
    topics = {name: store.topic_id(name) for name in topic_names}
    papers: dict[int, object] = {}
    member: dict[int, set[str]] = {}
    for name, tid in topics.items():
        for p in store.topic_papers(tid):
            papers.setdefault(p.id, p)
            member.setdefault(p.id, set()).add(name)
    triage = {name: {pid: json.loads(r["result"]) for pid, r in store.reviews(tid, "triage").items()
                     if r["status"] == "ok"} for name, tid in topics.items()}
    logs = store.fetch_logs()
    rank = {"central": 3, "relevant": 2, "tangential": 1, "off_topic": 0}

    rows, counts = [], Counter()
    for pid, p in papers.items():
        ratings = {name: triage[name].get(pid) for name in topics}
        best = max((r for r in ratings.values() if r), key=lambda r: rank[r["relevance"]], default=None)
        relevance = best["relevance"] if best else ""
        group = ("central/relevant" if relevance in ("central", "relevant")
                 else "tangential/off-topic" if relevance else "not triaged")
        files = store.files(pid)
        access, detail = classify(p, files, logs.get(pid))
        counts[(group, access)] += 1
        file_path = files.get("pdf") or files.get("xml")
        rows.append({
            "paper_id": pid,
            "title": p.title,
            "year": p.year or "",
            "venue": p.venue or "",
            "type": p.work_type or "",
            "tags": p.tags,
            "topics": "; ".join(sorted(member[pid])),
            **{f"relevance: {name}": (ratings[name] or {}).get("relevance", "") for name in topics},
            "best relevance": relevance,
            "central/relevant": "yes" if group == "central/relevant" else "no",
            "study design": (best or {}).get("study_design", ""),
            "triage reason": (best or {}).get("reason", ""),
            "has abstract": "yes" if len(p.abstract or "") > 200 else "no",
            "open access status": p.oa_status or "",
            "access": access,
            "access detail": detail,
            "file": str(store.resolve(file_path["path"])) if file_path else "",
            "doi link": f"https://doi.org/{p.doi}" if p.doi else (p.landing_url or ""),
            "cited by": p.cited_by or 0,
        })

    rows.sort(key=lambda r: (-rank.get(r["best relevance"], -1), ACCESS_ORDER.index(r["access"]), -r["cited by"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as fh:  # BOM: Excel then reads UTF-8 correctly
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]) if rows else ["paper_id"])
        writer.writeheader()
        writer.writerows(rows)
    return dict(counts)
