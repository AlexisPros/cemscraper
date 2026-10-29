"""Proxy lanes: configuration, routing through a proxy, spreading sessions over lanes."""

from __future__ import annotations

import asyncio
import base64

import pytest
import respx
from pydantic import SecretStr, ValidationError

from cem_scraper.captcha import CaptchaUnavailable, TwoCaptchaProvider
from cem_scraper.client import CemClient
from cem_scraper.config import AccessProviderName, Settings
from cem_scraper.metrics import Metrics
from cem_scraper.models import ExamSession
from cem_scraper.session_manager import SessionManager
from tests.test_captcha import API, READY, ok
from tests.test_orchestrator import ScriptedProvider, build, default_responder

PROXY = "http://user-sess1:s3cret@proxy.example.net:2334"


def test_proxy_urls_from_env_string_keep_the_password_out_of_repr() -> None:
    s = Settings(_env_file=None, access_provider="http_form", proxy_urls=f"{PROXY}, http://u:p@10.0.0.2:8080")  # type: ignore[call-arg]
    assert [name for name, _ in s.lane_specs()] == [
        "direct",
        "proxy1 (proxy.example.net:2334)",
        "proxy2 (10.0.0.2:8080)",
    ]
    assert "s3cret" not in repr(s) and "s3cret" not in str(s.lane_specs())
    only_proxies = s.model_copy(update={"proxy_include_direct": False})
    assert [name for name, _ in only_proxies.lane_specs()] == [
        "proxy1 (proxy.example.net:2334)",
        "proxy2 (10.0.0.2:8080)",
    ]


@pytest.mark.parametrize("bad", ["proxy.example.net:2334", "socks5://u:p@h:1080", "http://user:s3cret@host"])
def test_malformed_proxy_is_rejected_without_echoing_it(bad: str) -> None:
    with pytest.raises(ValidationError) as info:
        Settings(_env_file=None, access_provider="http_form", proxy_urls=bad)  # type: ignore[call-arg]
    assert "s3cret" not in str(info.value)


def test_proxies_need_the_http_form_provider() -> None:
    with pytest.raises(ValidationError, match="only with CEM_ACCESS_PROVIDER=http_form"):
        Settings(_env_file=None, access_provider=AccessProviderName.MANUAL_CHROME, proxy_urls=PROXY)  # type: ignore[call-arg]


async def test_client_sends_requests_through_the_proxy_with_its_credentials(settings: Settings) -> None:
    """A tiny local proxy: it answers itself and records what the client sent."""
    seen: list[list[str]] = []

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = (await reader.readuntil(b"\r\n\r\n")).decode().split("\r\n")
        seen.append(head)
        body = b"<html><body>ok</body></html>"
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: %d\r\n\r\n%s" % (len(body), body))
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    s = settings.model_copy(update={"base_url": "http://cem.invalid/pytcem/"})
    try:
        async with CemClient(s, Metrics(), proxy=SecretStr(f"http://user-sess1:s3cret@127.0.0.1:{port}")) as client:
            page = await client.get("eula_pes.php")
    finally:
        server.close()
        await server.wait_closed()
    assert "ok" in page.html
    request_line, *headers = seen[0]
    assert request_line == "GET http://cem.invalid/pytcem/eula_pes.php HTTP/1.1"  # absolute form: sent to the proxy
    sent = dict(h.split(": ", 1) for h in headers if h)
    sent = {k.lower(): v for k, v in sent.items()}
    assert sent["proxy-authorization"] == "Basic " + base64.b64encode(b"user-sess1:s3cret").decode()


def lane_settings(settings: Settings, **update: object) -> Settings:
    return settings.model_copy(
        update={"proxy_urls": [SecretStr(PROXY)], "requests_per_second": 10_000, "missing_streak_to_stop": 3, **update}
    )


async def test_sessions_spread_over_lanes_and_reauthorize_on_the_same_one(settings: Settings) -> None:
    s = lane_settings(settings, max_reauthorizations_per_session=1)
    manager = SessionManager(s, ScriptedProvider(default_responder(5)), Metrics())
    direct, proxy = manager.lanes
    first = await manager.open(ExamSession.from_code("20031"))
    second = await manager.open(ExamSession.from_code("20032"))
    assert (first.lane, second.lane) == (direct, proxy)
    assert first.client.traffic is direct.traffic and second.client.traffic is proxy.traffic
    assert direct.traffic is not proxy.traffic  # each IP has its own rate limit

    again = await manager.reauthorize(second)
    assert again.lane is proxy and proxy.active == 1  # the cookie stays on the IP that obtained it
    await manager.release(second)  # releasing the expired grant again changes nothing
    await manager.release(again)
    await manager.release(first)
    assert (direct.active, proxy.active) == (0, 0)


async def test_run_uses_every_lane(settings: Settings) -> None:
    s = lane_settings(settings, pipeline_depth=1)
    provider = ScriptedProvider(default_responder(4))
    orchestrator, _ = build(s, provider)
    report = await orchestrator.run(["20031", "20032", "20041", "20042"])
    assert all(r.stored == 4 for r in report.sessions)
    lanes = report.metrics["lanes"]
    assert set(lanes) == {"direct", "proxy1 (proxy.example.net:2334)"}
    assert all(lane["requests"] > 0 and lane["failures"] == 0 for lane in lanes.values())


@respx.mock
async def test_parallel_solves_on_two_lanes_cannot_exceed_the_solve_cap() -> None:
    respx.post(f"{API}/getBalance").mock(return_value=ok(balance=1))
    create = respx.post(f"{API}/createTask").mock(return_value=ok(taskId=1))
    respx.post(f"{API}/getTaskResult").mock(return_value=READY)

    async def yielding_sleep(_: float) -> None:
        await asyncio.sleep(0)

    p = TwoCaptchaProvider(SecretStr("TEST-KEY"), max_solves=1, sleep=yielding_sleep)
    results = await asyncio.gather(
        p.solve(site_key="k", page_url="u"), p.solve(site_key="k", page_url="u"), return_exceptions=True
    )
    await p.close()
    assert sum(isinstance(r, CaptchaUnavailable) for r in results) == 1
    assert create.call_count == 1 and p.solves == 1
