"""Back up the library to another folder, e.g. one synced by Google Drive or OneDrive.

The database is copied with SQLite's online backup API, so the copy is consistent even if
the library is in use. Papers and topic outputs are copied incrementally (new or changed
files only), and nothing is ever deleted at the destination, so a mistake in the library
cannot propagate to the backup.

Work from the library, not from the synced copy: a sync client rewriting research.db
while SQLite writes to it can corrupt the database.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from scientia.config import Config
from scientia.store import Store

_FOLDERS = ("papers", "topics", "inbox")


@dataclass
class BackupResult:
    copied: int = 0
    unchanged: int = 0
    bytes_copied: int = 0


def backup_library(config: Config, store: Store, dest: Path) -> BackupResult:
    dest = dest.resolve()
    if dest == config.home or config.home in dest.parents:
        raise ValueError("the backup folder must be outside the library")
    dest.mkdir(parents=True, exist_ok=True)
    result = BackupResult()

    store.commit()
    tmp = dest / "research.db.partial"
    target = sqlite3.connect(tmp)
    try:
        store.conn.backup(target)
    finally:
        target.close()
    os.replace(tmp, dest / "research.db")
    result.copied += 1
    result.bytes_copied += (dest / "research.db").stat().st_size

    for folder in _FOLDERS:
        src_root = config.home / folder
        if not src_root.exists():
            continue
        for src in src_root.rglob("*"):
            if not src.is_file():
                continue
            dst = dest / folder / src.relative_to(src_root)
            stat = src.stat()
            if dst.exists():
                d = dst.stat()
                if d.st_size == stat.st_size and d.st_mtime >= stat.st_mtime - 1:
                    result.unchanged += 1
                    continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            result.copied += 1
            result.bytes_copied += stat.st_size

    papers = store.conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
    files = store.conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    (dest / "BACKUP.txt").write_text(
        f"scientia library backup\n"
        f"made:     {datetime.now(timezone.utc).isoformat(timespec='seconds')}\n"
        f"from:     {config.home}\n"
        f"contents: {len(store.topics())} topics, {papers} papers, {files} full-text files\n\n"
        "To restore, copy this folder to a local disk and point SCIENTIA_HOME at the copy\n"
        "(or copy it over the library). Don't use the synced folder itself as the live library.\n",
        encoding="utf-8",
    )
    return result
