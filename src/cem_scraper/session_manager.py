"""Access management: EULA, session entry, and authorized-session lifecycle.

Three distinct things must not be confused:

- the *browser/HTTP session* (cookie jar, e.g. ``PHPSESSID``),
- the *examination session* (``sesja`` code such as ``20031``),
- *CAPTCHA verification* (required each time an examination session is
  entered through the official form).

An ``AccessProvider`` turns "I want examination session X" into an
``AccessGrant`` — a cookie jar (or transport) belonging to a session that the
site itself authorized through its official form. The scraper never forges
authorization state; when a grant is missing or expired it stops and reports.
"""

from __future__ import annotations

import asyncio
import logging
import re
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx

from cem_scraper.captcha import CaptchaProvider, CaptchaUnavailable
from cem_scraper.client import CemClient, ClientError, sanitize_snapshot
from cem_scraper.config import (
    EULA_PATH,
    EXAM_TYPE,
    FORM_PATH,
    QUESTION_PATH,
    AccessProviderName,
    Settings,
)
from cem_scraper.metrics import Metrics
from cem_scraper.models import ExamSession
from cem_scraper.parser import (
    PageKind,
    classify_page,
    detect_session_code,
    make_soup,
    parse_entry_form,
)
from cem_scraper.throttle import TrafficControl

log = logging.getLogger(__name__)


QUESTION_URL_RE = re.compile(r".*/wyswietl_pytania\w*\.php.*")


def describe_form(settings: Settings, html: str, session: ExamSession) -> None:
    """Log which sessions the official form offers; save it when snapshots are enabled."""
    if settings.save_html_dir:
        settings.save_html_dir.mkdir(parents=True, exist_ok=True)
        path = settings.save_html_dir / f"form_{session.session_code}.html"
        path.write_text(sanitize_snapshot(html), encoding="utf-8")
    options = parse_entry_form(html).session_options
    if not options:
        log.info("the session form has no static session list (it may load dynamically)")
        return
    codes = sorted(options)
    log.info("the form offers %d sessions: %s ... %s", len(codes), ", ".join(codes[:3]), ", ".join(codes[-3:]))
    if session.session_code not in options:
        log.warning("session %s is NOT offered by this form", session.label)


def verify_shown_session(html: str, session: ExamSession) -> None:
    """The question page must belong to the requested session (header or hidden field)."""
    soup = make_soup(html)
    shown = detect_session_code(soup)
    hidden = soup.select_one("input[name=sesja]")
    if shown is None and hidden is not None:
        shown = str(hidden.get("value") or "") or None
    if shown is not None and shown != session.session_code:
        raise AccessBlocked(
            f"the page shows exam session {shown}, expected {session.session_code} ({session.label}); "
            "choose the right session in the form"
        )


class AccessBlocked(Exception):
    """Authorized access could not be obtained; collection for this session must stop."""


class OpenCancelled(Exception):
    """The run stopped while this session was waiting for its turn to be authorized."""


@dataclass
class AccessGrant:
    session_code: str
    provider: str
    obtained_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    first_page_html: str | None = field(default=None, repr=False)
    transport: httpx.AsyncBaseTransport | None = field(default=None, repr=False)
    # Absolute URL of the question page the site served after the form submission.
    question_url: str | None = None


class AccessProvider(ABC):
    name: str
    interactive: bool = False

    def base_transport(self) -> httpx.AsyncBaseTransport | None:
        return None

    @abstractmethod
    async def acquire(self, session: ExamSession, client: CemClient) -> AccessGrant:
        """Authorize ``client`` for ``session`` (populate its cookie jar) or raise ``AccessBlocked``."""

    async def close(self) -> None:
        return None


# --------------------------------------------------------------------------- offline fixtures


class FixtureAccessProvider(AccessProvider):
    """Serves local HTML fixtures through an in-process transport. Never touches the network."""

    name = "fixtures"

    def __init__(self, fixtures_dir: Path) -> None:
        self.fixtures_dir = fixtures_dir
        if not fixtures_dir.is_dir():
            raise AccessBlocked(f"fixtures directory {fixtures_dir} does not exist")
        self.requests: list[dict[str, str]] = []

    def _handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(QUESTION_PATH):
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            self.requests.append(form)
            page = self.fixtures_dir / f"{form.get('sesja')}_{form.get('nr_pytania')}.html"
            if not page.exists():
                page = self.fixtures_dir / "_no_question.html"
            if not page.exists():
                return httpx.Response(404)
            return httpx.Response(200, content=page.read_bytes(), headers={"Content-Type": "text/html"})
        return httpx.Response(200, html="<html><body>offline fixture server</body></html>")

    def base_transport(self) -> httpx.AsyncBaseTransport:
        return httpx.MockTransport(self._handler)

    async def acquire(self, session: ExamSession, client: CemClient) -> AccessGrant:
        return AccessGrant(session_code=session.session_code, provider=self.name)


# --------------------------------------------------------------------------- automated HTTP form


class HttpFormAccessProvider(AccessProvider):
    """Fully automated entry: EULA page -> acceptance -> session form -> CAPTCHA -> question 1.

    Proceeds only when EULA acceptance is explicitly configured
    (``CEM_ACCEPT_EULA=true``) and a CAPTCHA provider is available (e.g. 2Captcha
    with a key and budget); otherwise it stops before contacting CEM. The solver
    is told whether CEM accepted the token (refund on rejection).
    """

    name = "http_form"

    def __init__(self, settings: Settings, captcha: CaptchaProvider) -> None:
        self.settings = settings
        self.captcha = captcha

    async def acquire(self, session: ExamSession, client: CemClient) -> AccessGrant:
        if not self.settings.accept_eula:
            raise AccessBlocked(
                "EULA acceptance is not configured. Read the terms at "
                f"{self.settings.url(EULA_PATH)} and set CEM_ACCEPT_EULA=true to accept them."
            )
        if not self.captcha.available:
            raise AccessBlocked(
                f"automated session entry needs an authorized CAPTCHA provider; '{self.captcha.name}' is not "
                "available. Use CEM_ACCESS_PROVIDER=interactive_browser."
            )
        try:
            await client.get(EULA_PATH)
            form_page = await client.post(FORM_PATH, {"akceptacja": "tak"})
            describe_form(self.settings, form_page.html, session)
            form = parse_entry_form(form_page.html)
            verify_form_supports(form.session_options, form.specialization_options, session)
            if not form.recaptcha_sitekey:
                raise AccessBlocked("session-entry form has no reCAPTCHA sitekey; page layout changed?")
            fields = dict(form.fields) | {
                "sesja": session.session_code,
                "typ": EXAM_TYPE,
                "specjalizacja": session.specialization_code,
                "nr_pytania": "1",
            }
            solution = await self.captcha.solve(site_key=form.recaptcha_sitekey, page_url=form_page.url)
            fields["g-recaptcha-response"] = solution.token
            try:
                first = await client.post(form.action or QUESTION_PATH, fields)
            finally:
                fields.pop("g-recaptcha-response", None)  # single use, never kept
        except CaptchaUnavailable as exc:
            raise AccessBlocked(str(exc)) from exc
        except ClientError as exc:
            raise AccessBlocked(f"session entry failed: {exc}") from exc
        kind = classify_page(make_soup(first.html))
        if kind is PageKind.QUESTION:
            await self.captcha.report(solution, accepted=True)
        elif kind is PageKind.ENTRY_FORM:  # the form came back: the token was rejected
            await self.captcha.report(solution, accepted=False)
        if kind is not PageKind.QUESTION:
            raise AccessBlocked(f"session entry for {session.label} was not accepted (got {kind.value} page)")
        verify_shown_session(first.html, session)
        log.info("session %s entered automatically", session.label)
        return AccessGrant(
            session_code=session.session_code, provider=self.name, first_page_html=first.html, question_url=first.url
        )

    async def close(self) -> None:
        await self.captcha.close()


def verify_form_supports(session_options: dict[str, str], spec_options: dict[str, str], session: ExamSession) -> None:
    """Check the session code / specialization against the form's own <select> options when present."""
    if session_options and session.session_code not in session_options:
        raise AccessBlocked(
            f"session code {session.session_code} is not offered by the official form "
            f"(YYYY1/YYYY2 convention may not hold); available: {sorted(session_options)[:10]}..."
        )
    if spec_options and session.specialization_code not in spec_options:
        log.warning(
            "specialization %s not in the static form options; it may be loaded dynamically",
            session.specialization_code,
        )


# --------------------------------------------------------------------------- interactive browser


class PlaywrightRequestTransport(httpx.AsyncBaseTransport):
    """HTTPX transport that sends requests through a Playwright browser context.

    Requests share the browser context's cookie store, i.e. exactly the session
    the human authorized. Used when ``CEM_BROWSER_CONTEXT_REQUESTS=true``.
    """

    _DROP = frozenset({"host", "content-length", "cookie", "connection", "accept-encoding"})

    def __init__(self, context: Any, timeout_ms: float) -> None:
        self._context = context
        self._timeout_ms = timeout_ms

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        body = await request.aread()
        headers = {k: v for k, v in request.headers.items() if k.lower() not in self._DROP}
        response = await self._context.request.fetch(
            str(request.url), method=request.method, headers=headers, data=body or None, timeout=self._timeout_ms
        )
        content = await response.body()
        out_headers = [
            (h["name"], h["value"])
            for h in response.headers_array
            if h["name"].lower() not in ("content-encoding", "content-length", "transfer-encoding")
        ]
        return httpx.Response(response.status, headers=out_headers, content=content, request=request)


class InteractiveBrowserAccessProvider(AccessProvider):
    """A human solves the reCAPTCHA in a visible Chromium window; the scraper does the rest.

    Flow per examination session: open the official form (accepting the EULA
    only when configured, otherwise waiting for the user to do it), pre-fill
    session/type/specialization/question 1, then wait until the *user* solves
    the reCAPTCHA and submits. The resulting authorized session is handed to
    HTTPX (cookies, or the browser context itself as transport).
    """

    name = "interactive_browser"
    interactive = True

    def __init__(self, settings: Settings, *, use_context_requests: bool = False) -> None:
        self.settings = settings
        self.use_context_requests = use_context_requests
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None

    async def _ensure_context(self) -> Any:
        if self._context is not None:
            return self._context
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:  # pragma: no cover - depends on optional extra
            raise AccessBlocked(
                "Playwright is not installed: `uv sync --extra browser && uv run playwright install chromium`"
            ) from exc
        self._playwright = await async_playwright().start()
        chromium = self._playwright.chromium
        if self.settings.browser_profile_dir:
            self._context = await chromium.launch_persistent_context(
                str(self.settings.browser_profile_dir), headless=self.settings.browser_headless
            )
        else:
            self._browser = await chromium.launch(headless=self.settings.browser_headless)
            self._context = await self._browser.new_context()
        return self._context

    async def acquire(self, session: ExamSession, client: CemClient) -> AccessGrant:
        context = await self._ensure_context()
        page = await context.new_page()
        timeout_ms = self.settings.browser_wait_timeout * 1000
        try:
            # The PES entry point. Opening form_pytania_pes.php directly (without the PES
            # EULA) sends the user to the general eula.php / form_pytania.php instead.
            await page.goto(self.settings.url(EULA_PATH))
            eula = page.locator("form:has(input[name=akceptacja])")
            if await eula.count():
                if self.settings.accept_eula:
                    log.info("accepting the terms of use (CEM_ACCEPT_EULA=true)")
                    box = eula.locator("input[name=akceptacja][type=checkbox]")  # live page: a checkbox
                    if await box.count():
                        await box.check()
                    await eula.locator("[type=submit]").first.click()
                else:
                    log.warning(
                        "ACTION REQUIRED: read the terms of use in the browser window, tick the box "
                        "and click 'Wyświetl pytania'."
                    )
            await page.wait_for_selector("select[name=sesja], .g-recaptcha", timeout=timeout_ms)
            describe_form(self.settings, await page.content(), session)
            await self._prefill(page, session)
            log.warning(
                "ACTION REQUIRED: in the browser window, check %s / Psychiatria / question 1, "
                "solve the reCAPTCHA and submit. Waiting up to %.0fs...",
                session.label,
                self.settings.browser_wait_timeout,
            )
            await page.wait_for_url(QUESTION_URL_RE, timeout=timeout_ms)
            await page.wait_for_load_state("domcontentloaded")
            html = await page.content()
            question_url = page.url
        except AccessBlocked:
            raise
        except Exception as exc:  # Playwright timeouts, closed window, navigation errors
            raise AccessBlocked(f"interactive authorization for {session.label} did not complete: {exc}") from exc
        finally:
            await page.close()

        if classify_page(make_soup(html)) is not PageKind.QUESTION:
            raise AccessBlocked("browser did not reach a question page after the form submission")
        verify_shown_session(html, session)

        transport = None
        if self.use_context_requests:
            transport = PlaywrightRequestTransport(context, self.settings.read_timeout * 1000)
        else:
            # Hosts this session talks to: the configured site and wherever the form landed
            # (www.cem.edu.pl vs cem.edu.pl). Third-party cookies (e.g. Google's) are not copied.
            hosts = {h for h in (urlsplit(self.settings.base_url).hostname, urlsplit(question_url).hostname) if h}
            for cookie in await context.cookies():
                domain = cookie["domain"].lstrip(".")
                if any(host == domain or host.endswith("." + domain) for host in hosts):
                    client.cookies.set(cookie["name"], cookie["value"], domain=cookie["domain"], path=cookie["path"])
        log.info("session %s authorized interactively", session.label)
        return AccessGrant(
            session_code=session.session_code,
            provider=self.name,
            first_page_html=html,
            transport=transport,
            question_url=question_url,
        )

    async def _prefill(self, page: Any, session: ExamSession) -> None:
        """Best-effort pre-selection; the user can always correct it by hand."""
        for name, value in (
            ("sesja", session.session_code),
            ("typ", EXAM_TYPE),
            ("specjalizacja", session.specialization_code),
        ):
            select = page.locator(f"select[name={name}]")
            try:
                if await select.count():
                    await page.wait_for_function(
                        "([n, v]) => !!document.querySelector(`select[name=${n}] option[value='${v}']`)",
                        arg=[name, value],
                        timeout=10_000,
                    )
                    await select.select_option(value)
            except Exception as exc:
                log.warning("could not pre-select %s=%s (%s); please choose it manually", name, value, exc)
        number = page.locator("input[name=nr_pytania]")
        try:
            if await number.count():
                await number.fill("1")
        except Exception:
            log.warning("could not pre-fill nr_pytania")

    async def close(self) -> None:
        if self._context is not None:
            await self._context.close()
        if self._browser is not None:
            await self._browser.close()
        if self._playwright is not None:
            await self._playwright.stop()
        self._context = self._browser = self._playwright = None


# --------------------------------------------------------------------------- manager


def build_access_provider(settings: Settings, captcha: CaptchaProvider) -> AccessProvider:
    match settings.access_provider:
        case AccessProviderName.FIXTURES:
            return FixtureAccessProvider(settings.fixtures_dir)
        case AccessProviderName.HTTP_FORM:
            return HttpFormAccessProvider(settings, captcha)
        case AccessProviderName.INTERACTIVE_BROWSER:
            return InteractiveBrowserAccessProvider(settings, use_context_requests=settings.browser_context_requests)
    raise AccessBlocked(f"unknown access provider {settings.access_provider!r}")


@dataclass
class AuthorizedSession:
    session: ExamSession
    client: CemClient
    grant: AccessGrant
    reauthorizations: int = 0


class SessionManager:
    """Hands out authorized ``CemClient``s, one per examination session.

    All clients share one ``TrafficControl``, so collecting sessions in parallel
    never multiplies the request rate. Authorizations are serialized: at most one
    official-form entry (one CAPTCHA for the user) is in progress at a time.
    """

    def __init__(
        self,
        settings: Settings,
        provider: AccessProvider,
        metrics: Metrics,
        traffic: TrafficControl | None = None,
    ) -> None:
        self.settings = settings
        self.provider = provider
        self.metrics = metrics
        self.traffic = traffic or TrafficControl(settings, metrics)
        self._authorizing = asyncio.Lock()

    def _new_client(self, transport: httpx.AsyncBaseTransport | None) -> CemClient:
        return CemClient(self.settings, self.metrics, transport=transport, traffic=self.traffic)

    async def open(self, session: ExamSession, *, abort_if: Callable[[], bool] | None = None) -> AuthorizedSession:
        async with self._authorizing:
            if abort_if is not None and abort_if():  # don't ask for another CAPTCHA for a stopping run
                raise OpenCancelled(f"run stopped before {session.label} was authorized")
            client = self._new_client(self.provider.base_transport())
            try:
                grant = await self.provider.acquire(session, client)
            except BaseException:
                await client.aclose()
                raise
        if grant.transport is not None:
            await client.aclose()
            client = self._new_client(grant.transport)
        if grant.question_url:
            client.question_url = grant.question_url
            log.info("questions for %s are requested from %s", session.label, grant.question_url)
        return AuthorizedSession(session=session, client=client, grant=grant)

    async def reauthorize(self, auth: AuthorizedSession) -> AuthorizedSession:
        """Obtain a fresh grant after expiry, within the configured limit; else ``AccessBlocked``."""
        if auth.reauthorizations >= self.settings.max_reauthorizations_per_session:
            raise AccessBlocked(
                f"authorization for {auth.session.label} expired and the re-authorization limit "
                f"({self.settings.max_reauthorizations_per_session}) is reached"
            )
        log.warning("authorization for %s expired; re-authorizing", auth.session.label)
        await auth.client.aclose()
        fresh = await self.open(auth.session)
        fresh.reauthorizations = auth.reauthorizations + 1
        return fresh

    async def release(self, auth: AuthorizedSession) -> None:
        await auth.client.aclose()

    async def close(self) -> None:
        await self.provider.close()
