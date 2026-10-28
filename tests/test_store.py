import pytest

from scientia.models import Paper
from scientia.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    yield s
    s.close()


def test_same_paper_from_two_sources_is_merged(store):
    a, created_a = store.add_paper(Paper(title="A Study of Things", source="openalex",
                                         doi="https://doi.org/10.48550/arxiv.2101.00001",
                                         openalex_id="W1", cited_by=10))
    b, created_b = store.add_paper(Paper(title="A study of things", source="arxiv",
                                         arxiv_id="2101.00001v2", abstract="Abstract."))
    assert created_a and not created_b and a == b
    row = store.conn.execute("SELECT * FROM papers").fetchone()
    assert row["abstract"] == "Abstract." and row["cited_by"] == 10
    assert row["sources"] == '["openalex", "arxiv"]'


def test_preprint_and_published_version_merge_by_title(store):
    a, _ = store.add_paper(Paper(title="Learning operators between function spaces",
                                 source="arxiv", arxiv_id="2108.08481", year=2021))
    b, created = store.add_paper(Paper(title="Learning Operators Between Function Spaces",
                                       source="openalex", doi="10.5555/jmlr.1", year=2022))
    assert a == b and not created


def test_different_journal_dois_with_same_title_stay_separate(store):
    store.add_paper(Paper(title="A review of deep learning methods", source="openalex",
                          doi="10.1/a", year=2020))
    _, created = store.add_paper(Paper(title="A review of deep learning methods", source="openalex",
                                       doi="10.2/b", year=2020))
    assert created


def test_bridging_record_folds_two_rows_together(store):
    t = store.upsert_topic("t", "q")
    a, _ = store.add_paper(Paper(title="Alpha", source="openalex", doi="10.1/x"))
    b, _ = store.add_paper(Paper(title="Alpha preprint", source="arxiv", arxiv_id="2001.00001"))
    store.link(t, a)
    store.link(t, b)
    merged, _ = store.add_paper(Paper(title="Alpha", source="europepmc", doi="10.1/x",
                                      arxiv_id="2001.00001"))
    assert merged == a
    assert store.conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0] == 1
    [p] = store.topic_papers(t)
    assert p.doi == "10.1/x" and p.arxiv_id == "2001.00001"


def test_topic_status_and_lookup(store):
    t = store.upsert_topic("t", "q")
    pid, _ = store.add_paper(Paper(title="Beta", source="arxiv", arxiv_id="2001.00002"))
    assert store.link(t, pid) and not store.link(t, pid)
    store.set_status(t, pid, "read", note="skimmed")
    p = store.find_paper(t, "2001.00002")
    assert p.status == "read" and p.note == "skimmed"
    with pytest.raises(ValueError):
        store.set_status(t, pid, "bogus")


def test_absolute_paths_from_a_moved_library_are_repaired(tmp_path):
    old, new = tmp_path / "old-lib", tmp_path / "new-lib"
    s = Store(old / "research.db")
    pid, _ = s.add_paper(Paper(title="Gamma", source="arxiv", arxiv_id="2001.00003"))
    (old / "papers" / "arxiv_2001.00003").mkdir(parents=True)
    (old / "papers" / "arxiv_2001.00003" / "paper.pdf").write_bytes(b"%PDF-1.4")
    # Simulate a row written by an older version, which stored absolute paths.
    s.conn.execute(
        "INSERT INTO files (paper_id, kind, path, url, via, sha256, retrieved_at) VALUES (?, 'pdf', ?, 'u', 'arxiv', 'h', 't')",
        (pid, str(old / "papers" / "arxiv_2001.00003" / "paper.pdf")),
    )
    s.commit()
    s.close()
    old.rename(new)

    s = Store(new / "research.db")
    stored = s.files(pid)["pdf"]["path"]
    assert stored == "papers/arxiv_2001.00003/paper.pdf"
    assert s.resolve(stored).read_bytes() == b"%PDF-1.4"
    s.close()


def test_topic_queries_are_kept_per_label(store):
    t = store.upsert_topic("axspa", '"axial spondyloarthritis" secukinumab', label="il17")
    store.upsert_topic("axspa", "upadacitinib spondyloarthritis")
    store.add_query(t, '"axial spondyloarthritis" (secukinumab OR ixekizumab)', label="il17")  # replaces
    assert [(q["label"], q["query"]) for q in store.queries(t)] == [
        ("il17", '"axial spondyloarthritis" (secukinumab OR ixekizumab)'),
        ("upadacitinib-spondyloarthritis", "upadacitinib spondyloarthritis"),
    ]
    assert store.remove_query(t, "il17") and not store.remove_query(t, "il17")


def test_single_query_topics_from_older_versions_are_migrated(tmp_path):
    s = Store(tmp_path / "old.db")
    s.conn.execute("INSERT INTO topics (name, query, created) VALUES ('old', 'fourier neural operator', 't')")
    s.commit()
    s.close()
    s = Store(tmp_path / "old.db")
    t = s.topic_id("old")
    assert [(q["label"], q["query"]) for q in s.queries(t)] == [("main", "fourier neural operator")]
    s.close()
