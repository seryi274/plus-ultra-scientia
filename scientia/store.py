"""SQLite persistence. The database, not the agent, is the source of truth: every paper
is stored once (deduplicated across sources by DOI / arXiv id / PMID / PMCID / OpenAlex id,
falling back to title + year), and each topic keeps its own scores and reading status.

File paths are stored relative to the library root (the database's folder), so the whole
library can be moved or synced to another machine without breaking them."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from scientia.models import Paper, title_key

ID_COLUMNS = ("doi", "arxiv_id", "pmid", "pmcid", "openalex_id")

# Lifecycle of a paper within a topic.
STATUSES = ("new", "downloaded", "unavailable", "read", "skipped")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS papers (
    id               INTEGER PRIMARY KEY,
    title            TEXT NOT NULL,
    title_key        TEXT NOT NULL,
    abstract         TEXT,
    authors          TEXT NOT NULL DEFAULT '[]',
    year             INTEGER,
    published        TEXT,
    venue            TEXT,
    doi              TEXT UNIQUE,
    arxiv_id         TEXT UNIQUE,
    pmid             TEXT UNIQUE,
    pmcid            TEXT UNIQUE,
    openalex_id      TEXT UNIQUE,
    cited_by         INTEGER,
    is_oa            INTEGER,
    license          TEXT,
    pdf_urls         TEXT NOT NULL DEFAULT '[]',
    openalex_has_pdf INTEGER NOT NULL DEFAULT 0,
    landing_url      TEXT,
    sources          TEXT NOT NULL DEFAULT '[]',
    first_seen       TEXT NOT NULL,
    updated          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS papers_title_key ON papers(title_key);

CREATE TABLE IF NOT EXISTS topics (
    id       INTEGER PRIMARY KEY,
    name     TEXT NOT NULL UNIQUE,
    query    TEXT NOT NULL,
    created  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS searches (
    id        INTEGER PRIMARY KEY,
    topic_id  INTEGER NOT NULL REFERENCES topics(id),
    source    TEXT NOT NULL,
    query     TEXT NOT NULL,
    params    TEXT NOT NULL,
    results   INTEGER NOT NULL,
    error     TEXT,
    ran_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS topic_papers (
    topic_id    INTEGER NOT NULL REFERENCES topics(id),
    paper_id    INTEGER NOT NULL REFERENCES papers(id),
    score       REAL NOT NULL DEFAULT 0,
    status      TEXT NOT NULL DEFAULT 'new',
    first_seen  TEXT NOT NULL,
    note        TEXT,
    PRIMARY KEY (topic_id, paper_id)
);

CREATE TABLE IF NOT EXISTS files (
    id            INTEGER PRIMARY KEY,
    paper_id      INTEGER NOT NULL REFERENCES papers(id),
    kind          TEXT NOT NULL,          -- 'pdf' or 'xml'
    path          TEXT NOT NULL,
    url           TEXT NOT NULL,
    via           TEXT NOT NULL,          -- which route supplied it
    license       TEXT,
    sha256        TEXT NOT NULL,
    retrieved_at  TEXT NOT NULL,
    UNIQUE (paper_id, kind)
);

-- OpenAlex sometimes holds one paper as several works (preprint, conference and journal
-- versions), each with its own citations. Every OpenAlex id seen for a paper maps here.
CREATE TABLE IF NOT EXISTS openalex_aliases (
    openalex_id  TEXT PRIMARY KEY,
    paper_id     INTEGER NOT NULL REFERENCES papers(id)
);
CREATE INDEX IF NOT EXISTS openalex_aliases_paper ON openalex_aliases(paper_id);

-- Citation edges between OpenAlex works, kept even when the cited work is not in the
-- library: they are what citation expansion counts.
CREATE TABLE IF NOT EXISTS citations (
    citing  TEXT NOT NULL,
    cited   TEXT NOT NULL,
    PRIMARY KEY (citing, cited)
);
CREATE INDEX IF NOT EXISTS citations_cited ON citations(cited);
"""

# Columns added after the first release, applied to existing databases on open.
_ADDED_COLUMNS = {"papers": [("work_type", "TEXT"), ("is_retracted", "INTEGER")]}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _journal_doi(doi: str | None) -> bool:
    """A DOI that is not one of arXiv's own preprint DOIs."""
    return bool(doi) and not doi.startswith("10.48550/")


@dataclass
class StoredPaper:
    """A papers row joined with its per-topic state."""
    row: sqlite3.Row

    def __getattr__(self, name: str) -> Any:
        try:
            value = self.row[name]
        except IndexError:
            raise AttributeError(name) from None
        if name in ("authors", "pdf_urls", "sources"):
            return json.loads(value)
        return value

    @property
    def tags(self) -> str:
        """Short markers for listings: retraction and non-research work types."""
        tags = []
        if self.row["is_retracted"]:
            tags.append("[RETRACTED]")
        if self.row["work_type"] in ("review", "editorial", "letter", "erratum", "retraction"):
            tags.append(f"[{self.row['work_type']}]")
        return " ".join(tags)


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.home = path.parent.resolve()
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(_SCHEMA)
        self._add_missing_columns()
        self.conn.execute(
            "INSERT OR IGNORE INTO openalex_aliases (openalex_id, paper_id)"
            " SELECT openalex_id, id FROM papers WHERE openalex_id IS NOT NULL"
        )
        self.conn.commit()
        self._relativize_file_paths()

    def _add_missing_columns(self) -> None:
        for table, columns in _ADDED_COLUMNS.items():
            existing = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            for name, decl in columns:
                if name not in existing:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    def close(self) -> None:
        self.conn.close()

    # -- topics -----------------------------------------------------------------

    def upsert_topic(self, name: str, query: str) -> int:
        row = self.conn.execute("SELECT id FROM topics WHERE name = ?", (name,)).fetchone()
        if row:
            self.conn.execute("UPDATE topics SET query = ? WHERE id = ?", (query, row["id"]))
            return row["id"]
        cur = self.conn.execute(
            "INSERT INTO topics (name, query, created) VALUES (?, ?, ?)", (name, query, _now())
        )
        return cur.lastrowid

    def topic_id(self, name: str) -> int:
        row = self.conn.execute("SELECT id FROM topics WHERE name = ?", (name,)).fetchone()
        if not row:
            raise KeyError(f"unknown topic {name!r}; create it with `scientia search`")
        return row["id"]

    def topics(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT t.name, t.query, t.created, COUNT(tp.paper_id) AS papers,
                      SUM(tp.status = 'downloaded') AS downloaded
               FROM topics t LEFT JOIN topic_papers tp ON tp.topic_id = t.id
               GROUP BY t.id ORDER BY t.name"""
        ).fetchall()

    def log_search(self, topic_id: int, source: str, query: str, params: dict, results: int,
                   error: str | None = None) -> None:
        self.conn.execute(
            "INSERT INTO searches (topic_id, source, query, params, results, error, ran_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (topic_id, source, query, json.dumps(params), results, error, _now()),
        )

    # -- papers -----------------------------------------------------------------

    def add_paper(self, paper: Paper) -> tuple[int, bool]:
        """Insert or merge a paper. Returns (paper_id, created)."""
        matches = self._find_matches(paper)
        if not matches:
            paper_id, created = self._insert(paper), True
        else:
            paper_id, *duplicates = sorted(matches)
            for dup in duplicates:
                self._absorb(paper_id, dup)
            self._merge_into(paper_id, paper)
            created = False
        if paper.openalex_id:
            self.conn.execute(
                "INSERT OR REPLACE INTO openalex_aliases (openalex_id, paper_id) VALUES (?, ?)",
                (paper.openalex_id, paper_id),
            )
            if paper.references is not None:
                self.set_references(paper.openalex_id, paper.references)
        return paper_id, created

    def _find_matches(self, paper: Paper) -> set[int]:
        ids = set()
        for column, value in paper.identifiers.items():
            row = self.conn.execute(f"SELECT id FROM papers WHERE {column} = ?", (value,)).fetchone()
            if row:
                ids.add(row["id"])
        if paper.openalex_id:
            row = self.conn.execute(
                "SELECT paper_id FROM openalex_aliases WHERE openalex_id = ?", (paper.openalex_id,)
            ).fetchone()
            if row:
                ids.add(row["paper_id"])
        key = title_key(paper.title)
        if not ids and paper.year and len(key.split()) >= 4:
            # Same title within a year either way: usually preprint vs. published version.
            # Two different journal DOIs mean two different works that share a title.
            for row in self.conn.execute(
                "SELECT id, year, doi FROM papers WHERE title_key = ?", (key,)
            ):
                if row["year"] is not None and abs(row["year"] - paper.year) > 1:
                    continue
                if _journal_doi(row["doi"]) and _journal_doi(paper.doi) and row["doi"] != paper.doi:
                    continue
                ids.add(row["id"])
        return ids

    def _insert(self, p: Paper) -> int:
        now = _now()
        cur = self.conn.execute(
            """INSERT INTO papers (title, title_key, abstract, authors, year, published, venue,
                   doi, arxiv_id, pmid, pmcid, openalex_id, cited_by, is_oa, license, pdf_urls,
                   openalex_has_pdf, landing_url, sources, first_seen, updated, work_type, is_retracted)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (p.title, title_key(p.title), p.abstract, json.dumps(p.authors), p.year, p.published,
             p.venue, p.doi, p.arxiv_id, p.pmid, p.pmcid, p.openalex_id, p.cited_by,
             None if p.is_oa is None else int(p.is_oa), p.license, json.dumps(p.pdf_urls),
             int(p.openalex_has_pdf), p.landing_url, json.dumps([p.source]), now, now,
             p.work_type, None if p.is_retracted is None else int(p.is_retracted)),
        )
        return cur.lastrowid

    def _merge_into(self, paper_id: int, p: Paper) -> None:
        """Fill gaps in the stored record from a new sighting of the same paper."""
        row = self.conn.execute("SELECT * FROM papers WHERE id = ?", (paper_id,)).fetchone()
        updates: dict[str, Any] = {}
        for column in (*ID_COLUMNS, "abstract", "year", "published", "venue", "license", "landing_url",
                       "work_type"):
            value = getattr(p, column)
            if row[column] is None and value is not None:
                updates[column] = value
        if not json.loads(row["authors"]) and p.authors:
            updates["authors"] = json.dumps(p.authors)
        if p.cited_by is not None and (row["cited_by"] or 0) < p.cited_by:
            updates["cited_by"] = p.cited_by
        if p.is_oa and not row["is_oa"]:
            updates["is_oa"] = 1
        if p.openalex_has_pdf and not row["openalex_has_pdf"]:
            updates["openalex_has_pdf"] = 1
        if p.is_retracted is not None and (p.is_retracted or row["is_retracted"] is None):
            updates["is_retracted"] = int(p.is_retracted)  # a retraction is sticky
        urls = json.loads(row["pdf_urls"])
        if new := [u for u in p.pdf_urls if u not in urls]:
            updates["pdf_urls"] = json.dumps(urls + new)
        sources = json.loads(row["sources"])
        if p.source not in sources:
            updates["sources"] = json.dumps(sources + [p.source])
        # An identifier may already belong to a different row only if _find_matches missed
        # it, which it cannot; but guard anyway so a bad record never aborts a whole run.
        for column in ID_COLUMNS:
            if column in updates and self.conn.execute(
                f"SELECT 1 FROM papers WHERE {column} = ? AND id != ?", (updates[column], paper_id)
            ).fetchone():
                del updates[column]
        if updates:
            updates["updated"] = _now()
            assignments = ", ".join(f"{c} = ?" for c in updates)
            self.conn.execute(f"UPDATE papers SET {assignments} WHERE id = ?", (*updates.values(), paper_id))

    def _absorb(self, keep: int, dup: int) -> None:
        """Two stored rows turned out to be the same paper: fold `dup` into `keep`."""
        dup_row = self.conn.execute("SELECT * FROM papers WHERE id = ?", (dup,)).fetchone()
        self.conn.execute(
            """INSERT OR IGNORE INTO topic_papers (topic_id, paper_id, score, status, first_seen, note)
               SELECT topic_id, ?, score, status, first_seen, note FROM topic_papers WHERE paper_id = ?""",
            (keep, dup),
        )
        self.conn.execute("DELETE FROM topic_papers WHERE paper_id = ?", (dup,))
        self.conn.execute("UPDATE OR IGNORE files SET paper_id = ? WHERE paper_id = ?", (keep, dup))
        self.conn.execute("DELETE FROM files WHERE paper_id = ?", (dup,))
        self.conn.execute("UPDATE openalex_aliases SET paper_id = ? WHERE paper_id = ?", (keep, dup))
        self.conn.execute("DELETE FROM papers WHERE id = ?", (dup,))
        sources = json.loads(dup_row["sources"])
        self._merge_into(keep, Paper(
            title=dup_row["title"], source=sources[0] if sources else "merged",
            abstract=dup_row["abstract"], authors=json.loads(dup_row["authors"]),
            year=dup_row["year"], published=dup_row["published"], venue=dup_row["venue"],
            doi=dup_row["doi"], arxiv_id=dup_row["arxiv_id"], pmid=dup_row["pmid"],
            pmcid=dup_row["pmcid"], openalex_id=dup_row["openalex_id"],
            cited_by=dup_row["cited_by"], is_oa=bool(dup_row["is_oa"]), license=dup_row["license"],
            pdf_urls=json.loads(dup_row["pdf_urls"]),
            openalex_has_pdf=bool(dup_row["openalex_has_pdf"]), landing_url=dup_row["landing_url"],
            work_type=dup_row["work_type"],
            is_retracted=None if dup_row["is_retracted"] is None else bool(dup_row["is_retracted"]),
        ))

    def link(self, topic_id: int, paper_id: int) -> bool:
        """Attach a paper to a topic. Returns True if it is new to the topic."""
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO topic_papers (topic_id, paper_id, first_seen) VALUES (?, ?, ?)",
            (topic_id, paper_id, _now()),
        )
        return cur.rowcount == 1

    def set_score(self, topic_id: int, paper_id: int, score: float) -> None:
        self.conn.execute(
            "UPDATE topic_papers SET score = ? WHERE topic_id = ? AND paper_id = ?",
            (score, topic_id, paper_id),
        )

    def set_status(self, topic_id: int, paper_id: int, status: str, note: str | None = None) -> None:
        if status not in STATUSES:
            raise ValueError(f"status must be one of {STATUSES}")
        self.conn.execute(
            "UPDATE topic_papers SET status = ?, note = COALESCE(?, note) WHERE topic_id = ? AND paper_id = ?",
            (status, note, topic_id, paper_id),
        )

    def topic_papers(self, topic_id: int, status: str | None = None,
                     limit: int | None = None) -> list[StoredPaper]:
        sql = """SELECT p.*, tp.score, tp.status, tp.note, tp.first_seen AS topic_first_seen
                 FROM topic_papers tp JOIN papers p ON p.id = tp.paper_id
                 WHERE tp.topic_id = ?"""
        args: list[Any] = [topic_id]
        if status:
            sql += " AND tp.status = ?"
            args.append(status)
        sql += " ORDER BY tp.score DESC, p.cited_by DESC"
        if limit:
            sql += " LIMIT ?"
            args.append(limit)
        return [StoredPaper(r) for r in self.conn.execute(sql, args)]

    def find_paper(self, topic_id: int, ref: str) -> StoredPaper | None:
        """Look a paper up by its row id or any identifier."""
        for column in ("id", *ID_COLUMNS):
            row = self.conn.execute(
                f"""SELECT p.*, tp.score, tp.status, tp.note, tp.first_seen AS topic_first_seen
                    FROM topic_papers tp JOIN papers p ON p.id = tp.paper_id
                    WHERE tp.topic_id = ? AND p.{column} = ?""",
                (topic_id, ref.lower() if column == "doi" else ref),
            ).fetchone()
            if row:
                return StoredPaper(row)
        return None

    def get_paper(self, paper_id: int) -> StoredPaper:
        return StoredPaper(self.conn.execute("SELECT * FROM papers WHERE id = ?", (paper_id,)).fetchone())

    def paper_by(self, column: str, value: str) -> int | None:
        """Row id of the paper with this identifier, in any topic."""
        if column not in ID_COLUMNS:
            raise ValueError(column)
        row = self.conn.execute(f"SELECT id FROM papers WHERE {column} = ?", (value,)).fetchone()
        return row["id"] if row else None

    def papers_without_pdf(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT id, title, title_key FROM papers p WHERE NOT EXISTS
               (SELECT 1 FROM files f WHERE f.paper_id = p.id AND f.kind = 'pdf')"""
        ).fetchall()

    def mark_downloaded(self, paper_id: int) -> int:
        """Mark the paper downloaded in every topic still waiting for it."""
        cur = self.conn.execute(
            """UPDATE topic_papers SET status = 'downloaded', note = NULL
               WHERE paper_id = ? AND status IN ('new', 'unavailable')""",
            (paper_id,),
        )
        return cur.rowcount

    def set_note(self, topic_id: int, paper_id: int, note: str) -> None:
        self.conn.execute(
            "UPDATE topic_papers SET note = ? WHERE topic_id = ? AND paper_id = ?", (note, topic_id, paper_id)
        )

    # -- citations ----------------------------------------------------------------

    def set_references(self, citing: str, cited: list[str]) -> None:
        """Replace the stored reference list of one OpenAlex work."""
        self.conn.execute("DELETE FROM citations WHERE citing = ?", (citing,))
        self.conn.executemany(
            "INSERT OR IGNORE INTO citations (citing, cited) VALUES (?, ?)", [(citing, c) for c in cited]
        )

    def references_of(self, openalex_ids: list[str]) -> dict[str, list[str]]:
        refs: dict[str, list[str]] = {i: [] for i in openalex_ids}
        for chunk in _chunks(openalex_ids, 500):
            marks = ",".join("?" * len(chunk))
            for row in self.conn.execute(f"SELECT citing, cited FROM citations WHERE citing IN ({marks})", chunk):
                refs[row["citing"]].append(row["cited"])
        return refs

    def aliases(self, openalex_ids: list[str] | None = None, topic_id: int | None = None) -> dict[str, int]:
        """OpenAlex id -> paper id, for the given ids, or for every paper in a topic."""
        if topic_id is not None:
            return {r["openalex_id"]: r["paper_id"] for r in self.conn.execute(
                """SELECT a.openalex_id, a.paper_id FROM openalex_aliases a
                   JOIN topic_papers tp ON tp.paper_id = a.paper_id WHERE tp.topic_id = ?""", (topic_id,))}
        found: dict[str, int] = {}
        for chunk in _chunks(openalex_ids or [], 500):
            marks = ",".join("?" * len(chunk))
            for r in self.conn.execute(
                f"SELECT openalex_id, paper_id FROM openalex_aliases WHERE openalex_id IN ({marks})", chunk
            ):
                found[r["openalex_id"]] = r["paper_id"]
        return found

    def topic_links(self, topic_id: int) -> dict[int, tuple[int, int]]:
        """paper id -> (topic papers citing it, topic papers it cites), counting each
        paper once however many OpenAlex versions it has."""
        sql = """WITH t AS (SELECT a.openalex_id, a.paper_id FROM openalex_aliases a
                           JOIN topic_papers tp ON tp.paper_id = a.paper_id WHERE tp.topic_id = ?)
                 SELECT {group}.paper_id AS paper_id, COUNT(DISTINCT {other}.paper_id) AS n
                 FROM citations c JOIN t cited ON cited.openalex_id = c.cited
                                  JOIN t citing ON citing.openalex_id = c.citing
                 WHERE cited.paper_id != citing.paper_id
                 GROUP BY {group}.paper_id"""
        cited_by = {r["paper_id"]: r["n"]
                    for r in self.conn.execute(sql.format(group="cited", other="citing"), (topic_id,))}
        cites = {r["paper_id"]: r["n"]
                 for r in self.conn.execute(sql.format(group="citing", other="cited"), (topic_id,))}
        return {pid: (cited_by.get(pid, 0), cites.get(pid, 0)) for pid in cited_by.keys() | cites.keys()}

    # -- files ------------------------------------------------------------------

    def resolve(self, stored_path: str) -> Path:
        """Absolute location of a path from the files table."""
        path = Path(stored_path)
        return path if path.is_absolute() else self.home / path

    def _to_stored(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.home).as_posix()
        except ValueError:
            return str(path)  # outside the library: keep it absolute

    def _relativize_file_paths(self) -> None:
        """Rewrite absolute paths left by older versions, including ones pointing at the
        library's previous location (re-rooted at their 'papers' folder)."""
        changed = False
        for row in self.conn.execute("SELECT id, path FROM files").fetchall():
            path = Path(row["path"])
            if not path.is_absolute():
                continue
            stored = self._to_stored(path)
            if Path(stored).is_absolute() and "papers" in path.parts:
                i = len(path.parts) - 1 - path.parts[::-1].index("papers")
                candidate = Path(*path.parts[i:])
                if (self.home / candidate).exists():
                    stored = candidate.as_posix()
            if stored != row["path"]:
                self.conn.execute("UPDATE files SET path = ? WHERE id = ?", (stored, row["id"]))
                changed = True
        if changed:
            self.conn.commit()

    def add_file(self, paper_id: int, kind: str, path: Path, url: str, via: str,
                 license: str | None, sha256: str) -> None:
        self.conn.execute(
            """INSERT INTO files (paper_id, kind, path, url, via, license, sha256, retrieved_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (paper_id, kind) DO UPDATE SET path = excluded.path, url = excluded.url,
                   via = excluded.via, license = excluded.license, sha256 = excluded.sha256,
                   retrieved_at = excluded.retrieved_at""",
            (paper_id, kind, self._to_stored(path), url, via, license, sha256, _now()),
        )

    def file_with_hash(self, sha256: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM files WHERE sha256 = ?", (sha256,)).fetchone()

    def files(self, paper_id: int) -> dict[str, sqlite3.Row]:
        return {r["kind"]: r for r in self.conn.execute("SELECT * FROM files WHERE paper_id = ?", (paper_id,))}

    def commit(self) -> None:
        self.conn.commit()
