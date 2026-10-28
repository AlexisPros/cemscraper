"""Regression tests for paths where 2Captcha money could be spent for nothing.

2Captcha charges per solved task (createTask). Every test here pins down one way a
task could be bought twice, bought needlessly, or wasted after purchase.
All external services are mocked.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import respx
from pydantic import SecretStr

from cem_scraper.captcha import CaptchaSolution, CaptchaUnavailable, DisabledCaptchaProvider, TwoCaptchaProvider
from cem_scraper.client import CemClient
from cem_scraper.config import CaptchaProviderName, Settings
from cem_scraper.database import InMemoryRepository
from cem_scraper.main import limit_captcha_spending
from cem_scraper.metrics import Metrics
from cem_scraper.models import AttemptStatus, CollectionStatus, ExamSession, RunStatus
from cem_scraper.orchestrator import SessionOutcome
from cem_scraper.session_manager import AccessBlocked, HttpFormAccessProvider
from cem_scraper.throttle import RateLimiter, TrafficControl
from tests.conftest import EDGE, SYNTHETIC, load, question_page
from tests.test_captcha import API, KEY, READY, FakeTime, error, ok
from tests.test_orchestrator import ENTRY_FORM, ScriptedProvider, build, default_responder
from tests.test_session_manager import FakeAuthorizedCaptcha

CEM = "https://www.cem.edu.pl/pytcem/"
BROKEN_STATS = [["Kolumna"], ["?"]]  # always fails to parse: a "content" failure


@pytest.fixture
def fast(settings: Settings) -> Settings:
    return settings.model_copy(update={"requests_per_second": 10_000, "missing_streak_to_stop": 3})


# --------------------------------------------------------------------------- 1. never pay twice for one task


@pytest.mark.parametrize(
    "lost_response",
    [httpx.ReadTimeout("response lost"), httpx.RemoteProtocolError("dropped"), httpx.Response(502)],
)
@respx.mock
async def test_create_task_is_not_resent_when_it_may_exist(lost_response: object) -> None:
    respx.post(f"{API}/getBalance").mock(return_value=ok(balance=1))
    create = respx.post(f"{API}/createTask").mock(side_effect=[lost_response, ok(taskId=2)])
    ft = FakeTime()
    with pytest.raises(CaptchaUnavailable, match=r"(not resent|may have been created)"):
        await TwoCaptchaProvider(SecretStr(KEY), sleep=ft.sleep, clock=ft.clock).solve(site_key="k", page_url="u")
    assert create.call_count == 1  # a second createTask could have charged a second CAPTCHA


@respx.mock
async def test_create_task_retried_only_when_it_never_left() -> None:
    respx.post(f"{API}/getBalance").mock(return_value=ok(balance=1))
    create = respx.post(f"{API}/createTask").mock(side_effect=[httpx.ConnectError("no route"), ok(taskId=7)])
    respx.post(f"{API}/getTaskResult").mock(return_value=READY)
    ft = FakeTime()
    solution = await TwoCaptchaProvider(SecretStr(KEY), sleep=ft.sleep, clock=ft.clock).solve(
        site_key="k", page_url="u"
    )
    assert create.call_count == 2 and solution.task_id == "7"


@respx.mock
async def test_polling_and_balance_are_free_and_may_retry() -> None:
    respx.post(f"{API}/getBalance").mock(return_value=ok(balance=1))
    respx.post(f"{API}/createTask").mock(return_value=ok(taskId=1))
    result = respx.post(f"{API}/getTaskResult").mock(side_effect=[httpx.ReadTimeout("t"), httpx.Response(502), READY])
    ft = FakeTime()
    await TwoCaptchaProvider(SecretStr(KEY), sleep=ft.sleep, clock=ft.clock).solve(site_key="k", page_url="u")
    assert result.call_count == 3


# --------------------------------------------------------------------------- 2. no CAPTCHA when a download cannot help


def _broken_at(number: int, total: int = 4):
    def respond(session: str, n: int, provider: ScriptedProvider) -> str:
        if n == number:
            return question_page(number=n, stats_rows=BROKEN_STATS)
        return default_responder(total)(session, n, provider)

    return respond


async def test_resume_does_not_authorize_for_parse_failures(fast: Settings) -> None:
    provider = ScriptedProvider(_broken_at(3))
    orchestrator, repo = build(fast, provider)
    await orchestrator.run(["20031"])
    state = await repo.ensure_session(ExamSession.from_code("20031"))
    assert state.collection_status is CollectionStatus.FAILED and provider.grants == 1

    report = await build(fast, provider, repo)[0].run(["20031"])  # plain rerun / resume
    assert provider.grants == 1  # no new authorization, i.e. no new CAPTCHA
    assert report.sessions[0].outcome is SessionOutcome.INCOMPLETE
    assert "reparse" in (report.sessions[0].error or "")


async def test_retry_failed_skips_parse_failures_that_have_a_snapshot(fast: Settings, tmp_path: Path) -> None:
    provider = ScriptedProvider(_broken_at(3))
    orchestrator, repo = build(fast, provider)
    await orchestrator.run(["20031"])

    without_snapshot = fast.model_copy(update={"save_html_dir": tmp_path})
    await build(without_snapshot, provider, repo)[0].run(["20031"], only_failed=True)
    assert provider.grants == 2  # no saved page: downloading again is the only way to look at it

    (tmp_path / "20031_3.html").write_text(question_page(number=3, stats_rows=BROKEN_STATS), encoding="utf-8")
    report = await build(without_snapshot, provider, repo)[0].run(["20031"], only_failed=True)
    assert provider.grants == 2  # saved page exists: re-downloading would buy the same HTML
    assert "reparse" in (report.sessions[0].error or "")


async def test_network_failures_still_get_retried(fast: Settings) -> None:
    broken = {3}

    def respond(session: str, n: int, provider: ScriptedProvider) -> httpx.Response | str:
        return httpx.Response(500) if n in broken else default_responder(4)(session, n, provider)

    provider = ScriptedProvider(respond)
    orchestrator, repo = build(fast, provider)
    await orchestrator.run(["20031"])
    broken.clear()
    report = await build(fast, provider, repo)[0].run(["20031"])
    assert provider.grants == 2 and report.status is RunStatus.COMPLETED


async def test_known_empty_session_is_not_reopened_without_refetch(fast: Settings) -> None:
    provider = ScriptedProvider(default_responder(0))
    orchestrator, repo = build(fast, provider)
    await orchestrator.run(["20031"])
    await build(fast, provider, repo)[0].run(["20031"])
    assert provider.grants == 1
    await build(fast, provider, repo)[0].run(["20031"], refetch=True)
    assert provider.grants == 2


# --------------------------------------------------------------------------- 3. no re-authorization on a guess


async def test_failed_probe_does_not_trigger_a_paid_reauthorization(fast: Settings) -> None:
    calls = {"q1": 0}

    def respond(session: str, n: int, provider: ScriptedProvider) -> httpx.Response | str:
        if n == 1:
            calls["q1"] += 1
            if 2 <= calls["q1"] <= 1 + 3:  # the probe of q1 times out on all its attempts
                return httpx.Response(503)
        if n >= 4:
            return ENTRY_FORM  # past the end: the site answers with the entry form
        return question_page(number=n)

    provider = ScriptedProvider(respond)
    s = fast.model_copy(update={"max_retries": 2, "concurrency": 1, "adaptive_concurrency": False})
    orchestrator, repo = build(s, provider)
    report = await orchestrator.run(["20031"])
    assert provider.grants == 1  # never re-authorized (= never paid) on an inconclusive probe
    state = await repo.ensure_session(ExamSession.from_code("20031"))
    statuses = await repo.attempt_statuses(state.id)
    assert statuses[4] is AttemptStatus.SERVER_ERROR  # retried in a later run, not counted as missing
    assert AttemptStatus.AUTH_ERROR not in statuses.values()
    assert report.status is RunStatus.PARTIAL


async def test_expired_session_still_reauthorizes(fast: Settings) -> None:
    def respond(session: str, n: int, provider: ScriptedProvider) -> str:
        # The first grant expires at q3; the probe of q1 then shows the entry form too.
        expired = provider.grants == 1 and (n >= 3 or len(provider.requested) > 3)
        return ENTRY_FORM if expired else default_responder(4)(session, n, provider)

    provider = ScriptedProvider(respond)
    orchestrator, _ = build(fast.model_copy(update={"concurrency": 1, "adaptive_concurrency": False}), provider)
    report = await orchestrator.run(["20031"])
    assert provider.grants == 2 and report.status is RunStatus.COMPLETED  # a real expiry is still handled


# --------------------------------------------------------------------------- 4/5. a bought token is used once and fresh


def _mock_cem(question_response: httpx.Response | None = None) -> respx.Route:
    respx.get(CEM + "eula_pes.php").mock(return_value=httpx.Response(200, html=load(EDGE / "eula.html")))
    respx.post(CEM + "form_pytania_pes.php").mock(return_value=httpx.Response(200, html=load(EDGE / "entry_form.html")))
    return respx.post(CEM + "wyswietl_pytania_pes.php").mock(
        return_value=question_response or httpx.Response(200, html=load(SYNTHETIC / "20031_1.html"))
    )


@pytest.mark.parametrize("failure", [httpx.Response(503), httpx.ReadTimeout("slow")])
@respx.mock
async def test_token_is_submitted_exactly_once(settings: Settings, failure: object) -> None:
    submit = respx.post(CEM + "wyswietl_pytania_pes.php").mock(side_effect=[failure, httpx.Response(200)])
    respx.get(CEM + "eula_pes.php").mock(return_value=httpx.Response(200, html=load(EDGE / "eula.html")))
    respx.post(CEM + "form_pytania_pes.php").mock(return_value=httpx.Response(200, html=load(EDGE / "entry_form.html")))
    captcha = FakeAuthorizedCaptcha()
    s = settings.model_copy(update={"accept_eula": True, "max_retries": 3})
    async with CemClient(s, Metrics(), traffic=TrafficControl(s, limiter=RateLimiter(1000))) as client:
        with pytest.raises(AccessBlocked, match="session entry failed"):
            await HttpFormAccessProvider(s, captcha).acquire(ExamSession.from_code("20031"), client)
    assert submit.call_count == 1  # never resent with the same (single-use) token
    assert len(captcha.calls) == 1


@respx.mock
async def test_token_is_not_bought_during_a_server_pause(settings: Settings) -> None:
    ft = FakeTime()
    s = settings.model_copy(update={"accept_eula": True})
    traffic = TrafficControl(s, limiter=RateLimiter(5, clock=ft.clock, sleep=ft.sleep))
    respx.get(CEM + "eula_pes.php").mock(return_value=httpx.Response(200, html=load(EDGE / "eula.html")))

    def form_page(request: httpx.Request) -> httpx.Response:
        traffic.limiter.defer(90)  # meanwhile another session got "Retry-After: 90"
        return httpx.Response(200, html=load(EDGE / "entry_form.html"))

    respx.post(CEM + "form_pytania_pes.php").mock(side_effect=form_page)
    respx.post(CEM + "wyswietl_pytania_pes.php").mock(
        return_value=httpx.Response(200, html=load(SYNTHETIC / "20031_1.html"))
    )
    solved_at: list[float] = []

    class ClockedCaptcha(FakeAuthorizedCaptcha):
        async def solve(self, *, site_key: str, page_url: str) -> CaptchaSolution:
            solved_at.append(ft.now)
            return await super().solve(site_key=site_key, page_url=page_url)

    async with CemClient(s, Metrics(), traffic=traffic) as client:
        await HttpFormAccessProvider(s, ClockedCaptcha()).acquire(ExamSession.from_code("20031"), client)
    assert solved_at and solved_at[0] >= 90  # bought only after the pause, so it is fresh at submission


async def test_idle_ignores_normal_spacing() -> None:
    ft = FakeTime()
    limiter = RateLimiter(0.5, clock=ft.clock, sleep=ft.sleep)
    await limiter.acquire()  # reserves the next slot 2 s ahead: normal spacing, not a pause
    await limiter.idle()
    assert ft.sleeps == []


@respx.mock
async def test_stale_token_is_flagged(settings: Settings, caplog: pytest.LogCaptureFixture) -> None:
    _mock_cem()

    class SlowCaptcha(FakeAuthorizedCaptcha):
        async def solve(self, *, site_key: str, page_url: str) -> CaptchaSolution:
            solution = await super().solve(site_key=site_key, page_url=page_url)
            solution.solved_at -= 130  # pretend it was solved 130 s ago
            return solution

    s = settings.model_copy(update={"accept_eula": True})
    with caplog.at_level(logging.INFO):
        async with CemClient(s, Metrics(), traffic=TrafficControl(s, limiter=RateLimiter(1000))) as client:
            await HttpFormAccessProvider(s, SlowCaptcha()).acquire(ExamSession.from_code("20031"), client)
    assert "has probably expired" in caplog.text


# --------------------------------------------------------------------------- 6. lifetime budget across runs


async def _repo_with_spent(amount: str) -> InMemoryRepository:
    repo = InMemoryRepository()
    run = await repo.start_run(["20031"], "http_form")
    await repo.finish_run(run, RunStatus.COMPLETED, {"captcha_cost": float(amount)})
    return repo


async def test_lifetime_budget_limits_the_next_run() -> None:
    s = Settings(_env_file=None, captcha_total_budget=5.0, captcha_max_spend=1.0)  # type: ignore[call-arg]
    captcha = TwoCaptchaProvider(SecretStr(KEY), max_spend=Decimal("1.0"))
    await limit_captcha_spending(captcha, s, await _repo_with_spent("4.7"))
    assert captcha.max_spend == Decimal("0.3")
    await captcha.close()


async def test_lifetime_budget_exhausted_blocks_before_any_request() -> None:
    s = Settings(_env_file=None, captcha_total_budget=5.0)  # type: ignore[call-arg]
    captcha = TwoCaptchaProvider(SecretStr(KEY))
    with pytest.raises(AccessBlocked, match="lifetime CAPTCHA budget used up"):
        await limit_captcha_spending(captcha, s, await _repo_with_spent("5.0"))
    await captcha.close()


async def test_free_modes_ignore_the_budget() -> None:
    await limit_captcha_spending(DisabledCaptchaProvider(), Settings(_env_file=None), await _repo_with_spent("99"))  # type: ignore[call-arg]


def test_total_budget_cannot_exceed_100_pln() -> None:
    with pytest.raises(ValueError):
        Settings(_env_file=None, captcha_total_budget=30)  # type: ignore[call-arg]
    assert Settings(_env_file=None, captcha_provider=CaptchaProviderName.TWOCAPTCHA).captcha_total_budget == 5.0  # type: ignore[call-arg]


# --------------------------------------------------------------------------- already-correct behaviour, pinned


@respx.mock
async def test_unsolvable_task_is_refunded_and_replaced_not_paid_twice() -> None:
    respx.post(f"{API}/getBalance").mock(return_value=ok(balance=1))
    respx.post(f"{API}/createTask").mock(side_effect=[ok(taskId=1), ok(taskId=2)])
    respx.post(f"{API}/getTaskResult").mock(side_effect=[error("ERROR_CAPTCHA_UNSOLVABLE", 12), READY])
    ft = FakeTime()
    metrics = Metrics()
    await TwoCaptchaProvider(SecretStr(KEY), sleep=ft.sleep, clock=ft.clock, metrics=metrics).solve(
        site_key="k", page_url="u"
    )
    assert metrics.captcha_solves == 1  # only the solved task is counted as spent


@respx.mock
async def test_checks_that_can_fail_happen_before_paying(settings: Settings) -> None:
    respx.get(CEM + "eula_pes.php").mock(return_value=httpx.Response(200))
    respx.post(CEM + "form_pytania_pes.php").mock(return_value=httpx.Response(200, html=load(EDGE / "entry_form.html")))
    captcha = FakeAuthorizedCaptcha()
    s = settings.model_copy(update={"accept_eula": True})
    async with CemClient(s, Metrics(), traffic=TrafficControl(s, limiter=RateLimiter(1000))) as client:
        with pytest.raises(AccessBlocked, match="not offered"):
            await HttpFormAccessProvider(s, captcha).acquire(ExamSession.from_code("20051"), client)
    assert captcha.calls == []  # session not in the form: nothing bought
