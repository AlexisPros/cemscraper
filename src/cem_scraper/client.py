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
from cem_scraper.parser import attr, decode_html
from cem_scraper.throttle import TrafficControl

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
        name = (attr(element, "name") or attr(element, "id")).lower()
        if any(hint in name for hint in SENSITIVE_INPUT_HINTS):
            if element.name == "textarea":
                element.string = ""
            else:
                element["value"] = ""
    return str(soup)


class CemClient:
    """HTTP client for one authorized cookie jar.

    All server-facing limits (request spacing, in-flight cap, circuit breaker)
    come from ``traffic``, which a ``SessionManager`` shares between the clients
    of a run. A standalone client gets its own.
    """

    def __init__(
        self,
        settings: Settings,
        metrics: Metrics,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        traffic: TrafficControl | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.settings = settings
        self.metrics = metrics
        self.traffic = traffic or TrafficControl(settings, metrics, sleep=sleep)
        # Replaced by the endpoint the site actually used for the authorized session
        # (e.g. wyswietl_pytania.php vs wyswietl_pytania_pes.php, with or without www).
        self.question_url: str = QUESTION_PATH
        self._sleep = sleep
        self._http = httpx.AsyncClient(
            base_url=settings.base_url,
            transport=transport,
            timeout=httpx.Timeout(settings.read_timeout, connect=settings.connect_timeout),
            limits=httpx.Limits(
                max_connections=settings.max_concurrency, max_keepalive_connections=settings.max_concurrency
            ),
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

    async def request(
        self, method: str, path: str, data: Mapping[str, str] | None = None, *, retries: int | None = None
    ) -> PageResponse:
        """Send with retries. ``retries=0`` for requests that must not be repeated (a CAPTCHA token)."""
        max_retries = self.settings.max_retries if retries is None else retries
        breaker = self.traffic.breaker
        if breaker.open:
            raise ServerUnavailable("circuit breaker open: too many consecutive server failures")
        last_error = "unknown error"
        for attempt in range(max_retries + 1):
            delay: float | None = None
            ticket = await self.traffic.concurrency.acquire()
            latency: float | None = None
            overloaded = False
            try:
                await self.traffic.limiter.acquire()
                started = time.monotonic()
                try:
                    response = await self._http.request(method, path, data=data)
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    elapsed = time.monotonic() - started
                    overloaded = True
                    self.metrics.record_request(elapsed, None, ok=False)
                    last_error = f"{type(exc).__name__} after {elapsed:.1f}s"
                    log.warning("%s %s failed (attempt %d): %s", method, path, attempt + 1, last_error)
                else:
                    latency = time.monotonic() - started
                    status = response.status_code
                    ok = 200 <= status < 300
                    self.metrics.record_request(latency, status, ok=ok)
                    log.debug("%s %s -> %d in %.2fs", method, path, status, latency)
                    if ok:
                        breaker.success()
                        return PageResponse(
                            url=str(response.url),
                            status_code=status,
                            html=decode_html(response.content, response.charset_encoding),
                            elapsed=latency,
                        )
                    if status in (401, 403):
                        raise AccessDenied(f"HTTP {status} for {path}")
                    if status not in RETRYABLE_STATUS:
                        raise UnexpectedStatus(status, path)
                    overloaded = True
                    last_error = f"HTTP {status}"
                    delay = parse_retry_after(response.headers.get("Retry-After"))
                    if delay is not None and delay > self.settings.max_retry_after:
                        raise ServerThrottled(f"server asked to wait {delay:.0f}s (Retry-After) for {path}")
                    log.warning("%s %s -> HTTP %d (attempt %d)", method, path, status, attempt + 1)
            finally:
                self.traffic.concurrency.release(ticket, latency, overloaded=overloaded)
            if attempt == max_retries:
                break
            self.metrics.record_retry()
            if delay is not None:
                # Retry-After applies to every client of the run, not just this request:
                # the shared limiter now holds back every request start, including this retry.
                self.traffic.limiter.defer(delay)
                log.info("server asked to wait %.1fs; pausing all requests", delay)
            else:
                wait = self._backoff(attempt)
                log.info("retrying %s %s in %.1fs", method, path, wait)
                await self._sleep(wait)

        if breaker.failure():
            raise ServerUnavailable(
                f"{breaker.consecutive_failures} consecutive requests failed after retries; last: {last_error}"
            )
        raise RequestFailed(f"{method} {path} failed after {max_retries + 1} attempt(s): {last_error}")

    async def get(self, path: str) -> PageResponse:
        return await self.request("GET", path)

    async def post(self, path: str, data: Mapping[str, str], *, retries: int | None = None) -> PageResponse:
        return await self.request("POST", path, data=data, retries=retries)

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
        page = await self.post(self.question_url, self.question_payload(session, number))
        if self.settings.save_html_dir:
            save_snapshot(self.settings.save_html_dir, session.session_code, number, page.html)
        return page


def save_snapshot(directory: Path, session_code: str, number: int, html: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{session_code}_{number}.html"
    path.write_text(sanitize_snapshot(html), encoding="utf-8")
    return path
