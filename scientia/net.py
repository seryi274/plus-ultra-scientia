"""A shared HTTP client: polite User-Agent, per-host pacing, and retries."""

from __future__ import annotations

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
_RETRY_STATUS = {429, 500, 502, 503, 504}


class HttpClient:
    def __init__(self, email: str | None = None, retries: int = 3, timeout: float = 60.0):
        contact = f" (mailto:{email})" if email else ""
        self._client = httpx.Client(
            headers={"User-Agent": f"plus-ultra-scientia/{__version__}{contact}"},
            timeout=timeout,
            follow_redirects=True,
        )
        self._retries = retries
        self._last_request: dict[str, float] = {}

    def get(self, url: str, **kwargs) -> httpx.Response:
        """GET with pacing and retries on transient failures. Does not raise on 4xx."""
        host = urlsplit(url).hostname or ""
        for attempt in range(self._retries + 1):
            self._wait_turn(host)
            try:
                response = self._client.get(url, **kwargs)
            except httpx.TransportError:
                if attempt == self._retries:
                    raise
                time.sleep(2**attempt)
                continue
            if response.status_code not in _RETRY_STATUS or attempt == self._retries:
                return response
            time.sleep(_retry_delay(response, attempt))
        raise AssertionError("unreachable")

    def _wait_turn(self, host: str) -> None:
        interval = _HOST_INTERVAL.get(host, _DEFAULT_INTERVAL)
        elapsed = time.monotonic() - self._last_request.get(host, 0.0)
        if elapsed < interval:
            time.sleep(interval - elapsed)
        self._last_request[host] = time.monotonic()

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
