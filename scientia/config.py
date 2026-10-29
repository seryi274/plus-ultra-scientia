"""Runtime configuration, read from environment variables.

SCIENTIA_HOME      Library root (database + downloaded files). Default: ~/scientia-library,
                   a fixed per-user folder, so every command finds the same library no
                   matter which directory it is run from.
OPENALEX_API_KEY   Free key from https://openalex.org/settings/api-key. Without it,
                   every caller on your IP shares a $0.10/day budget.
SCIENTIA_EMAIL     Contact address sent in the User-Agent of every request (arXiv and
                   Europe PMC ask for one), and to Unpaywall unless UNPAYWALL_EMAIL is set.
UNPAYWALL_EMAIL    Address sent only to Unpaywall, which requires one. Use it instead of
                   SCIENTIA_EMAIL to keep the address out of all other requests.
CORE_API_KEY       Optional; raises CORE's rate limit (https://core.ac.uk/services/api).
S2_API_KEY         Optional; Semantic Scholar key, avoids throttling of the public pool.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


DEFAULT_HOME = Path.home() / "scientia-library"


@dataclass(frozen=True)
class Config:
    home: Path
    openalex_api_key: str | None
    email: str | None
    core_api_key: str | None = None
    s2_api_key: str | None = None
    unpaywall_email: str | None = None

    @property
    def db_path(self) -> Path:
        return self.home / "research.db"

    @property
    def papers_dir(self) -> Path:
        return self.home / "papers"

    @property
    def topics_dir(self) -> Path:
        return self.home / "topics"

    @property
    def inbox_dir(self) -> Path:
        """Where manually downloaded PDFs wait for `scientia import`."""
        return self.home / "inbox"

    @classmethod
    def from_env(cls) -> Config:
        return cls(
            home=Path(os.environ.get("SCIENTIA_HOME") or DEFAULT_HOME).expanduser().resolve(),
            openalex_api_key=os.environ.get("OPENALEX_API_KEY") or None,
            email=os.environ.get("SCIENTIA_EMAIL") or None,
            core_api_key=os.environ.get("CORE_API_KEY") or None,
            s2_api_key=os.environ.get("S2_API_KEY") or None,
            unpaywall_email=os.environ.get("UNPAYWALL_EMAIL") or os.environ.get("SCIENTIA_EMAIL") or None,
        )
