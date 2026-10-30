# plus-ultra-scientia

A personal research pipeline: describe a topic, discover papers across OpenAlex, arXiv and
Europe PMC, keep them deduplicated in a local SQLite library, download the open-access full
text, and get a Markdown reading list.

This is milestone 1 of a larger plan: discovery → licensed acquisition → structured
extraction → evidence graph → hypothesis generation. The database is the source of truth.
LLM/agent layers are meant to sit on top of it later.

## Setup

```powershell
python -m venv .venv
.venv\Scripts\pip install -e ".[dev]"
```

Environment variables (all optional):

| Variable | Purpose |
|---|---|
| `OPENALEX_API_KEY` | Free key from <https://openalex.org/settings/api-key>. Gives $1/day of usage; without it your IP shares $0.10/day with everyone else on it. |
| `SCIENTIA_EMAIL` | Contact address sent in the User-Agent of every request; also used for Unpaywall if `UNPAYWALL_EMAIL` isn't set. |
| `UNPAYWALL_EMAIL` | Address sent **only** to Unpaywall, which requires one to enable its lookup. Use this to keep your address out of every other request. |
| `S2_API_KEY` | Semantic Scholar key (free). Without it, the shared public pool is often throttled. |
| `CORE_API_KEY` | CORE key (free, <https://core.ac.uk/services/api>). Raises the rate limit and enables CORE's own download endpoint. |
| `ANTHROPIC_API_KEY` | Claude API key from <https://platform.claude.com> (billed separately from a Claude subscription). Needed only for `scientia review`. |
| `SCIENTIA_HOME` | Library location. Default: `~/scientia-library` (`C:\Users\<you>\scientia-library` on Windows), the same whichever folder you run from. |

## Research projects

This repository is the infrastructure. Each piece of research lives in its own folder, kept
outside this repository and versionable on its own. A `project.toml` in that folder declares:

- the topics;
- their labelled queries, with `${VARIABLES}` for parts repeated across queries;
- the research question each topic's papers are judged against;
- the review profile (or a custom JSON schema), plus project-specific guidance for the LLM prompts;
- the citation-expansion steps.

```powershell
scientia run ..\axspa-research --dry-run       # apply project.toml, show what would run + cost
scientia run ..\axspa-research                 # search, expand, triage, fetch, reports, inventory
scientia run ..\axspa-research --with-extract  # also Opus evidence extraction (opt-in: costly)
scientia run ..\axspa-research --topic axspa-treatment --steps search,triage
```

`run` is idempotent:
- a query already searched on a source is skipped, and so is an expansion step already run
  with the same settings;
- reviewed papers aren't sent again, and downloads only try papers not yet attempted;
- `--refresh` re-runs searches and expansions (to pick up new publications) and retries
  failed downloads.

LLM spend per run is capped by `max_review_cost`. If the estimate exceeds the cap, the
top-ranked papers that fit are reviewed and the rest wait for the next run.

Outputs go to `<project>/outputs/`: `inventory.csv`, plus `<topic>/reading-list.md` and
`<topic>/evidence.md`. All papers, PDFs and the database stay in the shared library, so a
paper found by two projects is stored once. The format is documented at the top of
`scientia/project.py`.

The individual commands below remain available for exploring a topic by hand.

## Usage

```powershell
# Discover: query every source, merge duplicates, score, store. Each query is saved
# under a label; a topic can have many (one per sub-question, drug class, mechanism...).
scientia search axspa-treatment '("ankylosing spondylitis" OR "axial spondyloarthritis") AND (secukinumab OR ixekizumab)' --label il17 --limit 100
scientia search axspa-treatment '("ankylosing spondylitis" OR "axial spondyloarthritis") AND (tofacitinib OR upadacitinib)' --label jak --limit 100

# Re-run all saved queries later, newest first (only new papers are added), or just one
scientia search axspa-treatment --sort recent
scientia search axspa-treatment --label jak
scientia queries axspa-treatment              # list them; --remove <label> to drop one

scientia list neural-operators --top 20

# Follow citations from the 25 best papers: what they cite, and what cites them
scientia expand neural-operators --seeds 25
scientia list neural-operators --sort foundational   # most cited within the topic
scientia list neural-operators --sort frontier       # last 3 years, most connected to the topic

# LLM review of abstracts (needs ANTHROPIC_API_KEY): triage every paper, then extract evidence
scientia review axspa-treatment --profile clinical --question "Which treatments work in axSpA, and who responds?"
scientia review axspa-treatment --wait                    # collect the batch when it finishes
scientia review axspa-treatment --stage extract --dry-run # cost estimate first
scientia review axspa-treatment --stage extract --wait
scientia evidence axspa-treatment                         # evidence.md + evidence.csv

scientia fetch neural-operators --top 10 -v   # download open-access PDFs / XML (6 in parallel)
scientia inventory axspa-treatment axspa-genetics-immunology   # every paper, relevance + access, as CSV
scientia report neural-operators              # <library>/topics/neural-operators/reading-list.md
scientia import --move                        # file PDFs you saved into <library>/inbox/
scientia mark neural-operators 12 read --note "key idea: spectral convolution"
scientia topics
scientia info                                 # where the library is, which keys are set
```

Library layout:

```
scientia-library/
  research.db                         papers, topics, searches, per-topic status, files
  inbox/                              drop manually downloaded PDFs here
  papers/<doi_|arxiv_|...id>/         paper.pdf, paper.xml (Europe PMC JATS), metadata.json
  topics/<topic>/reading-list.md
```

File paths in the database are relative to the library root. You can move the whole folder,
or sync it to another machine, and point `SCIENTIA_HOME` at the new location. Libraries
written with absolute paths by older versions are converted automatically on open.
The library is kept outside the repository, so licensed PDFs can't be committed by accident.

## Where full text comes from

`fetch` tries these routes in order and stops at the first real PDF. It checks the file
bytes, because publisher "PDF" links often return an HTML landing page instead.

1. **arXiv** – free, paced to one request every 3 s as arXiv's API terms ask.
2. **Europe PMC full-text XML** – open-access subset only. This is the approved way to
   retrieve PMC content automatically.
3. **Open-access links** reported by OpenAlex / Europe PMC.
4. **Unpaywall** by DOI – only when `UNPAYWALL_EMAIL` or `SCIENTIA_EMAIL` is set.
5. **Semantic Scholar** `openAccessPdf`, by DOI, PMID or exact title.
6. **CORE** – repository copies, often accepted manuscripts of paywalled articles. Found
   by DOI or exact title and downloaded from the original repository. CORE's own
   `core.ac.uk/download` links sit behind a Cloudflare bot check, so they are skipped.
7. **OpenAlex cached PDF** – metered at $0.01 per file. Only runs with `--allow-paid`.

Papers with no open copy are marked `unavailable`. The reading list shows them with their
DOI link so you can retrieve them by hand through the UNED library.

Papers are fetched in parallel (`--workers`, default 6). The HTTP client hands out per-site
time slots, so each site's rate limit still holds. Before the downloads, Semantic Scholar is
asked about all candidates at once (500 per call). Every route tried, with its result, is
saved per paper.

`scientia inventory <topic>...` writes every paper of one or more topics to a CSV that opens
in Excel. Each row has its relevance rating, study design, OpenAlex open-access status and
an **access** category:

- `downloaded (PDF)`, or `downloaded (XML only)`;
- `free copy, download failed`: a legal free copy exists, but the publisher blocked the
  script, or the link returned a web page. Usually downloadable by hand in a browser.
- `free copy, OpenAlex cache`: only OpenAlex's cached PDF ($0.01 with `--allow-paid`);
- `licence needed`: no free copy is known; get it via the library, then `scientia import`;
- `not attempted yet`.

An "access detail" column gives the reason.

### Importing PDFs you downloaded yourself

1. Open the DOI links from the reading list's *Needs manual retrieval* section while signed in to the UNED library.
2. Save each PDF into the library's `inbox/` folder (`scientia info` shows where it is). Any file name works.
3. Run `scientia import --move`.

Each file is matched to its paper by the DOI in the PDF's metadata or file name, then by a
DOI or arXiv id on the first two pages, then by the paper's title on page 1. The file is then
filed under `papers/` and marked `downloaded` in every topic waiting for it. With
`--topic <name>`, a PDF whose DOI isn't in the library yet is looked up in OpenAlex (free)
and added to that topic. Identical files are detected by hash. Unmatched files are left
in place.

**Subscription content is intentionally not automated.** UNED's library forbids automated
mass downloading through its proxy. The legitimate route is the publishers' text-and-data-mining
APIs (Elsevier, Wiley, Springer Nature), which need entitlement confirmed by the library.
Ask `referencia@adm.uned.es` before adding those clients. Also ask whether licensed full text
may be processed by an external LLM API or only locally.

## Costs (OpenAlex, 2026)

A search costs $0.001 per call (up to 100 results). Single-record lookups are free. A cached
PDF costs $0.01. A typical `search` with three sources spends about $0.001 of OpenAlex budget.
arXiv, Europe PMC and Unpaywall are free.

A 25-seed `expand` costs well under a cent.

## Queries

All sources accept the same syntax: words, `"quoted phrases"`, `AND` / `OR` / `NOT` in
capitals, and parentheses. Adjacent terms are AND-ed. The query is translated for arXiv,
which needs a field prefix on every term. In OpenAlex, `*` is a wildcard, so write allele
names like `HLA-B*27` as `"HLA-B27"`.

A broad field needs several queries. "Ankylosing spondylitis" alone matches 52,000+ works,
and the top 100 by relevance are mostly reviews. One query per drug class or mechanism
reaches the primary research. For ranking, every query is an alternative. Within a query,
each AND-ed part counts as matched if any of its OR-ed options appears. So a paper on
ixekizumab in axial spondyloarthritis fully matches the `il17` query above.

## Citation expansion

Keyword search misses foundational papers that use older terminology, and recent ones that
use new terminology. `expand` takes the topic's best papers as seeds and follows citations
both ways through OpenAlex:

- **Backward** adds works cited by at least `--min-links` seeds (default 2): foundations.
- **Forward** adds works that cite at least `--min-links` topic papers: the frontier.

Each direction adds at most `--max-new` papers (default 50), most connected first, with a note
saying why each was added. Editorials, letters and errata are skipped (`--skip-types`; add
`review` to keep only primary research). Every run is one more snowball step outwards: a
second run starts from the grown topic and reaches further.

Citation edges are stored for every OpenAlex record the tool sees, including search results.
OpenAlex sometimes splits one paper into several records (preprint, conference and journal
versions), each with part of the citations. Every OpenAlex id is mapped to a single paper,
so each paper is counted once.

It works for any field OpenAlex covers. On a GLP-1 cardiovascular-outcomes topic, one
expansion surfaced the eight landmark outcome trials (LEADER, SUSTAIN-6, REWIND…) as the
topic's most cited papers. The keyword search had found none of them.

## Ranking

The stage-A score is in [0, 1] (`scientia/ranking.py`):

- 0.45 × query match in title/abstract (best-matching query, boolean-aware)
- 0.25 × citation links within the topic: being cited by topic papers counts 1, citing one
  counts 0.3, scaled to the topic's best-connected paper
- 0.15 × citation velocity
- 0.15 × recency

It is deliberately cheap and transparent. Its known weakness is literal query matching: a
trial titled "Liraglutide and…" doesn't match "GLP-1 receptor agonists". Embedding similarity
and LLM relevance judgements belong on the shortlist this score produces. Until then, use
`list --sort foundational` and `--sort frontier` for those two reading purposes. Reading
lists open with a *Core papers* section.

Papers are tagged `[RETRACTED]` (from OpenAlex) and `[review]`, `[editorial]`… by work type.

## LLM review

`scientia review` sends each paper's title and abstract to Claude through the Message
Batches API, which costs half price and usually returns within an hour. Every request uses
structured outputs, so each result is JSON in a fixed schema. It runs in two stages:

1. **Triage** (`claude-haiku-5-5`, every paper) gives:
   - relevance to the topic's research question: central / relevant / tangential / off-topic, with a one-sentence reason;
   - the study design;
   - key terms: drugs, genes, pathways, biomarkers.

   Before triage, missing abstracts are looked up for free: Europe PMC by PMID, then
   Semantic Scholar by DOI. Some publishers block their abstracts in both, so those papers
   are triaged on the title alone. The triage rating replaces the keyword match in the
   ranking. `list` shows it in the `rel` column: C(entral), R(elevant), t(angential), x (off-topic).
2. **Extraction** (`claude-opus-5-5`) covers central and relevant papers that have an
   abstract. It never starts unless asked with `--stage extract`. The schema follows the
   topic's `--profile`:
   - `clinical`: population, interventions with drug class, comparator, outcomes, response predictors, limitations, open questions;
   - `genetics`: genes and variants, pathways, cell types, data types, findings with their evidence, public datasets, therapeutic implications;
   - `general`.

   The model may record only what the abstract states, copying numbers as written.

Only abstracts are sent, never licensed full text. Re-running is safe: papers in a running
batch, or already reviewed, are not sent again. Failed or truncated results are retried.
`--sync` sends requests one by one at full price, for small tests. In that mode, Opus
requests opt into server-side refusal fallbacks (`fallbacks: "default"`); the Batches API
doesn't accept that option.

`scientia evidence <topic>` turns the extractions into `evidence.md` and `evidence.csv`. For
a clinical topic, that means evidence tables by drug class, plus reported response
predictors and open questions. For a genetics topic, it means the most-studied genes and
pathways, key findings, public datasets and stated therapeutic implications.

Costs on the AS pilot (about 700 papers per topic): triage about $0.13 per topic, and
extraction about $0.013 per paper at batch price.

## Backing up the library

The library (database and PDFs) is never versioned. Back it up to a folder synced by
Google Drive or OneDrive instead:

```powershell
scientia backup "G:\My Drive\scientia-library-backup"
```

The database is copied with SQLite's online backup API, so the copy is consistent even
while the library is in use. Papers and outputs are copied incrementally, and nothing is
ever deleted at the destination. Keep working from the local library: a sync client
rewriting `research.db` while SQLite writes to it can corrupt it. To restore, copy the
backup to a local disk and point `SCIENTIA_HOME` at it.

## Robustness

If a lookup service fails 12 times in a row during a fetch (CORE, Semantic Scholar,
Unpaywall, Europe PMC XML), it is switched off for the rest of the run. Its attempts are
then recorded as `skipped_disabled`, so an outage costs seconds instead of hours.
Optional lookups are retried once, not with the full backoff.

## Next steps

- `scientia update`: re-search with recent-first sorting, triage only what is new, and write a "what's new since last time" digest; schedule it weekly.
- A bridge report across topics: shared key terms, and genetics findings that bear on treatment questions.
- A full-text layer (Europe PMC JATS and PDF to sectioned text) with a licence policy deciding what may be sent to an LLM.
- Export to Zotero (BibTeX/RIS with PDFs); CI running the tests on every push.
- Publisher TDM clients, once UNED confirms entitlement.

## Tests

```powershell
.venv\Scripts\python -m pytest
```

Tests run offline against fixtures.
