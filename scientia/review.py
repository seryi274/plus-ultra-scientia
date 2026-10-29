"""LLM review of a topic's papers, in two stages:

  triage   Claude Haiku 5.5 reads every paper's title and abstract and rates its relevance
           to the topic's research question, its study design and the key terms it is about.
  extract  Claude Opus 5.5 extracts structured evidence from the abstracts of the papers
           rated central or relevant, with a schema chosen by the topic's profile.

Both stages go through the Message Batches API (half price, results usually within an
hour); `sync=True` sends requests one by one instead, for small runs. Every request uses
structured outputs, so results are JSON that matches the stage's schema.

Only abstracts are sent (no licensed full text), and the extraction prompt restricts the
model to what the abstract states.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import anthropic

from scientia.abstracts import MIN_ABSTRACT_CHARS
from scientia.store import Store, StoredPaper

TRIAGE_MODEL = "claude-haiku-5-5"
EXTRACT_MODEL = "claude-opus-5-5"
# USD per million tokens (input, output); the Batches API charges half.
PRICES = {TRIAGE_MODEL: (0.10, 0.50), EXTRACT_MODEL: (4.00, 20.00)}
# Rough output tokens per request, thinking included, for cost estimates.
_EXPECTED_OUTPUT = {"triage": 600, "extract": 2000}
_FALLBACK_BETA = "server-side-fallback-2026-07-01"
DONE = ("ok", "pending", "refused")  # statuses that are not re-submitted

RELEVANCE = ("central", "relevant", "tangential", "off_topic")
STUDY_DESIGNS = (
    "randomized_controlled_trial", "non_randomized_trial", "cohort", "case_control",
    "cross_sectional", "case_report_or_series", "systematic_review_or_meta_analysis",
    "narrative_review", "guideline_or_consensus", "genetic_association",
    "mendelian_randomization", "omics_profiling", "basic_or_translational_lab", "animal_model",
    "computational_or_methods", "other",
)
DATA_TYPES = (
    "gwas", "candidate_gene", "fine_mapping", "mendelian_randomization",
    "eqtl_or_functional_genomics", "transcriptomics", "single_cell", "epigenomics",
    "proteomics_or_metabolomics", "microbiome", "structural_biology", "cell_or_tissue_experiment",
    "animal_model", "clinical_cohort", "review", "other",
)


# -- schemas ------------------------------------------------------------------------

def _obj(**props: Any) -> dict:
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


def _str(description: str = "") -> dict:
    return {"type": "string", "description": description} if description else {"type": "string"}


def _list(items: dict, description: str = "") -> dict:
    schema = {"type": "array", "items": items}
    return {**schema, "description": description} if description else schema


def _enum(values: tuple[str, ...]) -> dict:
    return {"type": "string", "enum": list(values)}


TRIAGE_SCHEMA = _obj(
    relevance=_enum(RELEVANCE),
    reason=_str("one sentence"),
    study_design=_enum(STUDY_DESIGNS),
    key_terms=_list(_str(), "drugs, genes, variants, pathways, cell types, biomarkers or datasets; at most 10"),
)

_FINDINGS = _list(_obj(finding=_str(), evidence=_str("the statistic or experiment as stated")))

PROFILES: dict[str, tuple[dict, str]] = {
    "clinical": (
        _obj(
            population=_str("condition, subgroup and key characteristics"),
            sample_size=_str(),
            interventions=_list(_obj(name=_str(), drug_class=_str("standard class name, e.g. 'TNF inhibitor'"),
                                     regimen=_str())),
            comparator=_str(),
            outcomes=_list(_obj(measure=_str("e.g. ASAS40, BASDAI, mSASSS"), timepoint=_str(), result=_str())),
            response_predictors=_list(_obj(
                factor=_str(),
                association=_enum(("better_response", "worse_response", "no_association", "unclear")),
                detail=_str(),
            )),
            follow_up=_str(),
            main_finding=_str(),
            limitations=_list(_str()),
            open_questions=_list(_str()),
        ),
        "- interventions: every treatment studied, with its drug class in standard terms "
        "(e.g. 'TNF inhibitor', 'IL-17 inhibitor', 'JAK inhibitor', 'IL-23 inhibitor', 'NSAID').\n"
        "- response_predictors: only factors the abstract reports as associated (or explicitly not "
        "associated) with treatment response, e.g. CRP, HLA-B27, MRI inflammation, sex, smoking, BMI.",
    ),
    "genetics": (
        _obj(
            genes_variants=_list(_str(), "genes, loci and variants as written, e.g. 'ERAP1 rs30187'"),
            pathways=_list(_str()),
            cell_types=_list(_str()),
            data_types=_list(_enum(DATA_TYPES)),
            population=_str("ancestry, sample size, cases/controls"),
            key_findings=_FINDINGS,
            public_datasets=_list(_obj(name=_str(), identifier=_str("accession, consortium or URL"))),
            therapeutic_implications=_list(_str("only those the abstract states")),
            main_finding=_str(),
            limitations=_list(_str()),
            open_questions=_list(_str()),
        ),
        "- public_datasets: data the study used or released that others could reuse "
        "(GWAS summary statistics, GEO accessions, biobanks, consortia).\n"
        "- therapeutic_implications: only links to treatment that the abstract itself draws.",
    ),
    "general": (
        _obj(methods=_str(), key_findings=_FINDINGS, main_finding=_str(),
             limitations=_list(_str()), open_questions=_list(_str())),
        "",
    ),
}

_TRIAGE_SYSTEM = """You screen scientific papers for a literature review.

Research question:
{question}

For each paper you get its title, metadata and abstract. Judge:
- relevance to the research question:
  central: directly addresses the question; core reading
  relevant: contributes evidence or methods to part of the question
  tangential: same disease or field, but about something else
  off_topic: unrelated
- study_design: what the paper reports doing, as described
- key_terms: the specific drugs, genes, variants, pathways, cell types, biomarkers or datasets the paper is about, written as they appear (e.g. "secukinumab", "ERAP1", "IL-23/IL-17 axis"), at most 10
- reason: one sentence explaining the relevance rating

Use only the information given. When there is no abstract, judge from the title, keep the rating conservative and say so in the reason."""

_EXTRACT_SYSTEM = """You extract structured evidence from scientific abstracts for a literature review.

Research question:
{question}

Rules:
- Record only what the abstract states. Do not add facts from your own knowledge, and do not compute or estimate numbers the abstract does not give.
- Use an empty string or an empty list when the abstract does not report something.
- Copy numbers exactly as written, with units, confidence intervals and p-values.
- Keep each entry short: a phrase or one sentence.
- limitations and open_questions: only those the abstract states or explicitly leaves unresolved, not your own suggestions.
{guide}"""


# -- reviewer -----------------------------------------------------------------------

@dataclass
class CollectResult:
    finished: int = 0
    still_running: list[str] = field(default_factory=list)
    recorded: dict[str, int] = field(default_factory=dict)  # status -> count


class Reviewer:
    def __init__(self, store: Store, topic_id: int, question: str, profile: str,
                 client: anthropic.Anthropic | None = None):
        if profile not in PROFILES:
            raise ValueError(f"profile must be one of {', '.join(PROFILES)}")
        self.store = store
        self.topic_id = topic_id
        self.question = question
        self.profile = profile
        self._client = client

    @property
    def client(self) -> anthropic.Anthropic:
        if self._client is None:
            self._client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
        return self._client

    # -- what to review ------------------------------------------------------------

    def candidates(self, stage: str, include_tangential: bool = False) -> list[StoredPaper]:
        done = {pid for pid, r in self.store.reviews(self.topic_id, stage).items() if r["status"] in DONE}
        papers = [p for p in self.store.topic_papers(self.topic_id) if p.id not in done]
        if stage == "triage":
            return papers
        wanted = {"central", "relevant", "tangential"} if include_tangential else {"central", "relevant"}
        rated = {
            pid for pid, r in self.store.reviews(self.topic_id, "triage").items()
            if r["status"] == "ok" and json.loads(r["result"])["relevance"] in wanted
        }
        return [p for p in papers if p.id in rated and len(p.abstract or "") > MIN_ABSTRACT_CHARS]

    def estimate_cost(self, stage: str, papers: list[StoredPaper], batch: bool = True) -> float:
        model = self.model(stage)
        price_in, price_out = PRICES[model]
        system_tokens = len(self._system(stage)) / 3.5
        input_tokens = sum(system_tokens + len(_paper_text(p)) / 3.5 for p in papers)
        output_tokens = _EXPECTED_OUTPUT[stage] * len(papers)
        cost = (input_tokens * price_in + output_tokens * price_out) / 1e6
        return cost / 2 if batch else cost

    @staticmethod
    def model(stage: str) -> str:
        return TRIAGE_MODEL if stage == "triage" else EXTRACT_MODEL

    # -- requests ------------------------------------------------------------------

    def _system(self, stage: str) -> str:
        if stage == "triage":
            return _TRIAGE_SYSTEM.format(question=self.question)
        return _EXTRACT_SYSTEM.format(question=self.question, guide=PROFILES[self.profile][1])

    def request_params(self, stage: str, paper: StoredPaper) -> dict[str, Any]:
        schema = TRIAGE_SCHEMA if stage == "triage" else PROFILES[self.profile][0]
        return {
            "model": self.model(stage),
            "max_tokens": 4096 if stage == "triage" else 16000,  # thinking counts toward max_tokens
            # Identical for every paper in a topic, so it is cached across requests.
            "system": [{"type": "text", "text": self._system(stage), "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": _paper_text(paper)}],
            "output_config": {
                "effort": "low" if stage == "triage" else "medium",
                "format": {"type": "json_schema", "schema": schema},
            },
        }

    def submit_batch(self, stage: str, papers: list[StoredPaper]) -> str:
        from anthropic.types.messages.batch_create_params import Request

        batch = self.client.messages.batches.create(requests=[
            Request(custom_id=f"{stage}-{p.id}", params=self.request_params(stage, p)) for p in papers
        ])
        model = self.model(stage)
        self.store.add_review_batch(batch.id, self.topic_id, stage, model, len(papers))
        for p in papers:
            self.store.save_review(self.topic_id, p.id, stage, "pending", model, batch_id=batch.id)
        self.store.commit()
        return batch.id

    def collect(self) -> CollectResult:
        """Record the results of this topic's batches that have finished."""
        result = CollectResult()
        for row in self.store.open_review_batches(self.topic_id):
            batch = self.client.messages.batches.retrieve(row["batch_id"])
            if batch.processing_status != "ended":
                counts = batch.request_counts
                result.still_running.append(
                    f"{row['stage']} batch {row['batch_id']}: {counts.succeeded + counts.errored}"
                    f" of {row['requests']} done"
                )
                continue
            for item in self.client.messages.batches.results(row["batch_id"]):
                stage, paper_id = item.custom_id.split("-", 1)
                if item.result.type == "succeeded":
                    status = self._record(stage, int(paper_id), item.result.message, batch=True)
                else:  # errored, canceled or expired: eligible for resubmission
                    status = "error"
                    self.store.save_review(self.topic_id, int(paper_id), stage, status, row["model"],
                                           batch_id=row["batch_id"], result=json.dumps({"error": item.result.type}))
                result.recorded[status] = result.recorded.get(status, 0) + 1
            self.store.close_review_batch(row["batch_id"])
            self.store.commit()
            result.finished += 1
        return result

    def run_sync(self, stage: str, papers: list[StoredPaper]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for paper in papers:
            params = self.request_params(stage, paper)
            if stage == "extract":
                # Opus: if a safety classifier declines, the API retries on a fallback model.
                message = self.client.beta.messages.create(**params, betas=[_FALLBACK_BETA], fallbacks="default")
            else:
                message = self.client.messages.create(**params)
            status = self._record(stage, paper.id, message, batch=False)
            counts[status] = counts.get(status, 0) + 1
            self.store.commit()
        return counts

    def _record(self, stage: str, paper_id: int, message: Any, batch: bool) -> str:
        usage = message.usage
        cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
        cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
        price_in, price_out = PRICES.get(self.model(stage), (0.0, 0.0))
        cost = ((usage.input_tokens + 1.25 * cache_write + 0.1 * cache_read) * price_in
                + usage.output_tokens * price_out) / 1e6
        if batch:
            cost /= 2

        text = next((b.text for b in message.content if b.type == "text"), "")
        if message.stop_reason == "refusal":
            status, result = "refused", json.dumps({"stop_details": str(getattr(message, "stop_details", ""))})
        elif message.stop_reason == "max_tokens":
            status, result = "truncated", None
        else:
            try:
                json.loads(text)
                status, result = "ok", text
            except json.JSONDecodeError:
                status, result = "error", json.dumps({"unparsed": text[:2000]})
        self.store.save_review(
            self.topic_id, paper_id, stage, status, getattr(message, "model", self.model(stage)),
            result=result, input_tokens=usage.input_tokens + cache_write + cache_read,
            output_tokens=usage.output_tokens, cost_usd=round(cost, 6),
        )
        return status


def _paper_text(p: StoredPaper) -> str:
    abstract = p.abstract if len(p.abstract or "") > MIN_ABSTRACT_CHARS else None
    meta = " | ".join(x for x in (str(p.year or "n.d."), p.venue or "", p.work_type or "") if x)
    return (f"Title: {p.title}\n{meta}\n\n"
            + (f"Abstract:\n{abstract}" if abstract else "No abstract available."))


def triage_results(store: Store, topic_id: int) -> dict[int, dict]:
    """paper id -> parsed triage result, for papers triaged successfully."""
    return {pid: json.loads(r["result"]) for pid, r in store.reviews(topic_id, "triage").items()
            if r["status"] == "ok"}
