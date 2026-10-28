from __future__ import annotations

import asyncio

import httpx
import pytest
import respx
from pydantic import ValidationError

from cem_scraper.client import CemClient
from cem_scraper.config import Settings
from cem_scraper.metrics import Metrics
from cem_scraper.throttle import AdaptiveConcurrency, RateLimiter, TrafficControl

QUESTION_URL = "https://www.cem.edu.pl/pytcem/wyswietl_pytania_pes.php"


def controller(initial: int = 2, *, maximum: int = 4, rate: float = 0.5, **kw: object) -> AdaptiveConcurrency:
    return AdaptiveConcurrency(initial, maximum=maximum, rate=rate, **kw)  # type: ignore[arg-type]


async def complete(c: AdaptiveConcurrency, latency: float, n: int = 1, *, overloaded: bool = False) -> None:
    """Run ``n`` requests one after another with the given latency."""
    for _ in range(n):
        ticket = await c.acquire()
        c.release(ticket, None if overloaded else latency, overloaded=overloaded)


async def window(c: AdaptiveConcurrency, latency: float) -> None:
    await complete(c, latency, max(3, c.limit))


async def test_slow_server_raises_limit_towards_littles_law_target() -> None:
    changes: list[int] = []
    c = controller(on_change=lambda limit, _: changes.append(limit))
    for _ in range(4):
        await window(c, 10.0)  # 0.5 req/s * 10 s = 5 in flight needed, capped at 4
    assert c.limit == 4
    assert changes == [3, 4]


async def test_fast_server_keeps_or_lowers_limit() -> None:
    c = controller()
    await window(c, 0.2)  # needs ~1 in flight
    assert c.limit == 2  # within one of the target: unchanged
    high = controller(4)
    for _ in range(3):
        await window(high, 0.2)
    assert high.limit == 2


async def test_overload_halves_once_per_burst_and_pauses_increases() -> None:
    c = controller(4)
    tickets = [await c.acquire() for _ in range(4)]
    for ticket in tickets:  # four failures of the same burst
        c.release(ticket, None, overloaded=True)
    assert c.limit == 2  # halved once, not 4 -> 2 -> 1 -> ...
    for _ in range(3):  # hold_windows=3: no increase despite slow responses
        await window(c, 10.0)
    assert c.limit == 2
    await window(c, 10.0)
    assert c.limit == 3


async def test_probe_is_undone_when_latency_grows() -> None:
    c = controller()
    await window(c, 2.0)  # target ceil(0.5 * 2) = 1 -> no probe yet
    await window(c, 8.0)  # target 4 -> probe 2 -> 3, baseline 8 s
    assert c.limit == 3
    await window(c, 16.0)  # latency doubled after the increase: we are the problem
    assert c.limit == 2


async def test_disabled_keeps_fixed_limit() -> None:
    c = controller(enabled=False)
    for _ in range(5):
        await window(c, 10.0)
    await complete(c, 0, overloaded=True)
    assert c.limit == 2


async def test_bounds_respected() -> None:
    c = controller(1, maximum=1)
    await complete(c, 0, overloaded=True)
    assert c.limit == 1
    for _ in range(5):
        await window(c, 30.0)
    assert c.limit == 1
    with pytest.raises(ValueError):
        controller(5, maximum=4)


async def test_waiters_are_released_and_cancellation_does_not_leak() -> None:
    c = controller(1, enabled=False)
    first = await c.acquire()
    waiter = asyncio.create_task(c.acquire())
    cancelled = asyncio.create_task(c.acquire())
    await asyncio.sleep(0)
    assert not waiter.done()
    cancelled.cancel()
    await asyncio.sleep(0)
    c.release(first, 0.1)
    ticket = await asyncio.wait_for(waiter, 1)
    assert c.in_flight == 1
    c.release(ticket, 0.1)
    assert c.in_flight == 0


async def test_rate_limiter_defer_holds_back_everyone() -> None:
    now = [100.0]
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    limiter = RateLimiter(10.0, clock=lambda: now[0], sleep=fake_sleep)
    limiter.defer(30)
    await limiter.acquire()
    await limiter.acquire()
    assert [round(s, 1) for s in sleeps] == [30.0, 30.1]


@respx.mock
async def test_client_reports_overload_to_shared_controller(settings: Settings) -> None:
    respx.post(QUESTION_URL).mock(side_effect=[httpx.Response(503), httpx.Response(200)])
    traffic = TrafficControl(settings, limiter=RateLimiter(1000))
    assert traffic.concurrency.limit == 2
    async with CemClient(settings, Metrics(), traffic=traffic) as client:
        await client.post("wyswietl_pytania_pes.php", {})
    assert traffic.concurrency.limit == 1


@respx.mock
async def test_circuit_breaker_is_shared_between_clients(settings: Settings) -> None:
    respx.post(QUESTION_URL).mock(side_effect=httpx.ReadTimeout("t"))
    s = settings.model_copy(update={"max_retries": 0, "circuit_breaker_threshold": 2})
    traffic = TrafficControl(s, limiter=RateLimiter(1000))
    from cem_scraper.client import RequestFailed, ServerUnavailable

    async with CemClient(s, Metrics(), traffic=traffic) as a, CemClient(s, Metrics(), traffic=traffic) as b:
        with pytest.raises(RequestFailed):
            await a.post("wyswietl_pytania_pes.php", {})
        with pytest.raises(ServerUnavailable):
            await b.post("wyswietl_pytania_pes.php", {})


def test_settings_reject_max_below_initial() -> None:
    with pytest.raises(ValidationError, match="max_concurrency"):
        Settings(_env_file=None, concurrency=4, max_concurrency=2)  # type: ignore[call-arg]


async def test_metrics_record_limit_changes() -> None:
    metrics = Metrics()
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    traffic = TrafficControl(s, metrics)
    for _ in range(2):
        await window(traffic.concurrency, 10.0)
    assert [c["limit"] for c in metrics.snapshot()["concurrency_changes"]] == [3, 4]
