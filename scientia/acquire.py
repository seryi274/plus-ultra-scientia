"""Full-text acquisition through open-access routes only.

Routes, cheapest and most reliable first:
  1. arXiv PDF                    (free; paced to 1 request / 3 s)
  2. Europe PMC JATS XML          (free; open-access subset only, the approved PMC route)
  3. Open-access PDF links        (free; from OpenAlex locations and Europe PMC)
  4. Unpaywall DOI lookup         (free; needs SCIENTIA_EMAIL)
  5. Semantic Scholar             (free; openAccessPdf by DOI, PMID or exact title)
  6. CORE repository copies       (free; by DOI or exact title, often accepted manuscripts)
  7. OpenAlex cached PDF          (metered at $0.01 per file; only with --allow-paid)

Subscription content (UNED, Hespérides) is deliberately not fetched here: the library
forbids automated bulk downloading through its proxy, so papers with no open copy are
marked 'unavailable' and listed with their DOI link. PDFs you download yourself through
the library can then be filed with `scientia import` (see importer.py).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from scientia.config import Config
from scientia.net import HttpClient
from scientia.sources.core import Core
from scientia.sources.europepmc import EuropePMC
from scientia.sources.openalex import OpenAlex
from scientia.sources.semanticscholar import SemanticScholar
from scientia.store import Store, StoredPaper

_MAX_BYTES = 100 * 1024 * 1024
_UNPAYWALL = "https://api.unpaywall.org/v2"


@dataclass
class Outcome:
    pdf: Path | None = None
    xml: Path | None = None
    attempts: list[str] = field(default_factory=list)  # human-readable log of what was tried

    @property
    def ok(self) -> bool:
        return self.pdf is not None or self.xml is not None


class Acquirer:
    def __init__(self, config: Config, store: Store, http: HttpClient, allow_paid: bool = False):
        self._config = config
        self._store = store
        self._http = http
        self._openalex = OpenAlex(http, config.openalex_api_key)
        self._s2 = SemanticScholar(http, config.s2_api_key)
        self._core = Core(http, config.core_api_key)
        self._allow_paid = allow_paid

    def fetch(self, paper: StoredPaper) -> Outcome:
        outcome = Outcome()
        folder = self._config.papers_dir / paper_slug(paper)
        existing = self._store.files(paper.id)

        if "xml" in existing:
            outcome.xml = self._store.resolve(existing["xml"]["path"])
        elif paper.pmcid and paper.is_oa:
            url = EuropePMC.fulltext_xml_url(paper.pmcid)
            if data := self._download(url, outcome, "europepmc-xml", expect=b"<"):
                outcome.xml = self._save(paper, folder / "paper.xml", data, "xml", url, "europepmc")

        if "pdf" in existing:
            outcome.pdf = self._store.resolve(existing["pdf"]["path"])
        else:
            for url, via, license in self._pdf_candidates(paper, outcome):
                if data := self._download(url, outcome, via, expect=b"%PDF"):
                    outcome.pdf = self._save(paper, folder / "paper.pdf", data, "pdf", url, via, license)
                    break

        if outcome.ok:
            write_metadata(self._store, paper, folder)
        return outcome

    def _pdf_candidates(self, paper: StoredPaper, outcome: Outcome):
        """Yield (url, via, license) lazily, so later (slower or paid) routes only run if needed."""
        tried: set[str] = set()
        if paper.arxiv_id:
            url = f"https://arxiv.org/pdf/{paper.arxiv_id}"
            tried.add(url)
            yield url, "arxiv", None
        for url in paper.pdf_urls:
            if url not in tried and "arxiv.org/" not in url:
                tried.add(url)
                yield url, "oa-link", paper.license
        if paper.doi and self._config.email:
            for url, license in self._unpaywall(paper.doi, outcome):
                if url not in tried:
                    tried.add(url)
                    yield url, "unpaywall", license
        for url, via, license in self._lookup_routes(paper, outcome):
            if url not in tried:
                tried.add(url)
                yield url, via, license
        if paper.openalex_has_pdf and paper.openalex_id:
            if not self._allow_paid:
                outcome.attempts.append("openalex-content: skipped (costs $0.01; pass --allow-paid)")
            elif url := self._openalex.content_url(paper.openalex_id):
                yield url, "openalex-content", paper.license
            else:
                outcome.attempts.append("openalex-content: skipped (needs OPENALEX_API_KEY)")

    def _lookup_routes(self, paper: StoredPaper, outcome: Outcome):
        """Semantic Scholar, then CORE. Each is one or two API calls, so they run only
        when the free direct routes have failed."""
        try:
            if found := self._s2.open_pdf(doi=paper.doi, pmid=paper.pmid, title=paper.title):
                yield found[0], "semanticscholar", found[1]
            else:
                outcome.attempts.append("semanticscholar: no open-access PDF listed")
        except Exception as exc:
            outcome.attempts.append(f"semanticscholar: {exc}")
        try:
            urls = self._core.open_pdfs(doi=paper.doi, title=paper.title)
            if not urls:
                outcome.attempts.append("core: no repository copy found")
            for url in urls:
                yield url, "core", None
        except Exception as exc:
            outcome.attempts.append(f"core: {exc}")

    def _unpaywall(self, doi: str, outcome: Outcome) -> list[tuple[str, str | None]]:
        response = self._http.get(f"{_UNPAYWALL}/{doi}", params={"email": self._config.email})
        if response.status_code != 200:
            outcome.attempts.append(f"unpaywall: HTTP {response.status_code}")
            return []
        return [
            (loc["url_for_pdf"], loc.get("license"))
            for loc in response.json().get("oa_locations") or []
            if loc.get("url_for_pdf")
        ]

    def _download(self, url: str, outcome: Outcome, via: str, expect: bytes) -> bytes | None:
        label = re.sub(r"api_key=[^&]+", "api_key=***", url)
        try:
            response = self._http.get(url, headers=self._core.headers_for(url))
        except Exception as exc:  # network errors on one route must not stop the others
            outcome.attempts.append(f"{via}: {type(exc).__name__} for {label}")
            return None
        if response.status_code != 200:
            outcome.attempts.append(f"{via}: HTTP {response.status_code} for {label}")
            return None
        data = response.content
        if len(data) > _MAX_BYTES:
            outcome.attempts.append(f"{via}: file too large ({len(data)} bytes) at {label}")
            return None
        if not data.lstrip()[:5].startswith(expect):
            # Typically an HTML landing page, login wall or captcha instead of the document.
            outcome.attempts.append(f"{via}: not a {expect.decode()!r} document at {label}")
            return None
        outcome.attempts.append(f"{via}: ok ({len(data) // 1024} KiB) from {label}")
        return data

    def _save(self, paper: StoredPaper, path: Path, data: bytes, kind: str, url: str, via: str,
              license: str | None = None) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        stored_url = re.sub(r"[?&]api_key=[^&]+", "", url)
        self._store.add_file(paper.id, kind, path, stored_url, via, license,
                             hashlib.sha256(data).hexdigest())
        return path


def write_metadata(store: Store, paper: StoredPaper, folder: Path) -> None:
    meta = {k: getattr(paper, k) for k in (
        "title", "authors", "year", "published", "venue", "doi", "arxiv_id", "pmid",
        "pmcid", "openalex_id", "cited_by", "is_oa", "license", "landing_url", "abstract",
    )}
    meta["files"] = {
        kind: {k: row[k] for k in ("path", "url", "via", "license", "sha256", "retrieved_at")}
        for kind, row in store.files(paper.id).items()
    }
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "metadata.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), "utf-8")


def paper_slug(paper: StoredPaper) -> str:
    for prefix, value in (("doi", paper.doi), ("arxiv", paper.arxiv_id),
                          ("openalex", paper.openalex_id), ("pmid", paper.pmid),
                          ("pmc", paper.pmcid)):
        if value:
            return f"{prefix}_" + re.sub(r"[^A-Za-z0-9.\-]+", "_", value).strip("_")
    return f"paper_{paper.id}"
