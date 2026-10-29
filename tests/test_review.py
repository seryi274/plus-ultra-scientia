import json
from types import SimpleNamespace as NS

import pytest

from scientia.evidence import write_evidence
from scientia.models import Paper
from scientia.review import TRIAGE_SCHEMA, Reviewer
from scientia.store import Store

ABSTRACT = "We randomised 300 patients with active axial spondyloarthritis to secukinumab or placebo. " * 4


def message(payload, stop_reason="end_turn", model="claude-haiku-5-5"):
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return NS(content=[NS(type="thinking", thinking=""), NS(type="text", text=text)], stop_reason=stop_reason,
              model=model, usage=NS(input_tokens=1000, output_tokens=200, cache_creation_input_tokens=0,
                                    cache_read_input_tokens=0))


class FakeBatches:
    def __init__(self):
        self.created, self.status, self.outputs = [], "in_progress", {}

    def create(self, requests):
        self.created.append(list(requests))
        return NS(id=f"batch_{len(self.created)}")

    def retrieve(self, batch_id):
        return NS(processing_status=self.status, request_counts=NS(succeeded=0, errored=0))

    def results(self, batch_id):
        for custom_id, msg in self.outputs.items():
            yield NS(custom_id=custom_id, result=NS(type="succeeded", message=msg))


class FakeClient:
    def __init__(self):
        self.messages = NS(batches=FakeBatches(), create=None)


@pytest.fixture
def setup(tmp_path):
    store = Store(tmp_path / "t.db")
    topic = store.upsert_topic("axspa", "secukinumab spondyloarthritis")
    ids = []
    for i, abstract in enumerate([ABSTRACT, ABSTRACT, None]):
        pid, _ = store.add_paper(Paper(title=f"Trial {i}", source="openalex", openalex_id=f"W{i}", year=2020,
                                       abstract=abstract))
        store.link(topic, pid)
        ids.append(pid)
    client = FakeClient()
    reviewer = Reviewer(store, topic, "Which treatments work in axSpA?", "clinical", client=client)
    yield store, topic, ids, reviewer, client
    store.close()


def test_request_params_use_structured_output_and_cacheable_system(setup):
    store, topic, ids, reviewer, _ = setup
    paper = store.topic_papers(topic)[0]
    params = reviewer.request_params("triage", paper)
    assert params["model"] == "claude-haiku-5-5"
    assert params["output_config"]["format"]["schema"] == TRIAGE_SCHEMA
    assert params["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "Which treatments work in axSpA?" in params["system"][0]["text"]
    extract = reviewer.request_params("extract", paper)
    assert extract["model"] == "claude-opus-5-5"
    assert "response_predictors" in extract["output_config"]["format"]["schema"]["properties"]
    no_abstract = next(p for p in store.topic_papers(topic) if p.abstract is None)
    assert "No abstract available." in reviewer.request_params("triage", no_abstract)["messages"][0]["content"]


def test_batch_lifecycle_and_stage_eligibility(setup):
    store, topic, ids, reviewer, client = setup
    assert len(reviewer.candidates("triage")) == 3
    reviewer.submit_batch("triage", reviewer.candidates("triage"))
    assert reviewer.candidates("triage") == []  # pending papers are not resubmitted

    batches = client.messages.batches
    assert reviewer.collect().still_running  # nothing recorded while the batch runs
    batches.status = "ended"
    triage = {"reason": "r", "study_design": "randomized_controlled_trial", "key_terms": ["secukinumab"]}
    batches.outputs = {
        f"triage-{ids[0]}": message({**triage, "relevance": "central"}),
        f"triage-{ids[1]}": message({**triage, "relevance": "off_topic"}),
        f"triage-{ids[2]}": message("", stop_reason="refusal"),
    }
    collected = reviewer.collect()
    assert collected.finished == 1 and collected.recorded == {"ok": 2, "refused": 1}
    assert store.open_review_batches(topic) == []

    # Only the central paper with an abstract goes on to extraction.
    assert [p.id for p in reviewer.candidates("extract")] == [ids[0]]
    rows = store.reviews(topic, "triage")
    assert rows[ids[0]]["cost_usd"] == pytest.approx((1000 * 0.10 + 200 * 0.50) / 1e6 / 2)


def test_unparseable_or_truncated_results_are_retried(setup):
    store, topic, ids, reviewer, _ = setup
    assert reviewer._record("triage", ids[0], message("not json"), batch=False) == "error"
    assert reviewer._record("triage", ids[1], message("{", stop_reason="max_tokens"), batch=False) == "truncated"
    assert {p.id for p in reviewer.candidates("triage")} == set(ids)  # both are eligible again


def test_evidence_report_groups_by_drug_class(setup, tmp_path):
    store, topic, ids, reviewer, _ = setup
    store.save_review(topic, ids[0], "triage", "ok", "m", result=json.dumps(
        {"relevance": "central", "reason": "", "study_design": "randomized_controlled_trial", "key_terms": []}))
    store.save_review(topic, ids[0], "extract", "ok", "m", result=json.dumps({
        "population": "active axSpA", "sample_size": "300",
        "interventions": [{"name": "secukinumab", "drug_class": "IL-17 inhibitor", "regimen": "150 mg"}],
        "comparator": "placebo", "outcomes": [], "follow_up": "16 weeks", "main_finding": "ASAS40 improved",
        "response_predictors": [{"factor": "CRP", "association": "better_response", "detail": "elevated CRP"}],
        "limitations": [], "open_questions": ["Long-term radiographic effect?"],
    }))
    md, csv_path = write_evidence(store, topic, "axspa", "clinical", "Q?", tmp_path / "out")
    text = md.read_text(encoding="utf-8")
    assert "### IL-17 inhibitor (1)" in text and "ASAS40 improved" in text
    assert "| CRP | better response |" in text
    assert "Long-term radiographic effect?" in text
    assert "secukinumab" in csv_path.read_text(encoding="utf-8")
