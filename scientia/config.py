"""Runtime configuration, read from environment variables.

SCIENTIA_HOME      Library root (database + downloaded files). Default: ./library
OPENALEX_API_KEY   Free key from https://openalex.org/settings/api-key. Without it,
                   every caller on your IP shares a $0.10/day budget.
SCIENTIA_EMAIL     Contact address sent to APIs that ask for one (Unpaywall requires
                   it; arXiv and Europe PMC appreciate it in the User-Agent).
CORE_API_KEY       Optional; raises CORE's rate limit (https://core.ac.uk/services/api).
S2_API_KEY         Optional; Semantic Scholar key, avoids throttling of the public pool.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    home: Path
    openalex_api_key: str | None
    email: str | None
    core_api_key: str | None = None
    s2_api_key: str | None = None

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
            home=Path(os.environ.get("SCIENTIA_HOME", "library")).resolve(),
            openalex_api_key=os.environ.get("OPENALEX_API_KEY") or None,
            email=os.environ.get("SCIENTIA_EMAIL") or None,
            core_api_key=os.environ.get("CORE_API_KEY") or None,
            s2_api_key=os.environ.get("S2_API_KEY") or None,
        )
