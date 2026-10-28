"""InteractiveBrowserAccessProvider against a LOCAL stand-in server (never cem.edu.pl).

The stand-in issues a session cookie on the form page and serves questions only to
requests carrying it. The test plays the human: it submits the form the provider
pre-filled. This checks pre-filling, waiting, cookie hand-off to HTTPX and the
browser-context transport; it cannot check real reCAPTCHA behaviour.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

import pytest

from cem_scraper.config import Settings
from cem_scraper.metrics import Metrics
from cem_scraper.models import ExamSession
from cem_scraper.session_manager import InteractiveBrowserAccessProvider, SessionManager
from tests.conftest import EDGE, load, question_page

playwright = pytest.importorskip("playwright.async_api")

# The synthetic form without Google's script: nothing external is loaded.
FORM = load(EDGE / "entry_form.html").replace('<script src="https://www.google.com/recaptcha/api.js"></script>', "")


class StandIn(BaseHTTPRequestHandler):
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
        self._send(FORM, cookie="PHPSESSID=standin; Path=/")

    def do_POST(self) -> None:
        form = {k: v[0] for k, v in parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode()).items()}
        if "PHPSESSID=standin" not in (self.headers.get("Cookie") or ""):
            self._send(FORM)
            return
        number = int(form.get("nr_pytania", "1"))
        page = question_page(number=number, question=f"sesja {form.get('sesja')} pytanie {number}?")
        self._send(page.replace("</body>", f'<input type="hidden" name="sesja" value="{form.get("sesja")}"></body>'))


@pytest.fixture
def standin_url() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), StandIn)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/pytcem/"
    server.shutdown()


async def play_human(provider: InteractiveBrowserAccessProvider) -> None:
    """Wait for the pre-filled form, check it, and submit it as the user would."""
    for _ in range(200):
        if provider._context is not None and provider._context.pages:
            page = provider._context.pages[-1]
            if await page.locator("select[name=sesja]").count():
                await asyncio.sleep(0.3)  # let pre-fill finish
                assert await page.locator("select[name=sesja]").input_value() == "20031"
                assert await page.locator("select[name=specjalizacja]").input_value() == "725"
                await page.evaluate("document.forms[0].submit()")
                return
        await asyncio.sleep(0.05)
    raise AssertionError("form never appeared")


@pytest.mark.parametrize("context_requests", [False, True])
async def test_interactive_flow_hands_session_to_httpx(
    settings: Settings, standin_url: str, context_requests: bool
) -> None:
    s = settings.model_copy(
        update={
            "base_url": standin_url,
            "browser_headless": True,
            "browser_wait_timeout": 20,
            "requests_per_second": 1000,
        }
    )
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
        await manager.release(auth)
    finally:
        await manager.close()
