"""Shared HTTP plumbing: retries, token-bucket rate limiting, TTL cache.

One implementation, used by every client, so backoff and rate limits are
consistent and there is exactly one place to instrument.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any

import httpx

from app.pipeline.source_health import record_http_failure, record_http_ok

log = logging.getLogger(__name__)

RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


class ClientError(Exception):
    """Raised on a non-retryable API failure. Callers treat this as 'no data',
    never as 'zero'."""


class RateLimiter:
    """Simple async token bucket."""

    def __init__(self, rps: float) -> None:
        self.rps = max(rps, 0.1)
        self.capacity = max(1.0, self.rps)
        self._tokens = self.capacity
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rps)
            self._last = now
            if self._tokens < 1.0:
                wait = (1.0 - self._tokens) / self.rps
                await asyncio.sleep(wait)
                self._tokens = 0.0
                self._last = time.monotonic()
            else:
                self._tokens -= 1.0


class TTLCache:
    def __init__(self, ttl_s: float = 60.0, maxsize: int = 2048) -> None:
        self.ttl = ttl_s
        self.maxsize = maxsize
        self._d: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Any | None:
        hit = self._d.get(key)
        if not hit:
            return None
        expires, value = hit
        if time.monotonic() > expires:
            self._d.pop(key, None)
            return None
        return value

    def set(self, key: str, value: Any) -> None:
        if len(self._d) >= self.maxsize:
            self._d.clear()
        self._d[key] = (time.monotonic() + self.ttl, value)


class BaseHTTPClient:
    name = "base"

    def __init__(
        self,
        base_url: str,
        *,
        timeout_s: float = 20.0,
        max_retries: int = 3,
        backoff_base_s: float = 1.0,
        rps: float = 5.0,
        cache_ttl_s: float = 30.0,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.backoff_base_s = backoff_base_s
        self.limiter = RateLimiter(rps)
        self.cache = TTLCache(cache_ttl_s)
        self._headers = headers or {}
        self._client: httpx.AsyncClient | None = None

    async def _ensure(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(self.timeout_s),
                headers=self._headers,
                follow_redirects=True,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    def auth_headers(self, method: str, path: str, body: str) -> dict[str, str]:
        """Overridden by clients that sign requests."""
        return {}

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        cache_key: str | None = None,
    ) -> Any:
        if cache_key:
            cached = self.cache.get(cache_key)
            if cached is not None:
                return cached

        client = await self._ensure()
        body_str = ""
        if json_body is not None:
            import json as _json

            body_str = _json.dumps(json_body, separators=(",", ":"))

        # Signature must cover the query string exactly as sent.
        sign_path = path
        if params:
            sign_path = f"{path}?{httpx.QueryParams(params)}"

        last_err: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                await self.limiter.acquire()
                headers = self.auth_headers(method.upper(), sign_path, body_str)
                resp = await client.request(
                    method.upper(), path, params=params, json=json_body, headers=headers
                )
                if resp.status_code in RETRYABLE_STATUS:
                    raise httpx.HTTPStatusError(
                        f"{self.name} retryable {resp.status_code}", request=resp.request, response=resp
                    )
                if resp.status_code >= 400:
                    err = ClientError(
                        f"{self.name} {method} {path} -> {resp.status_code}: {resp.text[:300]}"
                    )
                    record_http_failure(self.name, method, path, err)
                    raise err
                data = resp.json() if resp.content else None
                if cache_key and data is not None:
                    self.cache.set(cache_key, data)
                record_http_ok(self.name, method, path)
                return data
            except (httpx.HTTPStatusError, httpx.TransportError, httpx.TimeoutException) as e:
                last_err = e
                if attempt >= self.max_retries:
                    break
                delay = self.backoff_base_s * (2**attempt) + random.uniform(0, 0.25)
                log.warning("%s %s %s failed (%s), retry %d in %.2fs", self.name, method, path, e, attempt + 1, delay)
                await asyncio.sleep(delay)

        attempts = "no retry" if self.max_retries == 0 else f"{self.max_retries} retries"
        err = ClientError(f"{self.name} {method} {path} failed ({attempts}): {last_err}")
        record_http_failure(self.name, method, path, err)
        raise err


def pick(payload: Any, *candidates: str, default: Any = None) -> Any:
    """Tolerant field extraction across candidate key names / dotted paths.

    Upstream response shapes are not fully verified in this environment (see
    docs/ENDPOINTS.md), so the normalizer looks for several plausible key names
    and returns `default` — normally None — when none is present. It never
    invents a value.
    """
    if payload is None:
        return default
    for cand in candidates:
        cur: Any = payload
        ok = True
        for part in cand.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
                cur = cur[int(part)]
            else:
                ok = False
                break
        if ok and cur is not None and cur != "":
            return cur
    return default


def to_float(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):  # NaN / inf
        return None
    return f


def to_int(v: Any) -> int | None:
    f = to_float(v)
    return int(f) if f is not None else None
