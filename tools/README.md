# tools

One-off scripts that support development of the pipeline but are not part of the `scientia`
command.

- `probe_sources.py`: measures how many extra PDFs a candidate full-text source would deliver
  (currently OpenAIRE and Crossref), before it is integrated. It runs against the papers an
  inventory lists as blocked, plus a fixed sample of those needing a licence. It is
  read-only: links are downloaded to check that they serve a real PDF, then discarded. It
  uses the library's HTTP client, so per-publisher pacing applies.

  ```powershell
  .venv\Scripts\python -I tools\probe_sources.py <inventory.csv> <library>\research.db results.json
  ```
