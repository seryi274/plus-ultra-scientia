"""File PDFs you downloaded yourself (e.g. through the UNED library) into the library.

Each PDF is matched to a stored paper using, in order of reliability:
  1. a DOI in the PDF's metadata (Info dictionary or XMP) or in its file name,
  2. a DOI or arXiv identifier printed on the first two pages,
  3. the title of a paper still lacking a PDF appearing on the first page.
With --topic, a DOI that is not in the library yet is looked up in OpenAlex (a free
single-record call) and the paper is added to that topic.
"""

from __future__ import annotations

import hashlib
import logging
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from pypdf import PdfReader

from scientia.acquire import paper_slug, write_metadata
from scientia.config import Config
from scientia.models import normalize_arxiv_id, normalize_doi, title_key
from scientia.sources.openalex import OpenAlex
from scientia.store import Store

logging.getLogger("pypdf").setLevel(logging.ERROR)  # malformed-PDF warnings are noise here

_DOI = re.compile(r"\b(10\.\d{4,9}/[^\s\"'<>{}\[\]]+)", re.IGNORECASE)
_DOI_IN_FILENAME = re.compile(r"\b(10\.\d{4,9})[_/](\S+)$", re.IGNORECASE)
_ARXIV = re.compile(r"arXiv:\s*(\d{4}\.\d{4,5}|[a-z\-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?", re.IGNORECASE)
_ARXIV_FILENAME = re.compile(r"^(\d{4}\.\d{4,5})(?:v\d+)?$")
_MIN_TITLE_WORDS = 4


@dataclass
class Evidence:
    # (doi, where it was found), most trustworthy first
    dois: list[tuple[str, str]] = field(default_factory=list)
    arxiv_ids: list[str] = field(default_factory=list)
    first_page: str = ""  # title_key-normalised text of page 1
    meta_title: str | None = None


@dataclass
class ImportResult:
    path: Path
    status: str  # imported | added | already-have | duplicate | unmatched | not-pdf
    detail: str
    paper_id: int | None = None


def read_evidence(path: Path) -> Evidence:
    ev = Evidence()

    def add_doi(raw: str, origin: str) -> None:
        doi = normalize_doi(raw.rstrip(".,;:)]>"))
        if doi and doi not in [d for d, _ in ev.dois]:
            ev.dois.append((doi, origin))

    stem = path.stem
    if m := _DOI_IN_FILENAME.search(stem):
        add_doi(f"{m.group(1)}/{m.group(2)}", "file name")
    if m := _ARXIV_FILENAME.match(stem):
        ev.arxiv_ids.append(m.group(1))

    reader = PdfReader(path)
    info = reader.metadata or {}
    for key in ("/doi", "/DOI", "/Subject", "/Keywords", "/WPS-ARTICLEDOI"):
        if value := info.get(key):
            for raw in _DOI.findall(str(value)):
                add_doi(raw, "metadata")
    try:
        if reader.xmp_metadata is not None:
            xmp = reader.xmp_metadata.stream.get_data().decode("utf-8", "replace")
            for raw in _DOI.findall(xmp):
                add_doi(raw, "metadata")
    except Exception:
        pass  # broken XMP is common and never fatal
    if title := info.get("/Title"):
        ev.meta_title = str(title)

    for number, page in enumerate(reader.pages[:2], 1):
        try:
            text = page.extract_text() or ""
        except Exception:
            continue
        for raw in _DOI.findall(text):
            add_doi(raw, f"page {number}")
        ev.arxiv_ids += [a for a in map(normalize_arxiv_id, _ARXIV.findall(text)) if a and a not in ev.arxiv_ids]
        if number == 1:
            ev.first_page = title_key(text[:3000])
    return ev


class Importer:
    def __init__(self, config: Config, store: Store, openalex: OpenAlex | None = None):
        self._config = config
        self._store = store
        self._openalex = openalex

    def import_pdf(self, path: Path, topic_id: int | None = None, move: bool = False,
                   force: bool = False) -> ImportResult:
        data = path.read_bytes()
        if not data[:1024].lstrip().startswith(b"%PDF"):
            return ImportResult(path, "not-pdf", "file does not start with a PDF header")
        sha = hashlib.sha256(data).hexdigest()
        if (existing := self._store.file_with_hash(sha)) and not force:
            return ImportResult(path, "duplicate", f"identical file already filed at {self._store.resolve(existing['path'])}",
                                existing["paper_id"])

        try:
            ev = read_evidence(path)
        except Exception as exc:
            return ImportResult(path, "unmatched", f"could not read PDF ({type(exc).__name__})")

        status = "imported"
        paper_id, how = self._match(ev)
        if paper_id is None and topic_id is not None:
            paper_id, how = self._add_from_openalex(ev, topic_id)
            status = "added"
        if paper_id is None:
            found = ", ".join(d for d, _ in ev.dois[:3]) or "no DOI found"
            return ImportResult(path, "unmatched", f"no matching paper ({found})")

        if topic_id is not None:
            self._store.link(topic_id, paper_id)
        if "pdf" in self._store.files(paper_id) and not force:
            return ImportResult(path, "already-have", f"paper {paper_id} already has a PDF (use --force)",
                                paper_id)

        paper = self._store.get_paper(paper_id)
        folder = self._config.papers_dir / paper_slug(paper)
        folder.mkdir(parents=True, exist_ok=True)
        dest = folder / "paper.pdf"
        source_uri = path.resolve().as_uri()
        if move:
            shutil.move(path, dest)
        else:
            shutil.copyfile(path, dest)
        self._store.add_file(paper_id, "pdf", dest, source_uri, "manual-import", None, sha)
        self._store.mark_downloaded(paper_id)
        write_metadata(self._store, paper, folder)
        return ImportResult(path, status, how, paper_id)

    def _match(self, ev: Evidence) -> tuple[int | None, str]:
        for doi, origin in ev.dois:
            if pid := self._store.paper_by("doi", doi):
                return pid, f"DOI {doi} ({origin})"
        for arxiv_id in ev.arxiv_ids:
            if pid := self._store.paper_by("arxiv_id", arxiv_id):
                return pid, f"arXiv {arxiv_id}"
        meta_key = title_key(ev.meta_title or "")
        best: tuple[int, int] | None = None  # (title length, paper id)
        for row in self._store.papers_without_pdf():
            key = row["title_key"]
            if len(key.split()) < _MIN_TITLE_WORDS:
                continue
            if key == meta_key or (ev.first_page and key in ev.first_page):
                if best is None or len(key) > best[0]:
                    best = (len(key), row["id"])
        if best:
            return best[1], "title on first page"
        return None, ""

    def _add_from_openalex(self, ev: Evidence, topic_id: int) -> tuple[int | None, str]:
        if not self._openalex:
            return None, ""
        for doi, origin in ev.dois[:3]:
            paper = self._openalex.get_by_doi(doi)
            if not paper:
                continue
            # A DOI printed in the text may be a citation; trust it only if the title is there too.
            if origin in ("metadata", "file name") or title_key(paper.title) in ev.first_page:
                paper_id, _ = self._store.add_paper(paper)
                self._store.link(topic_id, paper_id)
                return paper_id, f"new paper from OpenAlex via DOI {doi} ({origin})"
        return None, ""


def pdf_files(paths: list[Path]) -> list[Path]:
    files: list[Path] = []
    for path in paths:
        if path.is_dir():
            files += sorted(p for p in path.rglob("*") if p.is_file() and p.suffix.lower() == ".pdf")
        elif path.is_file():
            files.append(path)
    return files
