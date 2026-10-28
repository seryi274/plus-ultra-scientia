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
| `SCIENTIA_EMAIL` | Contact address sent in the User-Agent, and required to enable the Unpaywall fallback. |
| `S2_API_KEY` | Semantic Scholar key (free). Without it, the shared public pool is often throttled. |
| `CORE_API_KEY` | CORE key (free, <https://core.ac.uk/services/api>). Raises the rate limit and enables CORE's own download endpoint. |
| `SCIENTIA_HOME` | Library location (default `./library`, which is git-ignored). |

## Usage

```powershell
# Discover: query every source, merge duplicates, score, store
scientia search neural-operators "fourier neural operator" --since 2020-01-01 --limit 50

# Re-run later with the saved query, newest first (only new papers are added)
scientia search neural-operators --sort recent

scientia list neural-operators --top 20
scientia fetch neural-operators --top 10 -v   # download open-access PDFs / XML
scientia report neural-operators              # library/topics/neural-operators/reading-list.md
scientia import --move                        # file PDFs you saved into library/inbox/
scientia mark neural-operators 12 read --note "key idea: spectral convolution"
scientia topics
```

Library layout:

```
library/
  research.db                         papers, topics, searches, per-topic status, files
  inbox/                              drop manually downloaded PDFs here
  papers/<doi_|arxiv_|...id>/         paper.pdf, paper.xml (Europe PMC JATS), metadata.json
  topics/<topic>/reading-list.md
```

## Where full text comes from

`fetch` tries these routes in order and stops at the first real PDF. It checks the file
bytes, because publisher "PDF" links often return an HTML landing page instead.

1. **arXiv** – free, paced to one request every 3 s as arXiv's API terms ask.
2. **Europe PMC full-text XML** – open-access subset only. This is the approved way to
   retrieve PMC content automatically.
3. **Open-access links** reported by OpenAlex / Europe PMC.
4. **Unpaywall** by DOI – only when `SCIENTIA_EMAIL` is set.
5. **Semantic Scholar** `openAccessPdf`, by DOI, PMID or exact title.
6. **CORE** – repository copies, often accepted manuscripts of paywalled articles. Found
   by DOI or exact title and downloaded from the original repository. CORE's own
   `core.ac.uk/download` links sit behind a Cloudflare bot check, so they are skipped.
7. **OpenAlex cached PDF** – metered at $0.01 per file. Only runs with `--allow-paid`.

Papers with no open copy are marked `unavailable`. The reading list shows them with their
DOI link so you can retrieve them by hand through the UNED library.

### Importing PDFs you downloaded yourself

1. Open the DOI links from the reading list's *Needs manual retrieval* section while signed in to the UNED library.
2. Save each PDF into `library/inbox/`. Any file name works.
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

## Ranking

The stage-A score is in [0, 1]: 0.6 × query match in title/abstract + 0.25 × citation
velocity + 0.15 × recency (`scientia/ranking.py`). It is deliberately cheap and transparent.
Embedding similarity and LLM relevance judgements belong on the shortlist that this score
produces.

## Next steps

- Citation expansion via OpenAlex (`referenced_works`, `cites:` filter) to find foundational and follow-up work.
- Embedding-based re-ranking and LLM relevance review with structured output.
- Structured extraction per paper (problem, methods, assumptions, claims with locations, limitations).
- Scheduled monitoring (weekly `search --sort recent` plus a digest).
- Publisher TDM clients, once UNED confirms entitlement.

## Tests

```powershell
.venv\Scripts\python -m pytest
```

Tests run offline against fixtures.
