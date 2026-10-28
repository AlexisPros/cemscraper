"""Access management. The 'authorized' CAPTCHA provider below is a TEST DOUBLE returning a
dummy string against mocked endpoints; it does not solve anything."""

from __future__ import annotations

from urllib.parse import parse_qs

import httpx
import pytest
import respx

from cem_scraper.captcha import CaptchaProvider, CaptchaSolution, DisabledCaptchaProvider
from cem_scraper.client import CemClient
from cem_scraper.config import Settings
from cem_scraper.metrics import Metrics
from cem_scraper.models import ExamSession
from cem_scraper.session_manager import (
    AccessBlocked,
    AccessGrant,
    AccessProvider,
    FixtureAccessProvider,
    HttpFormAccessProvider,
    SessionManager,
)
from tests.conftest import EDGE, SYNTHETIC, load

BASE = "https://www.cem.edu.pl/pytcem/"


class FakeAuthorizedCaptcha(CaptchaProvider):
    name = "test-double"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.challenges: list[dict[str, object]] = []
        self.reports: list[bool] = []

    @property
    def available(self) -> bool:
        return True

    async def solve(self, *, site_key: str, page_url: str, **challenge: object) -> CaptchaSolution:
        self.calls.append((site_key, page_url))
        self.challenges.append(challenge)
        return CaptchaSolution(token="dummy-test-token", task_id="1")

    async def report(self, solution: CaptchaSolution, *, accepted: bool) -> None:
        self.reports.append(accepted)


def http_settings(settings: Settings, **kw: object) -> Settings:
    return settings.model_copy(update={"accept_eula": True, **kw})


@respx.mock
async def test_http_form_blocked_without_eula_consent(settings: Settings) -> None:
    provider = HttpFormAccessProvider(settings, FakeAuthorizedCaptcha())
    async with CemClient(settings, Metrics()) as client:
        with pytest.raises(AccessBlocked, match="EULA acceptance is not configured"):
            await provider.acquire(ExamSession.from_code("20031"), client)
    assert respx.calls.call_count == 0


@respx.mock
async def test_http_form_blocked_with_disabled_captcha_before_any_request(settings: Settings) -> None:
    provider = HttpFormAccessProvider(http_settings(settings), DisabledCaptchaProvider())
    async with CemClient(settings, Metrics()) as client:
        with pytest.raises(AccessBlocked, match="authorized CAPTCHA provider"):
            await provider.acquire(ExamSession.from_code("20031"), client)
    assert respx.calls.call_count == 0


async def test_disabled_captcha_refuses() -> None:
    from cem_scraper.captcha import CaptchaUnavailable

    with pytest.raises(CaptchaUnavailable):
        await DisabledCaptchaProvider().solve(site_key="k", page_url="u")


@respx.mock
async def test_http_form_flow_with_authorized_provider(settings: Settings) -> None:
    respx.get(BASE + "eula_pes.php").mock(return_value=httpx.Response(200, html=load(EDGE / "eula.html")))
    form_route = respx.post(BASE + "form_pytania_pes.php").mock(
        return_value=httpx.Response(200, html=load(EDGE / "entry_form.html"))
    )
    submit = respx.post(BASE + "wyswietl_pytania_pes.php").mock(
        return_value=httpx.Response(200, html=load(SYNTHETIC / "20031_1.html"), headers={"Set-Cookie": "PHPSESSID=x1"})
    )
    captcha = FakeAuthorizedCaptcha()
    s = http_settings(settings)
    provider = HttpFormAccessProvider(s, captcha)
    async with CemClient(s, Metrics()) as client:
        grant = await provider.acquire(ExamSession.from_code("20031"), client)
        assert client.cookies.get("PHPSESSID") == "x1"
    assert parse_qs(form_route.calls[0].request.content.decode()) == {"akceptacja": ["tak"]}
    sent = {k: v[0] for k, v in parse_qs(submit.calls[0].request.content.decode()).items()}
    assert sent["sesja"] == "20031" and sent["specjalizacja"] == "725" and sent["nr_pytania"] == "1"
    assert sent["krok"] == "2"  # extra hidden fields of the official form are kept
    assert captcha.calls == [("SYNTHETIC-SITEKEY", BASE + "form_pytania_pes.php")]
    assert grant.first_page_html and "Huntingtona" in grant.first_page_html
    assert "dummy-test-token" not in repr(grant)
    assert captcha.reports == [True]


@respx.mock
async def test_http_form_rejects_session_not_offered(settings: Settings) -> None:
    respx.get(BASE + "eula_pes.php").mock(return_value=httpx.Response(200))
    respx.post(BASE + "form_pytania_pes.php").mock(
        return_value=httpx.Response(200, html=load(EDGE / "entry_form.html"))
    )
    provider = HttpFormAccessProvider(http_settings(settings), FakeAuthorizedCaptcha())
    async with CemClient(settings, Metrics()) as client:
        with pytest.raises(AccessBlocked, match="not offered by the official form"):
            await provider.acquire(ExamSession.from_code("20051"), client)


@respx.mock
async def test_http_form_detects_rejected_submission(settings: Settings) -> None:
    respx.get(BASE + "eula_pes.php").mock(return_value=httpx.Response(200))
    respx.post(BASE + "form_pytania_pes.php").mock(
        return_value=httpx.Response(200, html=load(EDGE / "entry_form.html"))
    )
    respx.post(BASE + "wyswietl_pytania_pes.php").mock(
        return_value=httpx.Response(200, html=load(EDGE / "entry_form.html"))
    )
    captcha = FakeAuthorizedCaptcha()
    provider = HttpFormAccessProvider(http_settings(settings), captcha)
    async with CemClient(settings, Metrics()) as client:
        with pytest.raises(AccessBlocked, match="was not accepted"):
            await provider.acquire(ExamSession.from_code("20031"), client)
    assert captcha.reports == [False]  # rejected token reported for a refund


class CountingProvider(AccessProvider):
    name = "counting"

    def __init__(self, fail_after: int | None = None) -> None:
        self.calls = 0
        self.fail_after = fail_after

    async def acquire(self, session: ExamSession, client: CemClient) -> AccessGrant:
        self.calls += 1
        if self.fail_after is not None and self.calls > self.fail_after:
            raise AccessBlocked("no more grants")
        return AccessGrant(session_code=session.session_code, provider=self.name)


async def test_reauthorization_is_limited(settings: Settings) -> None:
    provider = CountingProvider()
    manager = SessionManager(settings.model_copy(update={"max_reauthorizations_per_session": 1}), provider, Metrics())
    auth = await manager.open(ExamSession.from_code("20031"))
    auth = await manager.reauthorize(auth)
    assert provider.calls == 2
    with pytest.raises(AccessBlocked, match="re-authorization limit"):
        await manager.reauthorize(auth)
    await manager.release(auth)


async def test_fixture_provider_serves_files(settings: Settings) -> None:
    provider = FixtureAccessProvider(SYNTHETIC)
    manager = SessionManager(settings, provider, Metrics())
    auth = await manager.open(ExamSession.from_code("20031"))
    page = await auth.client.fetch_question(auth.session, 2)
    missing = await auth.client.fetch_question(auth.session, 99)
    await manager.release(auth)
    assert "Syntetyczne pytanie testowe nr 2" in page.html
    assert "Brak pytania" in missing.html
    assert provider.requests[0]["nr_pytania"] == "2"


async def test_interactive_close_tolerates_a_browser_killed_by_ctrl_c(settings: Settings) -> None:
    """Ctrl+C reaches the browser first; closing it afterwards must not raise."""
    from cem_scraper.session_manager import InteractiveBrowserAccessProvider

    class Gone:
        closed = 0

        async def close(self) -> None:
            Gone.closed += 1
            raise RuntimeError("Target page, context or browser has been closed")

        async def stop(self) -> None:
            Gone.closed += 1
            raise RuntimeError("Connection closed while reading from the driver")

    provider = InteractiveBrowserAccessProvider(settings)
    provider._context, provider._browser, provider._playwright = Gone(), Gone(), Gone()
    await provider.close()  # must not raise
    assert Gone.closed == 3 and provider._context is None


@respx.mock
async def test_http_form_grant_carries_the_form_question_range(settings: Settings) -> None:
    options = "".join(f'<option value="{n}">{n}</option>' for n in range(1, 121))
    form = load(EDGE / "entry_form.html").replace(
        '<input type="text" name="nr_pytania" value="1">', f'<select name="nr_pytania">{options}</select>'
    )
    respx.get(BASE + "eula_pes.php").mock(return_value=httpx.Response(200))
    respx.post(BASE + "form_pytania_pes.php").mock(return_value=httpx.Response(200, html=form))
    respx.post(BASE + "wyswietl_pytania_pes.php").mock(
        return_value=httpx.Response(200, html=load(SYNTHETIC / "20031_1.html"))
    )
    s = http_settings(settings)
    async with CemClient(s, Metrics()) as client:
        grant = await HttpFormAccessProvider(s, FakeAuthorizedCaptcha()).acquire(ExamSession.from_code("20031"), client)
    assert grant.question_limit == 120
