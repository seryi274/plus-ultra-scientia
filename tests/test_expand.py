import pytest

from scientia.expand import expand
from scientia.models import Paper
from scientia.store import Store


def work(oid, title, refs=(), doi=None, work_type="article", year=2024):
    return Paper(title=title, source="openalex", openalex_id=oid, doi=doi, year=year,
                 work_type=work_type, references=list(refs))


class FakeOpenAlex:
    """Stands in for the OpenAlex client: a fixed set of works and who cites whom."""
    cost_usd = 0.0

    def __init__(self, works):
        self.works = {w.openalex_id: w for w in works}

    def resolve(self, dois=(), pmids=()):
        return [w for w in self.works.values() if w.doi in dois or (w.pmid and w.pmid in pmids)]

    def get_many(self, ids):
        return [self.works[i] for i in ids if i in self.works]

    def citing(self, openalex_id, limit=100, since=None):
        return [w for w in self.works.values() if openalex_id in (w.references or [])]


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    yield s
    s.close()


def test_backward_and_forward_expansion(store):
    seeds = [
        work("W1", "Seed one", refs=["WF", "WG"]),
        work("W2", "Seed two", refs=["WF", "WH"]),
        work("W3", "Seed three", refs=["WF", "WG"], doi="10.48550/arxiv.2001.00003"),
    ]
    others = [
        work("WF", "Foundation cited by all three seeds", year=1995),
        work("WG", "Foundation cited by two seeds", year=2000),
        work("WH", "Cited by one seed only", year=2001),
        work("WN1", "New paper citing two seeds", refs=["W1", "W2"]),
        work("WN2", "New paper citing one seed", refs=["W1"]),
        work("WE", "Editorial citing two seeds", refs=["W1", "W2"], work_type="editorial"),
    ]
    openalex = FakeOpenAlex(seeds + others)

    topic = store.upsert_topic("t", "q")
    for w in seeds[:2]:
        pid, _ = store.add_paper(w)
        store.link(topic, pid)
    # The third seed came from arXiv, so expansion must first find its OpenAlex record.
    pid, _ = store.add_paper(Paper(title="Seed three", source="arxiv", arxiv_id="2001.00003", year=2024))
    store.link(topic, pid)

    result = expand(store, openalex, topic, seeds=3, min_links=2, skip_types=["editorial"])

    assert (result.resolved, result.unresolved, result.seeds) == (1, 0, 3)
    titles = {p.title: p for p in store.topic_papers(topic)}
    assert titles["Foundation cited by all three seeds"].note == "backward: cited by 3 of 3 seeds"
    assert "Foundation cited by two seeds" in titles
    assert titles["New paper citing two seeds"].note == "forward: cites 2 topic papers"
    for absent in ("Cited by one seed only", "New paper citing one seed", "Editorial citing two seeds"):
        assert absent not in titles

    # Running again finds nothing new at the same thresholds for these candidates.
    again = expand(store, openalex, topic, seeds=3, min_links=2, skip_types=["editorial"])
    assert not again.added["backward"] and not again.added["forward"]


def test_topic_links_count_each_paper_once_across_openalex_versions(store):
    topic = store.upsert_topic("t", "q")
    # One paper that OpenAlex holds as two works (e.g. preprint and journal version).
    a, _ = store.add_paper(work("WA1", "Paper A", doi="10.1/a"))
    a2, _ = store.add_paper(work("WA2", "Paper A", doi="10.1/a"))
    assert a == a2
    b, _ = store.add_paper(work("WB", "Paper B", refs=["WA1"]))
    c, _ = store.add_paper(work("WC", "Paper C", refs=["WA2"]))
    d, _ = store.add_paper(work("WD", "Paper D", refs=["WA1", "WA2"]))
    for pid in (a, b, c, d):
        store.link(topic, pid)

    links = store.topic_links(topic)
    assert links[a] == (3, 0)  # B, C and D, with D counted once
    assert links[d] == (0, 1)


def test_retraction_and_type_are_stored_and_tagged(store):
    topic = store.upsert_topic("t", "q")
    pid, _ = store.add_paper(work("WR", "A retracted review", work_type="review"))
    store.link(topic, pid)
    store.add_paper(Paper(title="A retracted review", source="openalex", openalex_id="WR", is_retracted=True))
    [p] = store.topic_papers(topic)
    assert p.tags == "[RETRACTED] [review]"
