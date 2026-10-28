"""2Captcha provider against a mocked API (never the real service, never a real key)."""

from __future__ import annotations

import json
import logging
from decimal import Decimal

import httpx
import pytest
import respx
from pydantic import SecretStr

from cem_scraper.captcha import (
    CaptchaUnavailable,
    DisabledCaptchaProvider,
    TwoCaptchaProvider,
    build_captcha_provider,
)
from cem_scraper.config import CaptchaProviderName, Settings
from cem_scraper.metrics import Metrics

API = "https://api.2captcha.com"
KEY = "TEST-KEY-123456"
TOKEN = "03AFcWeA-synthetic-token"


class FakeTime:
    """Sleep that advances a fake clock instead of waiting."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def fake_time() -> FakeTime:
    return FakeTime()


def provider(fake_time: FakeTime, **kw: object) -> TwoCaptchaProvider:
    return TwoCaptchaProvider(
        SecretStr(KEY),
        sleep=fake_time.sleep,
        clock=fake_time.clock,
        **kw,  # type: ignore[arg-type]
    )


def body(call: respx.models.Call) -> dict[str, object]:
    return json.loads(call.request.content)


def ok(**data: object) -> httpx.Response:
    return httpx.Response(200, json={"errorId": 0, **data})


def error(code: str, error_id: int = 1) -> httpx.Response:
    return httpx.Response(200, json={"errorId": error_id, "errorCode": code, "errorDescription": "x"})


READY = ok(status="ready", solution={"gRecaptchaResponse": TOKEN, "token": TOKEN}, cost="0.00299")


@respx.mock
async def test_solve_happy_path(fake_time: FakeTime) -> None:
    respx.post(f"{API}/getBalance").mock(return_value=ok(balance=0.93958))
    create = respx.post(f"{API}/createTask").mock(return_value=ok(taskId=72345678901))
    result = respx.post(f"{API}/getTaskResult").mock(side_effect=[ok(status="processing"), READY])
    metrics = Metrics()
    p = provider(fake_time, metrics=metrics)
    solution = await p.solve(site_key="SITEKEY", page_url="https://www.cem.edu.pl/pytcem/form_pytania_pes.php")
    await p.close()

    assert solution.token == TOKEN and solution.task_id == "72345678901" and solution.cost == Decimal("0.00299")
    assert body(create.calls[0]) == {
        "clientKey": KEY,
        "task": {
            "type": "RecaptchaV2TaskProxyless",
            "websiteURL": "https://www.cem.edu.pl/pytcem/form_pytania_pes.php",
            "websiteKey": "SITEKEY",
        },
    }
    assert body(result.calls[0]) == {"clientKey": KEY, "taskId": 72345678901}
    assert fake_time.sleeps == [5.0, 5.0]  # never polls immediately after createTask
    assert (metrics.captcha_solves, metrics.captcha_cost) == (1, Decimal("0.00299"))
    assert TOKEN not in repr(solution)


@respx.mock
async def test_balance_checked_once_and_empty_balance_stops(fake_time: FakeTime) -> None:
    respx.post(f"{API}/getBalance").mock(return_value=ok(balance=0))
    create = respx.post(f"{API}/createTask")
    with pytest.raises(CaptchaUnavailable, match="balance is empty; top it up yourself"):
        await provider(fake_time).solve(site_key="k", page_url="u")
    assert create.call_count == 0  # nothing spent, nothing bought


@respx.mock
async def test_account_errors_are_not_retried(fake_time: FakeTime) -> None:
    respx.post(f"{API}/getBalance").mock(return_value=ok(balance=1))
    create = respx.post(f"{API}/createTask").mock(return_value=error("ERROR_ZERO_BALANCE", 10))
    with pytest.raises(CaptchaUnavailable, match="ERROR_ZERO_BALANCE"):
        await provider(fake_time).solve(site_key="k", page_url="u")
    assert create.call_count == 1


@respx.mock
async def test_wrong_key(fake_time: FakeTime) -> None:
    respx.post(f"{API}/getBalance").mock(return_value=error("ERROR_KEY_DOES_NOT_EXIST", 1))
    with pytest.raises(CaptchaUnavailable, match="ERROR_KEY_DOES_NOT_EXIST"):
        await provider(fake_time).solve(site_key="k", page_url="u")


@respx.mock
async def test_unsolvable_and_no_slot_get_a_new_task(fake_time: FakeTime) -> None:
    respx.post(f"{API}/getBalance").mock(return_value=ok(balance=1))
    create = respx.post(f"{API}/createTask").mock(
        side_effect=[error("ERROR_NO_SLOT_AVAILABLE", 2), ok(taskId=1), ok(taskId=2)]
    )
    respx.post(f"{API}/getTaskResult").mock(side_effect=[error("ERROR_CAPTCHA_UNSOLVABLE", 12), READY])
    solution = await provider(fake_time).solve(site_key="k", page_url="u")
    assert create.call_count == 3 and solution.task_id == "2"


@respx.mock
async def test_gives_up_after_max_tasks(fake_time: FakeTime) -> None:
    respx.post(f"{API}/getBalance").mock(return_value=ok(balance=1))
    respx.post(f"{API}/createTask").mock(return_value=ok(taskId=1))
    respx.post(f"{API}/getTaskResult").mock(return_value=error("ERROR_CAPTCHA_UNSOLVABLE", 12))
    with pytest.raises(CaptchaUnavailable, match="after 3 tasks"):
        await provider(fake_time).solve(site_key="k", page_url="u")


@respx.mock
async def test_timeout(fake_time: FakeTime) -> None:
    respx.post(f"{API}/getBalance").mock(return_value=ok(balance=1))
    respx.post(f"{API}/createTask").mock(return_value=ok(taskId=1))
    respx.post(f"{API}/getTaskResult").mock(return_value=ok(status="processing"))
    with pytest.raises(CaptchaUnavailable, match="not solved within 30s"):
        await provider(fake_time, timeout=30).solve(site_key="k", page_url="u")
    assert fake_time.now <= 40


@respx.mock
async def test_transient_http_errors_are_retried(fake_time: FakeTime) -> None:
    respx.post(f"{API}/getBalance").mock(side_effect=[httpx.Response(502), httpx.ConnectTimeout("t"), ok(balance=3)])
    assert await provider(fake_time).balance() == Decimal(3)


@respx.mock
async def test_budget_caps(fake_time: FakeTime) -> None:
    respx.post(f"{API}/getBalance").mock(return_value=ok(balance=1))
    respx.post(f"{API}/createTask").mock(return_value=ok(taskId=1))
    respx.post(f"{API}/getTaskResult").mock(return_value=READY)
    p = provider(fake_time, max_solves=2)
    await p.solve(site_key="k", page_url="u")
    await p.solve(site_key="k", page_url="u")
    assert not p.available
    with pytest.raises(CaptchaUnavailable, match="budget for this run reached"):
        await p.solve(site_key="k", page_url="u")

    spender = provider(fake_time, max_spend=Decimal("0.004"))
    await spender.solve(site_key="k", page_url="u")
    await spender.solve(site_key="k", page_url="u")  # 0.00598 > 0.004 after this one
    assert not spender.available


@respx.mock
async def test_missing_cost_is_charged_conservatively(fake_time: FakeTime) -> None:
    respx.post(f"{API}/getBalance").mock(return_value=ok(balance=1))
    respx.post(f"{API}/createTask").mock(return_value=ok(taskId=1))
    respx.post(f"{API}/getTaskResult").mock(return_value=ok(status="ready", solution={"gRecaptchaResponse": TOKEN}))
    solution = await provider(fake_time).solve(site_key="k", page_url="u")
    assert solution.cost == TwoCaptchaProvider.FALLBACK_COST


@respx.mock
async def test_feedback_is_best_effort(fake_time: FakeTime) -> None:
    correct = respx.post(f"{API}/reportCorrect").mock(return_value=ok(status="success"))
    respx.post(f"{API}/reportIncorrect").mock(return_value=error("ERROR_NO_SUCH_CAPCHA_ID", 16))
    p = provider(fake_time)
    from cem_scraper.captcha import CaptchaSolution

    await p.report(CaptchaSolution(token=TOKEN, task_id="42"), accepted=True)
    await p.report(CaptchaSolution(token=TOKEN, task_id="43"), accepted=False)  # must not raise
    assert body(correct.calls[0]) == {"clientKey": KEY, "taskId": 42}


@respx.mock
async def test_key_and_token_never_logged(fake_time: FakeTime, caplog: pytest.LogCaptureFixture) -> None:
    respx.post(f"{API}/getBalance").mock(side_effect=[httpx.Response(500), ok(balance=1)])
    respx.post(f"{API}/createTask").mock(return_value=ok(taskId=1))
    respx.post(f"{API}/getTaskResult").mock(return_value=READY)
    with caplog.at_level(logging.DEBUG):
        await provider(fake_time).solve(site_key="k", page_url="u")
    assert KEY not in caplog.text and TOKEN not in caplog.text
    assert "CAPTCHA solved by 2Captcha" in caplog.text


def test_availability_and_factory() -> None:
    assert not TwoCaptchaProvider(None).available
    assert not TwoCaptchaProvider(SecretStr("")).available
    assert TwoCaptchaProvider(SecretStr(KEY)).available
    s = Settings(_env_file=None, captcha_provider=CaptchaProviderName.TWOCAPTCHA, twocaptcha_api_key=KEY)  # type: ignore[call-arg]
    built = build_captcha_provider(s)
    assert isinstance(built, TwoCaptchaProvider) and built.available
    assert KEY not in repr(s)
    default = build_captcha_provider(Settings(_env_file=None))  # type: ignore[call-arg]
    assert isinstance(default, DisabledCaptchaProvider)
