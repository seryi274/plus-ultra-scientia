"""A shared HTTP client: polite User-Agent, pacing, and retries.

Pacing is per *site*: a host, or for the large publishers every domain they serve from,
so that e.g. doi.org -> linkinghub.elsevier.com -> www.sciencedirect.com and cell.com all
share Elsevier's budget. Redirects are followed hop by hop, each hop waiting for its own
site's turn. Safe to share between threads: time slots are handed out under a lock, so
parallel workers together still respect each site's minimum interval."""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

import httpx

from scientia import __version__

# Minimum seconds between requests to a host. arXiv asks for one request every 3 s.
_HOST_INTERVAL = {
    "export.arxiv.org": 3.0,
    "arxiv.org": 3.0,
    "api.semanticscholar.org": 1.1,
    "api.core.ac.uk": 1.0,
}
_DEFAULT_INTERVAL = 0.2

# Publishers whose websites sit behind bot protection: one request every few seconds across
# all their domains, so that parallel downloads never look like a burst to them.
PUBLISHER_INTERVAL = 4.0
PUBLISHER_DOMAINS = {
    "elsevier": ("elsevier.com", "sciencedirect.com", "cell.com", "thelancet.com", "els-cdn.com",
                 "elsevierhealth.com", "clinicalkey.com"),
    "wiley": ("wiley.com",),
    "springer-nature": ("springer.com", "springernature.com", "nature.com", "biomedcentral.com",
                        "springeropen.com"),
    "bmj": ("bmj.com",),
    "oxford": ("oup.com",),
    "taylor-francis": ("tandfonline.com", "taylorandfrancis.com"),
    "sage": ("sagepub.com",),
    "wolters-kluwer": ("lww.com", "ovid.com", "wolterskluwer.com"),
    "acr": ("rheumatology.org",),
    "karger": ("karger.com",),
    "thieme": ("thieme-connect.com", "thieme-connect.de"),
    "cambridge": ("cambridge.org",),
    "jstage": ("jst.go.jp",),
}
_RETRY_STATUS = {429, 500, 502, 503, 504}
_MAX_REDIRECTS = 10


def site_of(host: str) -> str:
    """The pacing key for a host: its publisher group if it belongs to one, else the host."""
    host = host.lower()
    for publisher, domains in PUBLISHER_DOMAINS.items():
        if any(host == d or host.endswith("." + d) for d in domains):
            return publisher
    return host


class HttpClient:
    def __init__(self, email: str | None = None, retries: int = 3, timeout: float = 60.0,
                 transport: httpx.BaseTransport | None = None):
        contact = f" (mailto:{email})" if email else ""
        self._client = httpx.Client(
            headers={"User-Agent": f"plus-ultra-scientia/{__version__}{contact}"},
            timeout=timeout,
            follow_redirects=False,  # followed by hand in request(), so each hop is paced
            transport=transport,
        )
        self._retries = retries
        self._next_slot: dict[str, float] = {}
        self._lock = threading.Lock()

    def get(self, url: str, **kwargs) -> httpx.Response:
        """GET with pacing and retries on transient failures. Does not raise on 4xx."""
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs) -> httpx.Response:
        return self.request("POST", url, **kwargs)

    def request(self, method: str, url: str, retries: int | None = None, **kwargs) -> httpx.Response:
        """`retries` overrides the client default, e.g. 1 for optional lookups whose failure
        should cost seconds, not the full backoff."""
        retries = self._retries if retries is None else retries
        for attempt in range(retries + 1):
            try:
                response = self._send(method, url, **kwargs)
            except httpx.TransportError:
                if attempt == retries:
                    raise
                time.sleep(2**attempt)
                continue
            if response.status_code not in _RETRY_STATUS or attempt == retries:
                return response
            time.sleep(_retry_delay(response, attempt))
        raise AssertionError("unreachable")

    def _send(self, method: str, url: str, **kwargs) -> httpx.Response:
        self._wait_turn(urlsplit(url).hostname or "")
        response = self._client.request(method, url, **kwargs)
        for _ in range(_MAX_REDIRECTS):
            if not response.is_redirect or response.next_request is None:
                return response
            following = response.next_request
            self._wait_turn(following.url.host)
            response = self._client.send(following)
        return response

    def _wait_turn(self, host: str) -> None:
        site = site_of(host)
        if site in PUBLISHER_DOMAINS:
            interval = PUBLISHER_INTERVAL
        else:
            interval = _HOST_INTERVAL.get(host, _DEFAULT_INTERVAL)
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next_slot.get(site, 0.0))
            self._next_slot[site] = slot + interval
        if slot > now:
            time.sleep(slot - now)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> HttpClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    retry_after = response.headers.get("Retry-After", "")
    if retry_after.isdigit():
        return min(float(retry_after), 60.0)
    # CORE sends the time at which the window reopens, e.g. 2026-10-28T19:57:59+0000.
    reopen = response.headers.get("X-RateLimit-Retry-After", "")
    try:
        wait = (datetime.strptime(reopen, "%Y-%m-%dT%H:%M:%S%z") - datetime.now(timezone.utc)).total_seconds()
        return min(max(wait, 1.0), 60.0)
    except ValueError:
        return float(2 ** (attempt + 1))
