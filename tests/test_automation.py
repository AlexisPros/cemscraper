"""Fully automated entry, from the EULA page to the last question, with CEM and 2Captcha MOCKED.

The stand-in for CEM is stateful: a session cookie from the EULA page, the official
form, and a session that becomes authorized only by a form submission carrying the
token 2Captcha returned. Nothing here touches the network.
"""

from __future__ import annotations

import json
from collections import defaultdict
from itertools import count
from urllib.parse import parse_qs

import httpx
import pytest
import respx
from pydantic import SecretStr

from cem_scraper.captcha import TwoCaptchaProvider
from cem_scraper.config import AccessProviderName, CaptchaProviderName, Settings
from cem_scraper.database import InMemoryRepository
from cem_scraper.metrics import Metrics
from cem_scraper.models import RunStatus
from cem_scraper.orchestrator import Orchestrator, SessionOutcome
from cem_scraper.session_manager import HttpFormAccessProvider, SessionManager
from tests.conftest import EDGE, load, question_page

CEM = "https://www.cem.edu.pl/pytcem/"
API = "https://api.2captcha.com"
TOKEN = "03AFcWeA-synthetic-token"
NO_QUESTION = '<html><body><div class="marginesy"><div><p>Brak</p></div></div></body></html>'
TOTALS = {"20031": 5, "20262": 3}


class FakeCem:
    def __init__(self, accepted_token: str = TOKEN) -> None:
        self.accepted_token = accepted_token
        self.cookies = count(1)
        self.authorized: set[tuple[str, str]] = set()
        self.submissions: list[dict[str, str]] = []
        self.navigation: list[dict[str, str]] = []

    @staticmethod
    def _cookie(request: httpx.Request) -> str:
        return (request.headers.get("cookie") or "").removeprefix("PHPSESSID=")

    def eula(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, html=load(EDGE / "eula.html"), headers={"Set-Cookie": f"PHPSESSID=s{next(self.cookies)}; Path=/"}
        )

    def form(self, request: httpx.Request) -> httpx.Response:
        assert parse_qs(request.content.decode()) == {"akceptacja": ["tak"]}
        assert self._cookie(request), "EULA must be accepted within the cookie session"
        return httpx.Response(200, html=load(EDGE / "entry_form.html"))

    def questions(self, request: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        cookie, session, number = self._cookie(request), form["sesja"], int(form["nr_pytania"])
        if "g-recaptcha-response" in form:
            self.submissions.append(form)
            if form["g-recaptcha-response"] != self.accepted_token:
                return httpx.Response(200, html=load(EDGE / "entry_form.html"))
            self.authorized.add((cookie, session))
        else:
            self.navigation.append(form)
            if (cookie, session) not in self.authorized:
                return httpx.Response(200, html=load(EDGE / "entry_form.html"))
        html = question_page(number=number) if number <= TOTALS[session] else NO_QUESTION
        return httpx.Response(200, html=html)


def mock_services(cem: FakeCem) -> dict[str, respx.Route]:
    respx.get(CEM + "eula_pes.php").mock(side_effect=cem.eula)
    respx.post(CEM + "form_pytania_pes.php").mock(side_effect=cem.form)
    respx.post(CEM + "wyswietl_pytania_pes.php").mock(side_effect=cem.questions)
    tasks = count(100)
    return {
        "balance": respx.post(f"{API}/getBalance").mock(
            return_value=httpx.Response(200, json={"errorId": 0, "balance": 2.5})
        ),
        "create": respx.post(f"{API}/createTask").mock(
            side_effect=lambda r: httpx.Response(200, json={"errorId": 0, "taskId": next(tasks)})
        ),
        "result": respx.post(f"{API}/getTaskResult").mock(
            return_value=httpx.Response(
                200,
                json={"errorId": 0, "status": "ready", "solution": {"gRecaptchaResponse": TOKEN}, "cost": "0.00299"},
            )
        ),
        "correct": respx.post(f"{API}/reportCorrect").mock(return_value=httpx.Response(200, json={"errorId": 0})),
        "incorrect": respx.post(f"{API}/reportIncorrect").mock(return_value=httpx.Response(200, json={"errorId": 0})),
    }


def build(settings: Settings) -> tuple[Orchestrator, InMemoryRepository, Metrics]:
    s = settings.model_copy(
        update={
            "access_provider": AccessProviderName.HTTP_FORM,
            "captcha_provider": CaptchaProviderName.TWOCAPTCHA,
            "accept_eula": True,
            "requests_per_second": 10_000,
            "pipeline_depth": 2,
        }
    )
    metrics = Metrics()
    captcha = TwoCaptchaProvider(SecretStr("TEST-KEY"), poll_interval=0.001, metrics=metrics)
    repo = InMemoryRepository()
    manager = SessionManager(s, HttpFormAccessProvider(s, captcha), metrics)
    return Orchestrator(s, repo, manager, metrics), repo, metrics


@respx.mock
async def test_unattended_run_from_eula_to_last_question(settings: Settings) -> None:
    cem = FakeCem()
    api = mock_services(cem)
    orchestrator, repo, metrics = build(settings)
    report = await orchestrator.run(["20031", "20262"])
    await orchestrator.sessions.close()

    assert report.status is RunStatus.COMPLETED
    records = await repo.fetch_records()
    per_session = defaultdict(list)
    for record in records:
        per_session[record["session_id"]].append(record["question_number"])
    assert per_session == {"20031": [1, 2, 3, 4, 5], "20262": [1, 2, 3]}

    # One CAPTCHA per session, each reported back as accepted; balance checked once.
    assert api["create"].call_count == 2 and api["correct"].call_count == 2
    assert api["balance"].call_count == 1 and api["incorrect"].call_count == 0
    assert metrics.captcha_solves == 2
    # The token travels only in the two form submissions, never in navigation requests.
    assert len(cem.submissions) == 2
    assert all("g-recaptcha-response" not in form for form in cem.navigation)
    # Question 1 came with the form submission: never requested again.
    assert all(form["nr_pytania"] != "1" for form in cem.navigation)
    assert json.loads(api["create"].calls[0].request.content)["task"]["websiteKey"] == "SYNTHETIC-SITEKEY"


@respx.mock
async def test_rejected_token_is_reported_and_blocks(settings: Settings) -> None:
    cem = FakeCem(accepted_token="something-else")
    api = mock_services(cem)
    orchestrator, repo, _ = build(settings)
    report = await orchestrator.run(["20031", "20262"])
    await orchestrator.sessions.close()

    assert report.status is RunStatus.BLOCKED
    assert report.sessions[0].outcome is SessionOutcome.BLOCKED
    assert api["incorrect"].call_count == 1  # refund requested for the rejected token
    assert api["create"].call_count == 1  # no further CAPTCHAs bought for a blocked run
    assert await repo.fetch_records() == []


@pytest.mark.parametrize("missing", ["accept_eula", "key"])
@respx.mock
async def test_nothing_is_contacted_without_consent_or_key(settings: Settings, missing: str) -> None:
    mock_services(FakeCem())
    s = settings.model_copy(
        update={"accept_eula": missing != "accept_eula", "requests_per_second": 10_000, "pipeline_depth": 1}
    )
    metrics = Metrics()
    captcha = TwoCaptchaProvider(None if missing == "key" else SecretStr("TEST-KEY"), metrics=metrics)
    orchestrator = Orchestrator(
        s, InMemoryRepository(), SessionManager(s, HttpFormAccessProvider(s, captcha), metrics), metrics
    )
    report = await orchestrator.run(["20031"])
    await orchestrator.sessions.close()
    assert report.status is RunStatus.BLOCKED
    assert respx.calls.call_count == 0
