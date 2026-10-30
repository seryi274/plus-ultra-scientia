import sqlite3

import pytest

from scientia.backup import backup_library
from scientia.config import Config
from scientia.models import Paper
from scientia.store import Store


@pytest.fixture
def library(tmp_path):
    config = Config(home=tmp_path / "lib", openalex_api_key=None, email=None)
    store = Store(config.db_path)
    store.add_paper(Paper(title="A paper", source="arxiv", arxiv_id="2001.00001"))
    (config.papers_dir / "arxiv_2001.00001").mkdir(parents=True)
    (config.papers_dir / "arxiv_2001.00001" / "paper.pdf").write_bytes(b"%PDF-1.4 one")
    yield config, store
    store.close()


def test_backup_is_consistent_incremental_and_never_deletes(library, tmp_path):
    config, store = library
    dest = tmp_path / "drive" / "backup"

    first = backup_library(config, store, dest)
    assert first.copied == 2 and first.unchanged == 0  # database + one PDF
    copy = sqlite3.connect(dest / "research.db")
    assert copy.execute("SELECT title FROM papers").fetchone()[0] == "A paper"
    copy.close()
    assert "1 papers" in (dest / "BACKUP.txt").read_text(encoding="utf-8")

    second = backup_library(config, store, dest)
    assert second.copied == 1 and second.unchanged == 1  # only the database again

    (config.papers_dir / "arxiv_2001.00001" / "paper.pdf").unlink()
    backup_library(config, store, dest)
    assert (dest / "papers" / "arxiv_2001.00001" / "paper.pdf").exists()


def test_backup_refuses_a_folder_inside_the_library(library):
    config, store = library
    with pytest.raises(ValueError, match="outside the library"):
        backup_library(config, store, config.home / "backup")
