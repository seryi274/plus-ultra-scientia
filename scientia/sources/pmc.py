"""PubMed Central's open-access articles on AWS (the "PMC Article Datasets" open-data bucket).

NIH publishes every article of the PMC open-access subset and of the author-manuscript
collection there, as PDF, JATS XML and plain text, with a JSON record carrying the licence.
It is the approved route for automated retrieval (the older oa.fcgi service is gone), needs
no credentials, and each article lives under <PMCID>.<version>/.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from scientia.net import HttpClient

BUCKET = "https://pmc-oa-opendata.s3.amazonaws.com"
_VERSION_PREFIX = re.compile(r"<Prefix>(PMC\d+)\.(\d+)/</Prefix>")


@dataclass
class PMCArticle:
    pmcid: str
    version: int
    license: str | None
    pdf_url: str | None
    xml_url: str | None


class PMCCloud:
    name = "pmc"

    def __init__(self, http: HttpClient):
        self._http = http

    def find(self, pmcid: str) -> PMCArticle | None:
        """The latest version of an article, or None if it isn't in the open-access datasets.
        Raises RuntimeError when the service itself fails."""
        listing = self._http.get(f"{BUCKET}/", params={"list-type": "2", "prefix": f"{pmcid}.", "delimiter": "/"},
                                 retries=1)
        if listing.status_code != 200:
            raise RuntimeError(f"PMC cloud HTTP {listing.status_code}")
        versions = [int(v) for p, v in _VERSION_PREFIX.findall(listing.text) if p == pmcid]
        if not versions:
            return None
        version = max(versions)
        meta = self._http.get(f"{BUCKET}/{pmcid}.{version}/{pmcid}.{version}.json", retries=1)
        if meta.status_code != 200:
            raise RuntimeError(f"PMC cloud metadata HTTP {meta.status_code}")
        record = meta.json()
        return PMCArticle(pmcid, version, record.get("license_code"),
                          _https(record.get("pdf_url")), _https(record.get("xml_url")))


def _https(s3_url: str | None) -> str | None:
    """s3://pmc-oa-opendata/KEY?md5=... -> https://pmc-oa-opendata.s3.amazonaws.com/KEY"""
    if not s3_url or not s3_url.startswith("s3://pmc-oa-opendata/"):
        return None
    return f"{BUCKET}/{s3_url.removeprefix('s3://pmc-oa-opendata/').split('?', 1)[0]}"
