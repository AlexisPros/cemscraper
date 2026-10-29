"""A broken or exhausted proxy costs as little as possible: no CAPTCHA, no traffic, no stopped run.

Failure modes covered (what httpx really raises was checked against a local proxy):
- proxy refuses (407 wrong login / 402 no traffic left): ``ProxyError("407 ...")`` -> lane retired at once;
- proxy unreachable (refused, DNS, connect timeout): ``ConnectError`` -> retired after 3 in a row;
- lane stalls (no successful answer for ``stall_timeout``) -> retired, unlike the direct lane (= CEM down);
- lane reaches its paid-traffic cap -> retired before sending more;
- a session on a retired lane continues on another lane without refetching what it stored;
- no lane left -> the session is blocked and the run stops cleanly.
"""

from __future__ import annotations

import asyncio
import gzip
from urllib.parse import parse_qs

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from cem_scraper.captcha import CaptchaProvider, CaptchaSolution
from cem_scraper.client import CemClient, LaneFailed, ServerUnavailable
from cem_scraper.config import Settings
from cem_scraper.metrics import Metrics
from cem_scraper.models import ExamSession, RunStatus
from cem_scraper.orchestrator import SessionOutcome
from cem_scraper.session_manager import (
    AccessBlocked,
    AccessGrant,
    HttpFormAccessProvider,
    SessionManager,
)
from cem_scraper.throttle import CircuitBreaker, RateLimiter, TrafficControl
from tests.conftest import question_page
from tests.test_orchestrator import ScriptedProvider, build, default_responder

PROXY = "http://user-sess1:s3cret@proxy.example.net:2334"
PROXY_LANE = "proxy1 (proxy.example.net:2334)"


def proxy_client(settings: Settings, handler, **overrides: object) -> CemClient:
    s = settings.model_copy(update=overrides)
    traffic = TrafficControl(s, name=PROXY_LANE, proxied=True, limiter=RateLimiter(1000))
    return CemClient(s, Metrics(), transport=httpx.MockTransport(handler), traffic=traffic)


class Calls:
    def __init__(self, *outcomes: Exception | httpx.Response) -> None:
        self.outcomes = list(outcomes)
        self.count = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.count += 1
        outcome = self.outcomes[min(self.count, len(self.outcomes)) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


# --------------------------------------------------------------------------- client


async def test_proxy_refusal_retires_the_lane_without_retrying(settings: Settings) -> None:
    calls = Calls(httpx.ProxyError("407 Proxy Authentication Required"))
    client = proxy_client(settings, calls, max_retries=10)
    with pytest.raises(LaneFailed, match="refused the connection: 407"):
        await client.get("eula_pes.php")
    assert calls.count == 1  # a wrong login or an empty proxy balance doesn't get better by retrying


async def test_unreachable_proxy_is_retired_after_three_faults_without_pausing(settings: Settings) -> None:
    calls = Calls(httpx.ConnectError("All connection attempts failed"))
    client = proxy_client(settings, calls, max_retries=10, overload_pause=30)
    with pytest.raises(LaneFailed, match="failed 3 times in a row"):
        await client.get("eula_pes.php")
    assert calls.count == 3
    assert client.traffic.concurrency.limit == settings.concurrency  # the proxy's fault: CEM is not slowed down


async def test_a_proxy_hiccup_does_not_retire_the_lane(settings: Settings) -> None:
    calls = Calls(httpx.ConnectError("x"), httpx.ConnectError("x"), httpx.Response(200, html="ok"))
    client = proxy_client(settings, calls, max_retries=10)
    await client.get("a.php")
    calls.outcomes = [httpx.ConnectError("x"), httpx.ConnectError("x"), httpx.Response(200, html="ok")]
    calls.count = 0
    await client.get("b.php")  # the success in between reset the fault counter


async def test_stalled_proxy_lane_is_retired_while_a_stalled_direct_lane_stops_the_run(settings: Settings) -> None:
    now = [0.0]

    def hang(_: httpx.Request) -> httpx.Response:
        now[0] += 45
        raise httpx.ReadTimeout("t")

    client = proxy_client(settings, hang, max_retries=10)
    client.traffic.breaker = CircuitBreaker(5, stall_timeout=180, clock=lambda: now[0])
    with pytest.raises(LaneFailed, match="no successful response for 180s"):
        await client.get("a.php")

    direct = CemClient(
        settings.model_copy(update={"max_retries": 10}),
        Metrics(),
        transport=httpx.MockTransport(hang),
        traffic=TrafficControl(settings, limiter=RateLimiter(1000)),
    )
    direct.traffic.breaker = CircuitBreaker(5, stall_timeout=180, clock=lambda: now[0])
    with pytest.raises(ServerUnavailable):
        await direct.get("a.php")


async def test_traffic_cap_stops_a_proxy_lane_before_it_spends_more(settings: Settings) -> None:
    body = "x" * 1500
    calls = Calls(httpx.Response(200, html=body))
    client = proxy_client(settings, calls, proxy_max_mb=0.002)  # 2000 bytes
    await client.get("a.php")
    await client.get("b.php")
    assert client.traffic.bytes_used > 3000
    with pytest.raises(LaneFailed, match="traffic cap of 0 MB"):
        await client.get("c.php")
    assert calls.count == 2  # the third page was never requested
    assert client.metrics.lane_bytes[PROXY_LANE] == client.traffic.bytes_used


async def test_traffic_counts_compressed_bytes_on_a_real_socket(settings: Settings) -> None:
    raw = b"<html>" + b"x" * 20_000 + b"</html>"
    wire = gzip.compress(raw)

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        head = b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Encoding: gzip\r\nContent-Length: %d\r\n\r\n"
        writer.write(head % len(wire) + wire)
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    s = settings.model_copy(update={"base_url": "http://cem.invalid/pytcem/"})
    traffic = TrafficControl(s, name=PROXY_LANE, proxied=True, limiter=RateLimiter(1000))
    try:
        async with CemClient(s, Metrics(), traffic=traffic, proxy=SecretStr(f"http://u:p@127.0.0.1:{port}")) as client:
            page = await client.get("eula_pes.php")
    finally:
        server.close()
        await server.wait_closed()
    assert len(page.html) == len(raw)
    assert len(wire) < traffic.bytes_used < len(wire) + 1000  # what the proxy bills, not the decoded 20 KB


async def test_the_direct_lane_has_no_traffic_cap(settings: Settings) -> None:
    traffic = TrafficControl(settings.model_copy(update={"proxy_max_mb": 0.001}), limiter=RateLimiter(1000))
    assert not traffic.over_byte_cap()
    traffic.add_bytes(10**9)
    assert not traffic.over_byte_cap()


def test_the_same_proxy_twice_is_rejected() -> None:
    with pytest.raises(ValidationError, match="same proxy twice"):
        Settings(_env_file=None, access_provider="http_form", proxy_urls=f"{PROXY},{PROXY}/")  # type: ignore[call-arg]


# --------------------------------------------------------------------------- entry through a real proxy socket


class MustNotSolve(CaptchaProvider):
    name = "must-not-solve"

    def __init__(self) -> None:
        self.solves = 0

    @property
    def available(self) -> bool:
        return True

    async def solve(self, **_: object) -> CaptchaSolution:  # type: ignore[override]
        self.solves += 1
        raise AssertionError("a CAPTCHA was bought for a lane whose proxy does not work")


async def test_proxy_with_wrong_login_buys_no_captcha_and_is_retired(settings: Settings) -> None:
    """A local proxy answering 407 to CONNECT, as a proxy with a wrong login or no traffic left does."""
    connects: list[str] = []

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        connects.append((await reader.readuntil(b"\r\n\r\n")).split(b"\r\n")[0].decode())
        writer.write(b"HTTP/1.1 407 Proxy Authentication Required\r\nContent-Length: 0\r\n\r\n")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    s = Settings(
        _env_file=None,  # type: ignore[call-arg]
        access_provider="http_form",
        accept_eula=True,
        base_url="https://cem.invalid/pytcem/",
        proxy_urls=f"http://user:wrong@127.0.0.1:{port}",
        proxy_include_direct=False,
        max_retries=4,
        overload_pause=0,
    )
    captcha = MustNotSolve()
    manager = SessionManager(s, HttpFormAccessProvider(s, captcha), Metrics())
    try:
        with pytest.raises(LaneFailed, match="407"):
            await manager.open(ExamSession.from_code("20031"))
        with pytest.raises(AccessBlocked, match="no working lane left"):
            await manager.open(ExamSession.from_code("20032"))
    finally:
        server.close()
        await server.wait_closed()
    assert captcha.solves == 0
    assert connects == ["CONNECT cem.invalid:443 HTTP/1.1"]  # one attempt, then the lane was retired
    assert manager.lanes[0].active == 0


# --------------------------------------------------------------------------- orchestrator


class ProxyBreaksAfter(ScriptedProvider):
    """Sessions entered on the proxy lane get a transport that serves ``ok`` pages, then fails."""

    def __init__(self, total: int, ok: int, error: Exception) -> None:
        super().__init__(default_responder(total), question_limit=total)
        self.ok = ok
        self.error = error
        self.entries: list[tuple[str, str]] = []
        self.via_proxy: list[tuple[str, int]] = []

    def _proxy(self, request: httpx.Request) -> httpx.Response:
        if self.ok <= 0:
            raise self.error
        self.ok -= 1
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        self.via_proxy.append((form["sesja"], int(form["nr_pytania"])))
        return httpx.Response(200, html=question_page(number=int(form["nr_pytania"])))

    async def acquire(self, session: ExamSession, client: CemClient) -> AccessGrant:
        grant = await super().acquire(session, client)
        self.entries.append((session.session_code, client.traffic.name))
        if client.traffic.proxied:
            grant.transport = httpx.MockTransport(self._proxy)
        return grant


def lanes(settings: Settings, **update: object) -> Settings:
    return settings.model_copy(
        update={"proxy_urls": [SecretStr(PROXY)], "requests_per_second": 10_000, "missing_streak_to_stop": 3, **update}
    )


@pytest.mark.parametrize(
    "error",
    [httpx.ProxyError("407 Proxy Authentication Required"), httpx.ConnectError("proxy gone")],
    ids=["refused", "unreachable"],
)
async def test_session_moves_to_another_lane_and_keeps_what_it_stored(settings: Settings, error: Exception) -> None:
    provider = ProxyBreaksAfter(total=6, ok=2, error=error)
    orchestrator, _ = build(lanes(settings, pipeline_depth=1), provider)
    report = await orchestrator.run(["20031", "20032"])

    assert report.status is RunStatus.COMPLETED
    assert [r.stored for r in report.sessions] == [6, 6]
    moved = next(code for code, lane in provider.entries if lane == PROXY_LANE)
    assert provider.entries.count((moved, "direct")) == 1  # one new entry (CAPTCHA) on the working lane
    stored_via_proxy = {n for code, n in provider.via_proxy if code == moved}
    assert len(stored_via_proxy) == 2
    assert not stored_via_proxy & set(provider.numbers(moved))  # not downloaded twice
    assert PROXY_LANE in report.metrics["lanes"] and report.metrics["lanes"][PROXY_LANE]["disabled"]


async def test_no_lane_left_blocks_cleanly(settings: Settings) -> None:
    provider = ProxyBreaksAfter(total=6, ok=0, error=httpx.ProxyError("407 Proxy Authentication Required"))
    orchestrator, _ = build(lanes(settings, pipeline_depth=1, proxy_include_direct=False), provider)
    report = await orchestrator.run(["20031", "20032"])
    assert report.status is RunStatus.BLOCKED
    assert report.sessions[0].outcome is SessionOutcome.BLOCKED
    assert "no working lane left" in (report.sessions[0].error or "")
    assert provider.grants == 1  # the second session asked for no entry (and no CAPTCHA)
