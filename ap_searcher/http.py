"""Cached, rate-limited HTTP GET.

Every outbound request goes through here so that request volume stays visible
(counters) and bounded (TTL cache + minimum interval between calls).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

import requests

log = logging.getLogger(__name__)


class BudgetExceeded(RuntimeError):
    """Raised when a run hits its configured request budget."""


@dataclass
class Counters:
    requests: int = 0
    cache_hits: int = 0
    errors: int = 0

    def as_dict(self) -> dict[str, int]:
        return {"requests": self.requests, "cache_hits": self.cache_hits, "errors": self.errors}


@dataclass
class Fetcher:
    """One fetcher per upstream (sreality, routing), sharing the store's http_cache."""

    store: object
    user_agent: str
    kind: str
    min_interval_s: float = 1.0
    timeout_s: float = 30.0
    budget: int | None = None
    use_cache: bool = True
    extra_headers: dict[str, str] = field(default_factory=dict)
    counters: Counters = field(default_factory=Counters)
    _last_call: float = 0.0
    _session: requests.Session | None = None

    def __post_init__(self) -> None:
        self._session = requests.Session()

    def get(self, url: str, max_age_s: float, force: bool = False) -> str:
        if self.use_cache and not force:
            cached = self.store.http_get(url, max_age_s)
            if cached is not None:
                self.counters.cache_hits += 1
                return cached

        if self.budget is not None and self.counters.requests >= self.budget:
            raise BudgetExceeded(f"{self.kind}: request budget of {self.budget} reached")

        wait = self.min_interval_s - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)

        headers = {
            "User-Agent": self.user_agent,
            "Accept-Language": "cs,en;q=0.8",
            **self.extra_headers,
        }
        last_err: Exception | None = None
        for attempt in range(3):
            try:
                resp = self._session.get(url, headers=headers, timeout=self.timeout_s)
                self._last_call = time.monotonic()
                self.counters.requests += 1
                if resp.status_code == 200:
                    if self.use_cache:
                        self.store.http_put(url, self.kind, 200, resp.text)
                    return resp.text
                if resp.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                    log.warning("%s: HTTP %s on %s, retrying", self.kind, resp.status_code, url)
                    time.sleep(2 ** attempt * 2)
                    continue
                self.counters.errors += 1
                raise requests.HTTPError(f"HTTP {resp.status_code} for {url}")
            except requests.RequestException as exc:
                last_err = exc
                self._last_call = time.monotonic()
                if attempt < 2:
                    time.sleep(2 ** attempt * 2)
                    continue
                self.counters.errors += 1
                raise
        raise last_err or RuntimeError("unreachable")
