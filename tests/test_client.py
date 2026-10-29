from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from urllib.parse import parse_qs

import httpx
import pytest
import respx

from cem_scraper.client import (
    AccessDenied,
    CemClient,
    RequestFailed,
    ServerThrottled,
    ServerUnavailable,
    UnexpectedStatus,
    parse_retry_after,
    sanitize_snapshot,
)
from cem_scraper.config import Settings
from cem_scraper.logging_config import RedactingFilter, redact
from cem_scraper.metrics import Metrics
from cem_scraper.models import ExamSession
from cem_scraper.throttle import CircuitBreaker, RateLimiter, TrafficControl

QUESTION_URL = "https://www.cem.edu.pl/pytcem/wyswietl_pytania_pes.php"


class FakeSleep:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


@pytest.fixture
def sleep() -> FakeSleep:
    return FakeSleep()


@pytest.fixture
def limiter_sleep() -> FakeSleep:
    return FakeSleep()


@pytest.fixture
def client_factory(settings: Settings, sleep: FakeSleep, limiter_sleep: FakeSleep):
    def make(**overrides: object) -> CemClient:
        s = settings.model_copy(update=overrides)
        # Backoff sleeps (``sleep``) are recorded separately from request spacing (``limiter_sleep``).
        traffic = TrafficControl(s, limiter=RateLimiter(1000, sleep=limiter_sleep))
        return CemClient(s, Metrics(), sleep=sleep, traffic=traffic)

    return make


@respx.mock
async def test_fetch_question_posts_observed_payload(client_factory) -> None:
    route = respx.post(QUESTION_URL).mock(return_value=httpx.Response(200, html="<p>ok</p>"))
    client = client_factory()
    page = await client.fetch_question(ExamSession.from_code("20262"), 2)
    assert page.status_code == 200 and "ok" in page.html
    sent = {k: v[0] for k, v in parse_qs(route.calls[0].request.content.decode()).items()}
    assert sent == {
        "autoryzacja": "1",
        "sesja": "20262",
        "typ": "PES",
        "specjalizacja": "725",
        "nr_pytania": "2",
        "gdzieidziesz": "doprzodu",
    }
    assert "g-recaptcha-response" not in sent
    assert client.metrics.request_successes == 1


@respx.mock
async def test_retries_transient_errors_with_backoff(client_factory, sleep: FakeSleep) -> None:
    respx.post(QUESTION_URL).mock(
        side_effect=[httpx.Response(503), httpx.ConnectTimeout("slow"), httpx.Response(200, html="ok")]
    )
    client = client_factory()
    page = await client.post("wyswietl_pytania_pes.php", {})
    assert page.html == "ok"
    assert client.metrics.retries == 2
    assert len(sleep.calls) == 2 and all(0 <= s <= 0.02 for s in sleep.calls)


@respx.mock
async def test_retry_after_pauses_every_request(client_factory, sleep: FakeSleep, limiter_sleep: FakeSleep) -> None:
    respx.post(QUESTION_URL).mock(
        side_effect=[httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200), httpx.Response(200)]
    )
    client = client_factory()
    await client.post("wyswietl_pytania_pes.php", {})  # the throttled request, retried after the pause
    await client.post("wyswietl_pytania_pes.php", {})  # an unrelated request must also respect the pause
    assert sleep.calls == []  # no private backoff: the pause is global
    assert [round(s) for s in limiter_sleep.calls] == [7, 7]


@respx.mock
async def test_excessive_retry_after_stops(client_factory) -> None:
    respx.post(QUESTION_URL).mock(return_value=httpx.Response(429, headers={"Retry-After": "3600"}))
    with pytest.raises(ServerThrottled):
        await client_factory().post("wyswietl_pytania_pes.php", {})


@respx.mock
async def test_access_denied_is_not_retried(client_factory) -> None:
    route = respx.post(QUESTION_URL).mock(return_value=httpx.Response(403))
    with pytest.raises(AccessDenied):
        await client_factory().post("wyswietl_pytania_pes.php", {})
    assert route.call_count == 1


@respx.mock
async def test_non_retryable_status(client_factory) -> None:
    route = respx.post(QUESTION_URL).mock(return_value=httpx.Response(404))
    with pytest.raises(UnexpectedStatus):
        await client_factory().post("wyswietl_pytania_pes.php", {})
    assert route.call_count == 1


@respx.mock
async def test_exhausted_retries_then_circuit_breaker(client_factory) -> None:
    respx.post(QUESTION_URL).mock(side_effect=httpx.ReadTimeout("timeout"))
    client = client_factory(max_retries=1, circuit_breaker_threshold=2)
    with pytest.raises(RequestFailed):
        await client.post("wyswietl_pytania_pes.php", {})
    with pytest.raises(ServerUnavailable):
        await client.post("wyswietl_pytania_pes.php", {})
    with pytest.raises(ServerUnavailable, match="circuit breaker open"):
        await client.post("wyswietl_pytania_pes.php", {})
    assert client.metrics.request_failures == 4


@respx.mock
async def test_success_resets_failure_counter(client_factory) -> None:
    respx.post(QUESTION_URL).mock(
        side_effect=[httpx.ReadTimeout("t"), httpx.Response(200), httpx.ReadTimeout("t"), httpx.Response(200)]
    )
    client = client_factory(max_retries=0, circuit_breaker_threshold=2)
    with pytest.raises(RequestFailed):
        await client.post("wyswietl_pytania_pes.php", {})
    await client.post("wyswietl_pytania_pes.php", {})
    with pytest.raises(RequestFailed):  # not ServerUnavailable: the counter was reset
        await client.post("wyswietl_pytania_pes.php", {})


@respx.mock
async def test_overload_pauses_the_whole_lane(
    client_factory, limiter_sleep: FakeSleep, caplog: pytest.LogCaptureFixture
) -> None:
    respx.post(QUESTION_URL).mock(side_effect=[httpx.ReadTimeout("t"), httpx.Response(503), httpx.Response(200)])
    client = client_factory(overload_pause=30)
    await client.post("wyswietl_pytania_pes.php", {})
    # Every retry (and every other request of the lane) starts only after the pause.
    assert len([s for s in limiter_sleep.calls if s > 29]) == 2
    assert caplog.text.count("pausing it for 30s") == 1  # logged once per pause, not per request


@respx.mock
async def test_stalled_lane_stops_without_exhausting_retries(client_factory) -> None:
    now = [0.0]

    def timeout(_: httpx.Request) -> httpx.Response:
        now[0] += 45  # each attempt hangs for the whole read timeout
        raise httpx.ReadTimeout("t")

    route = respx.post(QUESTION_URL).mock(side_effect=timeout)
    client = client_factory(max_retries=10)
    client.traffic.breaker = CircuitBreaker(5, stall_timeout=180, clock=lambda: now[0])
    with pytest.raises(ServerUnavailable, match="lane direct: no successful response for 180s"):
        await client.post("wyswietl_pytania_pes.php", {})
    assert route.call_count == 5  # 45 s .. 225 s; not 11 attempts
    with pytest.raises(ServerUnavailable, match="circuit breaker open on lane direct"):
        await client.post("wyswietl_pytania_pes.php", {})
    assert route.call_count == 5


def test_a_success_restarts_the_stall_clock() -> None:
    now = [0.0]
    breaker = CircuitBreaker(5, stall_timeout=180, clock=lambda: now[0])
    assert not breaker.attempt_failed()
    now[0] = 170
    breaker.success()
    now[0] = 200
    assert not breaker.attempt_failed()  # failing since 200, not since 0
    now[0] = 379
    assert not breaker.attempt_failed()
    now[0] = 380
    assert breaker.attempt_failed() and breaker.open


@respx.mock
async def test_decodes_declared_charset(client_factory) -> None:
    body = "Zażółć gęślą jaźń".encode("iso-8859-2")
    respx.post(QUESTION_URL).mock(
        return_value=httpx.Response(200, content=body, headers={"Content-Type": "text/html; charset=ISO-8859-2"})
    )
    page = await client_factory().post("wyswietl_pytania_pes.php", {})
    assert page.html == "Zażółć gęślą jaźń"


async def test_rate_limiter_spaces_requests() -> None:
    now = [0.0]
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    limiter = RateLimiter(2.0, clock=lambda: now[0], sleep=fake_sleep)
    for _ in range(3):
        await limiter.acquire()
    assert sleeps == [0.5, 1.0]


async def test_concurrency_is_capped(settings: Settings) -> None:
    import asyncio

    active = peak = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return httpx.Response(200)

    s = settings.model_copy(update={"concurrency": 2, "adaptive_concurrency": False})
    traffic = TrafficControl(s, limiter=RateLimiter(1000))
    client = CemClient(s, Metrics(), transport=httpx.MockTransport(handler), traffic=traffic)
    await asyncio.gather(*(client.get("x") for _ in range(6)))
    await client.aclose()
    assert peak == 2


def test_parse_retry_after_formats() -> None:
    now = datetime(2026, 10, 28, tzinfo=UTC)
    assert parse_retry_after("12") == 12.0
    assert parse_retry_after(format_datetime(now + timedelta(seconds=30)), now=now) == 30.0
    assert parse_retry_after("garbage") is None
    assert parse_retry_after(None) is None


def test_snapshot_sanitisation_blanks_tokens() -> None:
    html = (
        '<input type="hidden" name="g-recaptcha-response" value="SECRET1">'
        '<input type="hidden" name="csrf_token" value="SECRET2"><input name="sesja" value="20031">'
        '<textarea name="g-recaptcha-response">SECRET3</textarea>'
    )
    cleaned = sanitize_snapshot(html)
    assert "SECRET" not in cleaned and 'value="20031"' in cleaned


def test_log_redaction(caplog: pytest.LogCaptureFixture) -> None:
    assert "abc123" not in redact("Cookie: PHPSESSID=abc123")
    assert "tok" not in redact("data g-recaptcha-response=tok&x=1")
    assert "pw" not in redact("postgresql://cem:pw@localhost/cem")
    logger = logging.getLogger("redaction-test")
    logger.addFilter(RedactingFilter())
    with caplog.at_level(logging.INFO):
        logger.info("sent %s", "g-recaptcha-response=verysecret")
    assert "verysecret" not in caplog.text
