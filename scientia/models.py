"""The normalized paper record shared by every source, plus identifier helpers."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

_DOI_PREFIX = re.compile(r"^(https?://(dx\.)?doi\.org/|doi:)", re.IGNORECASE)
_ARXIV_DOI = re.compile(r"^10\.48550/arxiv\.(.+)$", re.IGNORECASE)
_ARXIV_URL = re.compile(r"arxiv\.org/(?:abs|pdf)/([^?#\s]+?)(?:\.pdf)?(?:[?#]|$)", re.IGNORECASE)
_ARXIV_VERSION = re.compile(r"v\d+$")


def normalize_doi(value: str | None) -> str | None:
    if not value:
        return None
    doi = _DOI_PREFIX.sub("", value.strip()).lower()
    return doi or None


def normalize_arxiv_id(value: str | None) -> str | None:
    """'arXiv:2101.00001v3' / 'http://arxiv.org/abs/math/9906169v1' -> version-less id."""
    if not value:
        return None
    value = value.strip()
    if m := _ARXIV_URL.search(value):
        value = m.group(1)
    value = re.sub(r"^arxiv:", "", value, flags=re.IGNORECASE)
    return _ARXIV_VERSION.sub("", value) or None


def arxiv_id_from_doi(doi: str | None) -> str | None:
    """arXiv mints DOIs of the form 10.48550/arXiv.<id>; recognising them links a
    DataCite record to the arXiv record of the same preprint."""
    if doi and (m := _ARXIV_DOI.match(doi)):
        return normalize_arxiv_id(m.group(1))
    return None


def arxiv_id_from_url(url: str | None) -> str | None:
    if url and (m := _ARXIV_URL.search(url)):
        return normalize_arxiv_id(m.group(1))
    return None


def normalize_pmcid(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip().upper()
    if value.isdigit():
        value = "PMC" + value
    return value if value.startswith("PMC") else None


def title_key(title: str) -> str:
    """Accent-, case- and punctuation-insensitive title, used as a last-resort dedup key."""
    text = unicodedata.normalize("NFKD", title)
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


@dataclass
class Paper:
    title: str
    source: str  # the API this record came from
    abstract: str | None = None
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    published: str | None = None  # ISO date, as precise as the source gives it
    venue: str | None = None
    doi: str | None = None
    arxiv_id: str | None = None
    pmid: str | None = None
    pmcid: str | None = None
    openalex_id: str | None = None
    cited_by: int | None = None
    is_oa: bool | None = None
    license: str | None = None
    # Candidate open-access PDF links, best first. Not guaranteed to serve a PDF:
    # publisher links often answer with an HTML landing page instead.
    pdf_urls: list[str] = field(default_factory=list)
    # OpenAlex holds a cached copy (downloadable through its metered content API).
    openalex_has_pdf: bool = False
    landing_url: str | None = None
    work_type: str | None = None  # OpenAlex type: article, review, preprint, book-chapter...
    is_retracted: bool | None = None
    # OpenAlex ids of the works this one cites; None when the source doesn't say.
    references: list[str] | None = None

    def __post_init__(self) -> None:
        self.doi = normalize_doi(self.doi)
        self.arxiv_id = normalize_arxiv_id(self.arxiv_id) or arxiv_id_from_doi(self.doi)
        self.pmcid = normalize_pmcid(self.pmcid)
        self.title = " ".join(self.title.split())

    @property
    def identifiers(self) -> dict[str, str]:
        ids = {
            "doi": self.doi,
            "arxiv_id": self.arxiv_id,
            "pmid": self.pmid,
            "pmcid": self.pmcid,
            "openalex_id": self.openalex_id,
        }
        return {k: v for k, v in ids.items() if v}
