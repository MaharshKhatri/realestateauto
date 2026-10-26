"""Polite HTTP client for public-records scraping.

- per-host minimum interval between requests (rate limiting)
- retries with exponential backoff on 429/5xx and network errors
- optional on-disk response cache with TTL (keyed by method + URL + params + body)
- honest, identifying User-Agent
- per-host success/failure counters that agents report into agent_runs
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

from core.config import CACHE_DIR, settings, user_agent

log = logging.getLogger(__name__)

RETRY_STATUSES = {429, 500, 502, 503, 504}


@dataclass
class HostStats:
    requests: int = 0
    cache_hits: int = 0
    failures: int = 0
    statuses: dict[int, int] = field(default_factory=lambda: defaultdict(int))

    def as_dict(self) -> dict[str, Any]:
        return {"requests": self.requests, "cache_hits": self.cache_hits,
                "failures": self.failures, "statuses": dict(self.statuses)}


class FetchError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class PoliteSession:
    """A requests.Session wrapper. One instance per agent run is typical."""

    _host_last_request: dict[str, float] = {}
    _lock = threading.Lock()

    def __init__(self, *, min_interval: float | None = None, cache_ttl_hours: float | None = None,
                 timeout: float | None = None, max_retries: int | None = None,
                 cache_dir: Path = CACHE_DIR / "http"):
        cfg = settings().get("scrapers", {})
        self.min_interval = cfg.get("min_interval_seconds", 1.5) if min_interval is None else min_interval
        self.cache_ttl = 3600 * (cfg.get("cache_ttl_hours", 12) if cache_ttl_hours is None else cache_ttl_hours)
        self.timeout = cfg.get("timeout_seconds", 45) if timeout is None else timeout
        self.max_retries = cfg.get("max_retries", 3) if max_retries is None else max_retries
        self.cache_dir = cache_dir
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": user_agent(),
            "Accept-Language": "en-US,en;q=0.9",
        })
        self.stats: dict[str, HostStats] = defaultdict(HostStats)

    # -- public API -----------------------------------------------------------------------------
    def get(self, url: str, *, params: dict | None = None, cache: bool = True, **kw) -> requests.Response:
        return self.request("GET", url, params=params, cache=cache, **kw)

    def post(self, url: str, *, data: dict | None = None, cache: bool = True, **kw) -> requests.Response:
        return self.request("POST", url, data=data, cache=cache, **kw)

    def get_json(self, url: str, *, params: dict | None = None, cache: bool = True) -> Any:
        resp = self.get(url, params=params, cache=cache)
        try:
            return resp.json()
        except ValueError as exc:
            raise FetchError(f"Non-JSON response from {url}: {resp.text[:200]}", resp.status_code) from exc

    def request(self, method: str, url: str, *, params: dict | None = None, data: dict | None = None,
                cache: bool = True, **kw) -> requests.Response:
        host = urlparse(url).netloc
        key = self._cache_key(method, url, params, data)
        if cache and self.cache_ttl > 0:
            cached = self._cache_read(key)
            if cached is not None:
                self.stats[host].cache_hits += 1
                return cached

        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self._throttle(host)
            self.stats[host].requests += 1
            try:
                resp = self.session.request(method, url, params=params, data=data,
                                            timeout=self.timeout, **kw)
            except requests.RequestException as exc:
                last_exc = exc
                self.stats[host].failures += 1
                log.warning("%s %s failed (attempt %d): %s", method, url, attempt + 1, exc)
                time.sleep(min(2 ** attempt * 2, 30))
                continue
            self.stats[host].statuses[resp.status_code] += 1
            if resp.status_code in RETRY_STATUSES and attempt < self.max_retries:
                wait = self._retry_after(resp) or min(2 ** attempt * 3, 60)
                log.warning("%s %s -> %s, retrying in %ss", method, url, resp.status_code, wait)
                time.sleep(wait)
                continue
            if resp.status_code >= 400:
                self.stats[host].failures += 1
                raise FetchError(f"{method} {url} -> HTTP {resp.status_code}", resp.status_code)
            if cache and self.cache_ttl > 0:
                self._cache_write(key, resp)
            return resp
        raise FetchError(f"{method} {url} failed after retries: {last_exc}")

    def stats_summary(self) -> dict[str, dict]:
        return {host: s.as_dict() for host, s in self.stats.items()}

    # -- internals ------------------------------------------------------------------------------
    def _throttle(self, host: str) -> None:
        # Reserve the next slot for this host under the lock, sleep outside it, so threads
        # hitting different hosts never block each other.
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._host_last_request.get(host, 0.0) + self.min_interval)
            self._host_last_request[host] = slot
        if slot > now:
            time.sleep(slot - now)

    @staticmethod
    def _retry_after(resp: requests.Response) -> float | None:
        value = resp.headers.get("Retry-After")
        try:
            return min(float(value), 120) if value else None
        except ValueError:
            return None

    @staticmethod
    def _cache_key(method: str, url: str, params: dict | None, data: dict | None) -> str:
        blob = json.dumps([method, url, params or {}, data or {}], sort_keys=True, default=str)
        return hashlib.sha256(blob.encode()).hexdigest()

    def _cache_path(self, key: str) -> Path:
        return self.cache_dir / key[:2] / f"{key}.json"

    def _cache_read(self, key: str) -> requests.Response | None:
        path = self._cache_path(key)
        if not path.exists() or time.time() - path.stat().st_mtime > self.cache_ttl:
            return None
        try:
            payload = json.loads(path.read_text())
        except (OSError, ValueError):
            return None
        resp = requests.Response()
        resp.status_code = payload["status"]
        resp._content = payload["body"].encode(payload.get("encoding") or "utf-8")
        resp.encoding = payload.get("encoding") or "utf-8"
        resp.url = payload["url"]
        resp.headers.update(payload.get("headers", {}))
        return resp

    def _cache_write(self, key: str, resp: requests.Response) -> None:
        path = self._cache_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"status": resp.status_code, "url": resp.url, "encoding": resp.encoding or "utf-8",
                   "headers": {"Content-Type": resp.headers.get("Content-Type", "")},
                   "body": resp.text}
        path.write_text(json.dumps(payload))
