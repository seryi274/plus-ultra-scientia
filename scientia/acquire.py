"""Full-text acquisition through open-access routes only.

Routes, cheapest and most reliable first:
  1. arXiv PDF                    (free; paced to 1 request / 3 s)
  2. Europe PMC JATS XML          (free; open-access subset only, the approved PMC route)
  3. Open-access PDF links        (free; from OpenAlex locations and Europe PMC)
  4. Unpaywall DOI lookup         (free; needs UNPAYWALL_EMAIL or SCIENTIA_EMAIL)
  5. Semantic Scholar             (free; openAccessPdf by DOI, PMID or exact title)
  6. CORE repository copies       (free; by DOI or exact title, often accepted manuscripts)
  7. OpenAlex cached PDF          (metered at $0.01 per file; only with --allow-paid)

Subscription content (UNED, Hespérides) is deliberately not fetched here: the library
forbids automated bulk downloading through its proxy, so papers with no open copy are
marked 'unavailable' and listed with their DOI link. PDFs you download yourself through
the library can then be filed with `scientia import` (see importer.py).

Every route tried is recorded (store.fetch_log), so a failed paper can be told apart as
"a free copy exists but could not be downloaded" or "no free copy is known".

Papers are fetched in parallel worker threads. Workers only do network and file I/O; all
database writes happen on the calling thread, as each paper completes.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
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
_BREAKER_LIMIT = 12  # consecutive service failures before a route is switched off for the run


class _Breaker:
    """Switches off a lookup route (CORE, Semantic Scholar, Unpaywall, Europe PMC XML) for the
    rest of a run once it fails many times in a row, so an outage costs seconds, not hours."""

    def __init__(self, limit: int = _BREAKER_LIMIT):
        self._limit = limit
        self._failures: dict[str, int] = {}
        self._lock = threading.Lock()
        self.disabled: list[str] = []

    def is_open(self, route: str) -> bool:
        return route in self.disabled

    def record(self, route: str, failed: bool) -> None:
        with self._lock:
            self._failures[route] = self._failures.get(route, 0) + 1 if failed else 0
            if failed and self._failures[route] >= self._limit and route not in self.disabled:
                self.disabled.append(route)


@dataclass
class Attempt:
    via: str
    url: str | None
    # ok, http_<code>, not_pdf (a web page instead of the document), too_large,
    # network_error, no_link (the lookup found nothing), lookup_failed, skipped_paid, skipped_no_key
    result: str
    detail: str = ""

    def describe(self) -> str:
        where = f" {self.url}" if self.url else ""
        return f"{self.via}: {self.result}{f' ({self.detail})' if self.detail else ''}{where}"


@dataclass
class SavedFile:
    kind: str
    path: Path
    url: str
    via: str
    license: str | None
    sha256: str


@dataclass
class Outcome:
    pdf: Path | None = None
    xml: Path | None = None
    tries: list[Attempt] = field(default_factory=list)
    saved: list[SavedFile] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.pdf is not None or self.xml is not None

    @property
    def attempts(self) -> list[str]:
        return [t.describe() for t in self.tries]


class Acquirer:
    def __init__(self, config: Config, store: Store, http: HttpClient, allow_paid: bool = False):
        self._config = config
        self._store = store
        self._http = http
        self._openalex = OpenAlex(http, config.openalex_api_key)
        self._s2 = SemanticScholar(http, config.s2_api_key)
        self._core = Core(http, config.core_api_key)
        self._allow_paid = allow_paid
        self._s2_known: dict[int, tuple[str, str | None] | None] = {}  # filled by prefetch()
        self._breaker = _Breaker()

    @property
    def disabled_routes(self) -> list[str]:
        return list(self._breaker.disabled)

    def _skip(self, route: str, outcome: Outcome) -> bool:
        if self._breaker.is_open(route):
            outcome.tries.append(Attempt(route, None, "skipped_disabled", "switched off after repeated failures"))
            return True
        return False

    def prefetch(self, papers: list[StoredPaper]) -> int:
        """Look up Semantic Scholar's open-access links for many papers at once (500 per
        call) instead of one call per paper during fetch. Returns how many have a link."""
        refs = {p.id: f"DOI:{p.doi}" if p.doi else f"PMID:{p.pmid}" for p in papers if p.doi or p.pmid}
        found = self._s2.open_pdfs_batch(list(refs.values()))
        if found is not None:  # on a failed call, fall back to per-paper lookups
            self._s2_known.update(zip(refs, found))
        return sum(1 for v in self._s2_known.values() if v)

    def fetch(self, paper: StoredPaper) -> Outcome:
        return self.fetch_many([paper], workers=1)[0]

    def fetch_many(self, papers: list[StoredPaper], workers: int = 6,
                   on_done: Callable[[StoredPaper, Outcome], None] | None = None) -> list[Outcome]:
        """Fetch papers in parallel. `on_done` runs on this thread after each paper's
        results are stored, in completion order."""
        existing = {p.id: {kind: self._store.resolve(row["path"]) for kind, row in self._store.files(p.id).items()}
                    for p in papers}
        outcomes: dict[int, Outcome] = {}
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futures = {pool.submit(self._fetch_files, p, existing[p.id]): p for p in papers}
            for future in as_completed(futures):
                paper = futures[future]
                outcome = future.result()
                self._record(paper, outcome)
                outcomes[paper.id] = outcome
                if on_done:
                    on_done(paper, outcome)
        return [outcomes[p.id] for p in papers]

    # -- worker side: network and files only, no database ------------------------------

    def _fetch_files(self, paper: StoredPaper, existing: dict[str, Path]) -> Outcome:
        outcome = Outcome()
        folder = self._config.papers_dir / paper_slug(paper)

        if "xml" in existing:
            outcome.xml = existing["xml"]
        elif paper.pmcid and paper.is_oa and not self._skip("europepmc-xml", outcome):
            url = EuropePMC.fulltext_xml_url(paper.pmcid)
            data = self._download(url, outcome, "europepmc-xml", expect=b"<")
            last = outcome.tries[-1].result
            self._breaker.record("europepmc-xml", last.startswith("http_5") or last == "network_error")
            if data:
                outcome.xml = self._save(outcome, folder / "paper.xml", data, "xml", url, "europepmc")

        if "pdf" in existing:
            outcome.pdf = existing["pdf"]
        else:
            for url, via, license in self._pdf_candidates(paper, outcome):
                if data := self._download(url, outcome, via, expect=b"%PDF"):
                    outcome.pdf = self._save(outcome, folder / "paper.pdf", data, "pdf", url, via, license)
                    break
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
        if paper.doi and self._config.unpaywall_email and not self._skip("unpaywall", outcome):
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
                outcome.tries.append(Attempt("openalex-content", None, "skipped_paid", "costs $0.01; --allow-paid"))
            elif url := self._openalex.content_url(paper.openalex_id):
                yield url, "openalex-content", paper.license
            else:
                outcome.tries.append(Attempt("openalex-content", None, "skipped_no_key", "needs OPENALEX_API_KEY"))

    def _lookup_routes(self, paper: StoredPaper, outcome: Outcome):
        """Semantic Scholar, then CORE. Each is one or two API calls, so they run only
        when the free direct routes have failed."""
        found = None
        if paper.id in self._s2_known:
            found = self._s2_known[paper.id]
        elif not self._skip("semanticscholar", outcome):
            try:
                found = self._s2.open_pdf(doi=paper.doi, pmid=paper.pmid, title=paper.title)
                self._breaker.record("semanticscholar", failed=False)
            except Exception as exc:
                outcome.tries.append(Attempt("semanticscholar", None, "lookup_failed", str(exc)))
                self._breaker.record("semanticscholar", failed=True)
        if found:
            yield found[0], "semanticscholar", found[1]
        elif not outcome.tries or outcome.tries[-1].via != "semanticscholar":
            outcome.tries.append(Attempt("semanticscholar", None, "no_link"))
        if self._skip("core", outcome):
            return
        try:
            urls = self._core.open_pdfs(doi=paper.doi, title=paper.title)
            self._breaker.record("core", failed=False)
        except Exception as exc:
            outcome.tries.append(Attempt("core", None, "lookup_failed", str(exc)))
            self._breaker.record("core", failed=True)
            return
        if not urls:
            outcome.tries.append(Attempt("core", None, "no_link"))
        for url in urls:
            yield url, "core", None

    def _unpaywall(self, doi: str, outcome: Outcome) -> list[tuple[str, str | None]]:
        try:
            response = self._http.get(f"{_UNPAYWALL}/{doi}", params={"email": self._config.unpaywall_email},
                                      retries=1)
        except Exception as exc:
            outcome.tries.append(Attempt("unpaywall", None, "lookup_failed", type(exc).__name__))
            self._breaker.record("unpaywall", failed=True)
            return []
        # 404 means Unpaywall doesn't know the DOI: an answer, not a service failure.
        self._breaker.record("unpaywall", failed=response.status_code >= 500)
        if response.status_code != 200:
            outcome.tries.append(Attempt("unpaywall", None, "lookup_failed", f"HTTP {response.status_code}"))
            return []
        found = [
            (loc["url_for_pdf"], loc.get("license"))
            for loc in response.json().get("oa_locations") or []
            if loc.get("url_for_pdf")
        ]
        if not found:
            outcome.tries.append(Attempt("unpaywall", None, "no_link"))
        return found

    def _download(self, url: str, outcome: Outcome, via: str, expect: bytes) -> bytes | None:
        label = re.sub(r"api_key=[^&]+", "api_key=***", url)
        try:
            response = self._http.get(url, headers=self._core.headers_for(url))
        except Exception as exc:  # network errors on one route must not stop the others
            outcome.tries.append(Attempt(via, label, "network_error", type(exc).__name__))
            return None
        if response.status_code != 200:
            outcome.tries.append(Attempt(via, label, f"http_{response.status_code}"))
            return None
        data = response.content
        if len(data) > _MAX_BYTES:
            outcome.tries.append(Attempt(via, label, "too_large", f"{len(data) // 2**20} MiB"))
            return None
        if not data.lstrip()[:5].startswith(expect):
            # Typically an HTML landing page, login wall or captcha instead of the document.
            outcome.tries.append(Attempt(via, label, "not_pdf" if expect == b"%PDF" else "not_xml"))
            return None
        outcome.tries.append(Attempt(via, label, "ok", f"{len(data) // 1024} KiB"))
        return data

    def _save(self, outcome: Outcome, path: Path, data: bytes, kind: str, url: str, via: str,
              license: str | None = None) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        outcome.saved.append(SavedFile(kind, path, re.sub(r"[?&]api_key=[^&]+", "", url), via, license,
                                       hashlib.sha256(data).hexdigest()))
        return path

    # -- calling thread: database ----------------------------------------------------

    def _record(self, paper: StoredPaper, outcome: Outcome) -> None:
        for f in outcome.saved:
            self._store.add_file(paper.id, f.kind, f.path, f.url, f.via, f.license, f.sha256)
        if outcome.tries:  # nothing tried means the files were already there
            self._store.save_fetch_log(paper.id, outcome.ok, [asdict(t) for t in outcome.tries])
        if outcome.saved:
            write_metadata(self._store, paper, self._config.papers_dir / paper_slug(paper))


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
