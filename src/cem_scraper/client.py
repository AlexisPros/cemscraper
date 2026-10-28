"""Asynchronous HTTP client for CEM pages.

Responsibilities: connection pooling, a global request rate limit, a
concurrency cap, explicit timeouts, retries with exponential backoff + full
jitter for transient failures, ``Retry-After`` handling and a circuit breaker
that stops the run on repeated server failures. It never logs cookies,
CAPTCHA tokens or request bodies.

The client does *not* create authorization. It only carries the cookie jar of
a session that an ``AccessProvider`` authorized.
"""

from __future__ import annotations

import asyncio
import email.utils
import logging
import random
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx
from bs4 import BeautifulSoup

from cem_scraper.config import EXAM_TYPE, QUESTION_PATH, Settings
from cem_scraper.metrics import Metrics
from cem_scraper.models import ExamSession
from cem_scraper.parser import decode_html

log = logging.getLogger(__name__)

RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}
SENSITIVE_INPUT_HINTS = ("captcha", "token", "csrf", "sess", "auth_key")


class ClientError(Exception):
    """Base class for HTTP-level failures."""


class AccessDenied(ClientError):
    """HTTP 401/403: the server refuses access. Never retried."""


class ServerThrottled(ClientError):
    """The server asked us to wait longer than we are configured to."""


class ServerUnavailable(ClientError):
    """Circuit breaker open after repeated failures; the run must stop."""


class RequestFailed(ClientError):
    """A request failed after exhausting retries (transient error)."""


class UnexpectedStatus(ClientError):
    """Non-retryable, non-auth HTTP status (e.g. 404)."""

    def __init__(self, status: int, url: str) -> None:
        super().__init__(f"HTTP {status} for {url}")
        self.status = status


@dataclass
class PageResponse:
    url: str
    status_code: int
    html: str
    elapsed: float


class RateLimiter:
    """Spaces request *starts* at least ``1 / rate`` seconds apart."""

    def __init__(
        self,
        rate: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._interval = 1.0 / rate
        self._clock = clock
        self._sleep = sleep
        self._lock = asyncio.Lock()
        self._next_slot = 0.0

    async def acquire(self) -> None:
        async with self._lock:
            now = self._clock()
            wait = self._next_slot - now
            self._next_slot = max(now, self._next_slot) + self._interval
        if wait > 0:
            await self._sleep(wait)


def parse_retry_after(value: str | None, now: datetime | None = None) -> float | None:
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - (now or datetime.now(UTC))).total_seconds())


def sanitize_snapshot(html: str) -> str:
    """Blank values of hidden inputs that could carry tokens before saving HTML to disk."""
    soup = BeautifulSoup(html, "lxml")
    for element in soup.find_all(["input", "textarea"]):
        name = (element.get("name") or element.get("id") or "").lower()
        if any(hint in name for hint in SENSITIVE_INPUT_HINTS):
            if element.name == "textarea":
                element.string = ""
            else:
                element["value"] = ""
    return str(soup)


class CemClient:
    def __init__(
        self,
        settings: Settings,
        metrics: Metrics,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rate_limiter: RateLimiter | None = None,
    ) -> None:
        self.settings = settings
        self.metrics = metrics
        self._sleep = sleep
        self._limiter = rate_limiter or RateLimiter(settings.requests_per_second, sleep=sleep)
        self._semaphore = asyncio.Semaphore(settings.concurrency)
        self._consecutive_failures = 0
        self._breaker_open = False
        self._http = httpx.AsyncClient(
            base_url=settings.base_url,
            transport=transport,
            timeout=httpx.Timeout(settings.read_timeout, connect=settings.connect_timeout),
            limits=httpx.Limits(max_connections=settings.concurrency, max_keepalive_connections=settings.concurrency),
            headers={"User-Agent": settings.effective_user_agent(), "Accept-Language": "pl,en;q=0.5"},
            follow_redirects=True,
        )

    @property
    def cookies(self) -> httpx.Cookies:
        return self._http.cookies

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> CemClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    def _backoff(self, attempt: int) -> float:
        cap = min(self.settings.backoff_max, self.settings.backoff_base * (2**attempt))
        return random.uniform(0, cap)  # full jitter

    async def request(self, method: str, path: str, data: Mapping[str, str] | None = None) -> PageResponse:
        if self._breaker_open:
            raise ServerUnavailable("circuit breaker open: too many consecutive server failures")
        last_error = "unknown error"
        for attempt in range(self.settings.max_retries + 1):
            await self._limiter.acquire()
            delay: float | None = None
            async with self._semaphore:
                started = time.monotonic()
                try:
                    response = await self._http.request(method, path, data=data)
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    elapsed = time.monotonic() - started
                    self.metrics.record_request(elapsed, None, ok=False)
                    last_error = f"{type(exc).__name__} after {elapsed:.1f}s"
                    log.warning("%s %s failed (attempt %d): %s", method, path, attempt + 1, last_error)
                else:
                    elapsed = time.monotonic() - started
                    status = response.status_code
                    ok = 200 <= status < 300
                    self.metrics.record_request(elapsed, status, ok=ok)
                    log.debug("%s %s -> %d in %.2fs", method, path, status, elapsed)
                    if ok:
                        self._consecutive_failures = 0
                        return PageResponse(
                            url=str(response.url),
                            status_code=status,
                            html=decode_html(response.content, response.charset_encoding),
                            elapsed=elapsed,
                        )
                    if status in (401, 403):
                        raise AccessDenied(f"HTTP {status} for {path}")
                    if status not in RETRYABLE_STATUS:
                        raise UnexpectedStatus(status, path)
                    last_error = f"HTTP {status}"
                    delay = parse_retry_after(response.headers.get("Retry-After"))
                    if delay is not None and delay > self.settings.max_retry_after:
                        raise ServerThrottled(f"server asked to wait {delay:.0f}s (Retry-After) for {path}")
                    log.warning("%s %s -> HTTP %d (attempt %d)", method, path, status, attempt + 1)
            if attempt == self.settings.max_retries:
                break
            wait = delay if delay is not None else self._backoff(attempt)
            self.metrics.record_retry()
            log.info("retrying %s %s in %.1fs", method, path, wait)
            await self._sleep(wait)

        self._consecutive_failures += 1
        if self._consecutive_failures >= self.settings.circuit_breaker_threshold:
            self._breaker_open = True
            raise ServerUnavailable(
                f"{self._consecutive_failures} consecutive requests failed after retries; last: {last_error}"
            )
        raise RequestFailed(f"{method} {path} failed after {self.settings.max_retries + 1} attempts: {last_error}")

    async def get(self, path: str) -> PageResponse:
        return await self.request("GET", path)

    async def post(self, path: str, data: Mapping[str, str]) -> PageResponse:
        return await self.request("POST", path, data=data)

    def question_payload(self, session: ExamSession, number: int) -> dict[str, str]:
        """Navigation form fields observed in an authorized browser session.

        ``autoryzacja=1`` is sent only because the site's own navigation form
        sends it; it does not grant access — the authorized session cookie does.
        """
        return {
            "autoryzacja": "1",
            "sesja": session.session_code,
            "typ": EXAM_TYPE,
            "specjalizacja": session.specialization_code,
            "nr_pytania": str(number),
            "gdzieidziesz": self.settings.navigation_direction,
        }

    async def fetch_question(self, session: ExamSession, number: int) -> PageResponse:
        page = await self.post(QUESTION_PATH, self.question_payload(session, number))
        if self.settings.save_html_dir:
            save_snapshot(self.settings.save_html_dir, session.session_code, number, page.html)
        return page


def save_snapshot(directory: Path, session_code: str, number: int, html: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{session_code}_{number}.html"
    path.write_text(sanitize_snapshot(html), encoding="utf-8")
    return path
