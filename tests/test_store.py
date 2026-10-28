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
