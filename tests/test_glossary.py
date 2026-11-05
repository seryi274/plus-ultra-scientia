import json
from types import SimpleNamespace as NS

import pytest

from scientia.glossary import GLOSSARY_SCHEMA, Glossary, run_glossary, term_key
from scientia.models import Paper
from scientia.project import ProjectError, load_project
from scientia.store import Store


def test_term_key_merges_spelling_variants_only():
    assert term_key("TNF inhibitors") == term_key("TNF-inhibitor") == term_key("tnf inhibitor")
    assert term_key("HLA-B*27") == term_key("HLA-B27")
    assert term_key("HLA-B*27:05") == term_key("HLA-B*2705")
    assert term_key("TNF-α") == term_key("TNF-alpha") == term_key("TNFα")
    assert term_key("γδ T cells") == term_key("gamma delta T cell")
    assert term_key("IL-17") != term_key("IL-17A")
    assert term_key("NSAIDs") == term_key("NSAID")


def message(entries, stop_reason="end_turn"):
    return NS(content=[NS(type="text", text=json.dumps({"entries": entries}))], stop_reason=stop_reason,
              usage=NS(input_tokens=2000, output_tokens=1000, cache_creation_input_tokens=0,
                       cache_read_input_tokens=0))


def entry(term, same_as="", confident=True):
    return {"term": term, "name_in_language": term, "expansion": "", "same_as": same_as,
            "category": "drug_or_intervention", "definition": f"What {term} is.",
            "in_this_field": "Why it matters.", "confident": confident}


class FakeClient:
    def __init__(self):
        self.requests = []
        self.messages = NS(create=self.create)

    def create(self, **params):
        self.requests.append(params)
        terms = [line[2:] for line in params["messages"][0]["content"].splitlines() if line.startswith("- ")]
        return message([entry(t, same_as="TNF inhibitors" if t == "anti-TNF" else "") for t in terms])


@pytest.fixture
def store(tmp_path):
    store = Store(tmp_path / "t.db")
    topic = store.upsert_topic("axspa")
    triage = [
        ("central", ["TNF inhibitors", "adalimumab", "anti-TNF"]),
        ("relevant", ["TNF inhibitor", "adalimumab", "anti-TNF"]),
        ("tangential", ["tnf inhibitors", "rare term"]),
        ("off_topic", ["adalimumab", "unrelated"]),  # off-topic papers do not count
    ]
    for i, (relevance, terms) in enumerate(triage):
        pid, _ = store.add_paper(Paper(title=f"Paper {i}", source="openalex", openalex_id=f"W{i}", year=2020))
        store.link(topic, pid)
        store.save_review(topic, pid, "triage", "ok", "m", result=json.dumps(
            {"relevance": relevance, "reason": "", "study_design": "other", "key_terms": terms}))
    store.commit()
    yield store, topic
    store.close()


def test_glossary_defines_frequent_terms_once_and_merges_synonyms(store, tmp_path):
    store, topic = store
    client = FakeClient()
    out = tmp_path / "glossary.md"
    cost = run_glossary(store, [topic], ["Which treatments work?"], out, title="axspa", language="es",
                        min_papers=2, client=client, log=lambda _: None)
    assert cost > 0 and len(client.requests) == 1
    params = client.requests[0]
    assert params["output_config"]["format"]["schema"] == GLOSSARY_SCHEMA
    system = params["system"][0]["text"]
    assert "Spanish" in system and "Which treatments work?" in system
    assert "TNF inhibitors (3)" in system and "adalimumab (2)" in system  # off-topic paper not counted
    assert "rare term" not in system and "unrelated" not in system

    text = out.read_text(encoding="utf-8")
    assert text.startswith("# Glosario: axspa")
    assert "### TNF inhibitors" in text and "también: anti-TNF" in text  # merged by same_as
    assert "### anti-TNF" not in text and "### adalimumab" in text
    assert "3 artículos" in text

    # Everything is defined: a second run makes no requests but still writes the file.
    assert run_glossary(store, [topic], [], out, title="axspa", language="es", min_papers=2, client=client,
                        log=lambda _: None) == 0.0
    assert len(client.requests) == 1
    # Another language is defined separately.
    assert Glossary(store, [topic], [], "en").pending(Glossary(store, [topic], [], "en").candidates(2))


def test_failed_or_unsure_definitions(store, tmp_path):
    store, topic = store
    glossary = Glossary(store, [topic], [], "en")
    candidates = glossary.candidates(min_papers=2)
    result = NS(defined=0, failed=0, cost=0.0)
    glossary._record(candidates, message([], stop_reason="max_tokens"), result)
    assert result.defined == 0 and result.failed == len(candidates)
    assert len(glossary.pending(candidates)) == len(candidates)  # retried next run

    glossary._record(candidates, message([entry("adalimumab", confident=False)]), result)
    assert result.defined == 1
    path = glossary.write(tmp_path / "g.md", "axspa", candidates, 2)
    assert "### adalimumab ⚠ check" in path.read_text(encoding="utf-8")


def test_project_glossary_settings(tmp_path):
    (tmp_path / "project.toml").write_text(
        '[[topics]]\nname = "t"\n\n[glossary]\nlanguage = "es"\n', encoding="utf-8")
    assert load_project(tmp_path).glossary == {"language": "es", "min_papers": 3, "max_terms": 400}
    (tmp_path / "project.toml").write_text(
        '[[topics]]\nname = "t"\n\n[glossary]\nlang = "es"\n', encoding="utf-8")
    with pytest.raises(ProjectError, match="lang"):
        load_project(tmp_path)
