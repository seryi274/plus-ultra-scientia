import json

import pytest

from scientia.acquire import _Breaker
from scientia.pipeline import SKIPPED_TYPES, expand_params, expanded_before
from scientia.project import ProjectError, load_project, sync_project
from scientia.review import Reviewer
from scientia.store import Store

PROJECT = '''
name = "demo"
max_review_cost = 5.0

[vars]
AX = '("ankylosing spondylitis" OR "axial spondyloarthritis")'

[defaults]
sources = ["openalex"]
limit = 100

[[topics]]
name = "demo-treatment"
question = "Which treatments work?"
profile = "clinical"
extract_hints = "Name outcome measures such as ASAS40."

[topics.queries]
il17 = '${AX} AND (secukinumab OR ixekizumab)'

[[topics.expand]]
direction = "both"

[[topics.expand]]
direction = "forward"
since = "2018-01-01"
skip_types = ["review"]

[[topics]]
name = "demo-custom"
question = "What methods are used?"
profile = "schemas/methods.json"

[topics.queries]
main = "operator learning"
'''

SCHEMA = {"type": "object", "properties": {"method": {"type": "string"}, "main_finding": {"type": "string"}},
          "required": ["method", "main_finding"], "additionalProperties": False}


@pytest.fixture
def project_dir(tmp_path):
    (tmp_path / "schemas").mkdir()
    (tmp_path / "schemas" / "methods.json").write_text(json.dumps(SCHEMA), encoding="utf-8")
    (tmp_path / "project.toml").write_text(PROJECT, encoding="utf-8")
    return tmp_path


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "lib" / "t.db")
    yield s
    s.close()


def test_load_project(project_dir):
    p = load_project(project_dir)
    treatment, custom = p.topics
    assert (p.name, p.max_review_cost, p.outputs) == ("demo", 5.0, project_dir.resolve() / "outputs")
    assert treatment.queries["il17"].startswith('("ankylosing spondylitis" OR')
    assert treatment.sources == ["openalex"] and treatment.limit == 100
    assert treatment.hints == {"extract": "Name outcome measures such as ASAS40."}
    assert treatment.expand[1] == expand_params(direction="forward", since="2018-01-01", skip_types=["review"])
    assert (custom.profile, custom.schema) == ("methods", SCHEMA)


@pytest.mark.parametrize("bad, message", [
    ('[[topics]]\nname = "x"\n[topics.queries]\nq = "${NOPE}"', "unknown variable"),
    ('[[topics]]\nname = "x"\nprofile = "astrology"', "profile must be"),
    ('[[topics]]\nname = "x"\nsources = ["google"]', "unknown source"),
    ('[[topics]]\nname = "x"\n[[topics.expand]]\nseed = 3', "unknown setting"),
    ('name = "empty"', "declares no"),
])
def test_project_errors(tmp_path, bad, message):
    (tmp_path / "project.toml").write_text(bad, encoding="utf-8")
    with pytest.raises(ProjectError, match=message):
        load_project(tmp_path)


def test_sync_adds_updates_and_reports_queries(project_dir, store):
    logs = []
    project = load_project(project_dir)
    sync_project(project, store, logs.append)
    t = store.topic_id("demo-treatment")
    assert [q["label"] for q in store.queries(t)] == ["il17"]
    settings = store.review_settings(t)
    assert settings["profile"] == "clinical" and settings["hints"]["extract"].startswith("Name outcome")
    assert store.review_settings(store.topic_id("demo-custom"))["schema"] == SCHEMA

    store.add_query(t, "something old", "legacy")
    project.topics[0].queries["il17"] = "secukinumab"
    logs.clear()
    sync_project(project, store, logs.append)
    assert "[demo-treatment] updated query 'il17'" in logs
    assert any("legacy" in line and "not the project file" in line for line in logs)
    sync_project(project, store, logs.append)  # nothing changes on a second sync
    assert logs.count("[demo-treatment] updated query 'il17'") == 1


def test_expansions_already_run_are_recognised(store):
    t = store.upsert_topic("t")
    params = expand_params(direction="forward", since="2018-01-01", skip_types=["review"])
    # Logged by an older version: same settings, defaults listed in another order.
    logged = {**params, "skip_types": ["review", *reversed(SKIPPED_TYPES)]}
    store.log_search(t, "openalex-expand", "citation expansion", logged, 10)
    assert expanded_before(store, t, params)
    assert not expanded_before(store, t, expand_params(direction="both"))


def test_project_hints_reach_the_prompts(project_dir, store):
    sync_project(load_project(project_dir), store, lambda _: None)
    t = store.topic_id("demo-treatment")
    s = store.review_settings(t)
    reviewer = Reviewer(store, t, s["question"], s["profile"], schema=s["schema"], hints=s["hints"])
    assert "ASAS40" in reviewer._system("extract") and "ASAS40" not in reviewer._system("triage")
    custom = store.topic_id("demo-custom")
    s = store.review_settings(custom)
    reviewer = Reviewer(store, custom, s["question"], s["profile"], schema=s["schema"], hints=s["hints"])
    assert reviewer.schema == SCHEMA


def test_breaker_switches_a_route_off_after_consecutive_failures():
    b = _Breaker(limit=3)
    for failed in (True, True, False, True, True):  # a success resets the count
        b.record("core", failed)
    assert not b.is_open("core")
    b.record("core", True)
    assert b.is_open("core") and b.disabled == ["core"]
