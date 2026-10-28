"""InteractiveBrowserAccessProvider against a LOCAL stand-in server (never cem.edu.pl).

The stand-in issues a session cookie on the form page and serves questions only to
requests carrying it. The test plays the human: it submits the form the provider
pre-filled. This checks pre-filling, waiting, cookie hand-off to HTTPX and the
browser-context transport; it cannot check real reCAPTCHA behaviour.
"""

from __future__ import annotations

import asyncio
import socket
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar
from urllib.parse import parse_qs

import pytest

from cem_scraper.config import Settings
from cem_scraper.metrics import Metrics
from cem_scraper.models import ExamSession
from cem_scraper.session_manager import (
    QUESTION_URL_RE,
    AccessBlocked,
    InteractiveBrowserAccessProvider,
    ManualChromeAccessProvider,
    SessionManager,
)
from tests.conftest import EDGE, load, question_page

playwright = pytest.importorskip("playwright.async_api")

# The synthetic form without Google's script: nothing external is loaded. Like the real
# general form it posts to wyswietl_pytania.php (no "_pes"): the tool must follow the site.
FORM = (
    load(EDGE / "entry_form.html")
    .replace('<script src="https://www.google.com/recaptcha/api.js"></script>', "")
    .replace('action="wyswietl_pytania_pes.php"', 'action="wyswietl_pytania.php"')
)
EULA = load(EDGE / "eula.html")  # posts akceptacja=tak to form_pytania_pes.php, like eula_pes.php
SEASONS = {"1": "wiosna", "2": "jesień"}


class StandIn(BaseHTTPRequestHandler):
    """Mimics the observed site: eula_pes.php -> POST form_pytania_pes.php -> POST wyswietl_pytania.php."""

    question_posts: ClassVar[list[str]] = []

    def log_message(self, *args: object) -> None:
        pass

    def _send(self, html: str, cookie: str | None = None) -> None:
        body = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        self._send(EULA, cookie="PHPSESSID=standin; Path=/")

    def do_POST(self) -> None:
        form = {k: v[0] for k, v in parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode()).items()}
        has_cookie = "PHPSESSID=standin" in (self.headers.get("Cookie") or "")
        if self.path.endswith("form_pytania_pes.php"):
            self._send(FORM if form.get("akceptacja") == "tak" and has_cookie else EULA)
            return
        StandIn.question_posts.append(self.path)
        if not has_cookie:
            self._send(FORM)
            return
        number, code = int(form.get("nr_pytania", "1")), form.get("sesja", "")
        header = f"Pytanie nr {number} - PES Psychiatria w sesji {SEASONS[code[-1]]} {code[:4]}"
        self._send(question_page(number=number, center=header, question=f"sesja {code} pytanie {number}?"))


@pytest.fixture
def standin_url() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), StandIn)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/pytcem/"
    server.shutdown()


async def play_human(provider: InteractiveBrowserAccessProvider, choose: str = "20031") -> None:
    """Wait for the pre-filled form, check it, and submit it as the user would."""
    for _ in range(200):
        if provider._context is not None and provider._context.pages:
            page = provider._context.pages[-1]
            if await page.locator("select[name=sesja]").count():
                await asyncio.sleep(0.3)  # let pre-fill finish
                assert await page.locator("select[name=sesja]").input_value() == "20031"
                assert await page.locator("select[name=specjalizacja]").input_value() == "725"
                if choose != "20031":  # the user picks another session by mistake
                    await page.locator("select[name=sesja]").select_option(choose)
                await page.evaluate("document.forms[0].submit()")
                return
        await asyncio.sleep(0.05)
    raise AssertionError("form never appeared")


def standin_settings(settings: Settings, standin_url: str) -> Settings:
    return settings.model_copy(
        update={
            "base_url": standin_url,
            "browser_headless": True,
            "browser_wait_timeout": 20,
            "requests_per_second": 1000,
            "accept_eula": True,
        }
    )


@pytest.mark.parametrize("context_requests", [False, True])
async def test_interactive_flow_hands_session_to_httpx(
    settings: Settings, standin_url: str, context_requests: bool
) -> None:
    s = standin_settings(settings, standin_url)
    StandIn.question_posts.clear()
    provider = InteractiveBrowserAccessProvider(s, use_context_requests=context_requests)
    manager = SessionManager(s, provider, Metrics())
    try:
        try:
            auth, _ = await asyncio.gather(manager.open(ExamSession.from_code("20031")), play_human(provider))
        except Exception as exc:
            if "Executable doesn't exist" in str(exc):
                pytest.skip("Chromium not installed: `playwright install chromium`")
            raise
        assert auth.grant.first_page_html and "sesja 20031 pytanie 1?" in auth.grant.first_page_html
        page = await auth.client.fetch_question(auth.session, 7)
        assert "sesja 20031 pytanie 7?" in page.html  # the authorized session works outside the browser tab
        # Navigation follows the endpoint the site used, not the hard-coded _pes one.
        assert auth.client.question_url.endswith("/pytcem/wyswietl_pytania.php")
        assert StandIn.question_posts == ["/pytcem/wyswietl_pytania.php"] * 2
        await manager.release(auth)
    finally:
        await manager.close()


async def test_wrong_session_chosen_in_browser_is_rejected(settings: Settings, standin_url: str) -> None:
    s = standin_settings(settings, standin_url)
    provider = InteractiveBrowserAccessProvider(s)
    manager = SessionManager(s, provider, Metrics())
    try:
        with pytest.raises(AccessBlocked, match="shows exam session 20262, expected 20031"):
            await asyncio.gather(manager.open(ExamSession.from_code("20031")), play_human(provider, "20262"))
    finally:
        await manager.close()


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


async def test_manual_chrome_hands_session_over_without_controlling_the_page(
    settings: Settings, standin_url: str, tmp_path: Path
) -> None:
    """A real Chrome process (Chrome for Testing, headless here) and the stand-in site.

    The provider only opens a tab and watches /json/list; the test plays the user in
    that tab through its own CDP connection, as a human would with mouse and keyboard.
    """
    async with playwright.async_playwright() as p:
        chrome = Path(p.chromium.executable_path)
    if not chrome.exists():  # noqa: ASYNC240 - one stat call in test setup
        pytest.skip("Chromium not installed: `playwright install chromium`")
    port = free_port()
    s = settings.model_copy(
        update={
            "base_url": standin_url,
            "requests_per_second": 1000,
            "browser_wait_timeout": 30,
            "chrome_path": chrome,
            "chrome_debug_port": port,
            "browser_profile_dir": tmp_path / "profile",
            "chrome_extra_args": ["--headless=new"],
        }
    )
    StandIn.question_posts.clear()
    provider = ManualChromeAccessProvider(s, poll_interval=0.1)
    manager = SessionManager(s, provider, Metrics())

    async def human() -> None:
        async with playwright.async_playwright() as p:
            for _ in range(100):
                try:
                    browser = await p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
                    break
                except Exception:
                    await asyncio.sleep(0.1)
            for _ in range(100):
                pages = [pg for ctx in browser.contexts for pg in ctx.pages if pg.url.endswith("eula_pes.php")]
                if pages:
                    break
                await asyncio.sleep(0.1)
            page = pages[0]
            await page.locator("input[name=akceptacja]").check()
            await page.locator("[type=submit]").first.click()
            await page.wait_for_selector("select[name=sesja]")
            await page.select_option("select[name=sesja]", "20031")
            await page.select_option("select[name=specjalizacja]", "725")
            await page.evaluate("document.forms[0].submit()")
            await page.wait_for_url(QUESTION_URL_RE)

    try:
        auth, _ = await asyncio.gather(manager.open(ExamSession.from_code("20031")), human())
        assert auth.grant.first_page_html and "sesja 20031 pytanie 1?" in auth.grant.first_page_html
        assert auth.client.question_url.endswith("/pytcem/wyswietl_pytania.php")
        page = await auth.client.fetch_question(auth.session, 9)
        assert "sesja 20031 pytanie 9?" in page.html  # the cookie from Chrome works over HTTPX
        await manager.release(auth)
    finally:
        await manager.close()
    assert provider._process is None  # the Chrome we started is closed again


async def test_manual_chrome_reports_missing_chrome(settings: Settings, tmp_path: Path) -> None:
    s = settings.model_copy(
        update={
            "chrome_path": tmp_path / "no-chrome",
            "chrome_debug_port": free_port(),
            "browser_profile_dir": tmp_path,
        }
    )
    provider = ManualChromeAccessProvider(s)
    with pytest.raises(AccessBlocked, match="Google Chrome not found"):
        await SessionManager(s, provider, Metrics()).open(ExamSession.from_code("20031"))
    await provider.close()
