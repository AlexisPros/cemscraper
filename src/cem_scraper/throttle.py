"""Server-facing traffic control shared by every HTTP client of a run.

- ``RateLimiter``: spaces request *starts*; ``Retry-After`` defers all of them.
- ``AdaptiveConcurrency``: how many requests may be in flight at once.
- ``TrafficControl``: bundles both with a run-wide circuit breaker.

Sharing matters once several examination sessions are collected in parallel:
the server must see one request stream with one rate, not one per session.
"""

from __future__ import annotations

import asyncio
import logging
import math
import statistics
import time
from collections import deque
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cem_scraper.config import Settings
    from cem_scraper.metrics import Metrics

log = logging.getLogger(__name__)


class RateLimiter:
    """Spaces request *starts* at least ``1 / rate`` seconds apart."""

    def __init__(
        self,
        rate: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.rate = rate
        self._interval = 1.0 / rate
        self._clock = clock
        self._sleep = sleep
        self._lock = asyncio.Lock()
        self._next_slot = 0.0

    def defer(self, seconds: float) -> None:
        """Hold back *all* future request starts by ``seconds`` (the server asked us to wait)."""
        self._next_slot = max(self._next_slot, self._clock() + seconds)

    async def acquire(self) -> None:
        async with self._lock:
            now = self._clock()
            wait = self._next_slot - now
            self._next_slot = max(now, self._next_slot) + self._interval
        if wait > 0:
            await self._sleep(wait)


class AdaptiveConcurrency:
    """In-flight request limit that follows Little's law within ``[minimum, maximum]``.

    Sustaining ``rate`` requests/s at latency ``L`` needs ``rate * L`` requests in
    flight. CEM answers either in ~0.2 s or in ~10 s; with a fixed limit of 2 the
    slow phase drops throughput to 0.2 req/s although the rate limit allows 0.5.
    More in-flight requests never raise the request *rate* (the ``RateLimiter``
    still spaces every start); they only stop slow responses from idling the
    allowed rate.

    Evaluated once per window of completed requests (``max(3, limit)``):

    - overload in flight (timeout, 429, 5xx) -> halve the limit at once, pause increases;
    - after a +1 probe, median latency grew by more than ``latency_tolerance``
      -> the server is queueing because of us: undo and pause increases;
    - limit below the target ``ceil(rate * median latency)`` -> probe +1;
    - limit more than one above the target -> -1.

    Each change starts a new *epoch*; results of requests started in an older
    epoch neither count towards the next window nor trigger another halving, so
    one burst of failures halves the limit once, not once per request.
    """

    def __init__(
        self,
        initial: int,
        *,
        minimum: int = 1,
        maximum: int,
        rate: float,
        enabled: bool = True,
        latency_tolerance: float = 1.5,
        hold_windows: int = 3,
        on_change: Callable[[int, str], None] | None = None,
    ) -> None:
        if not minimum <= initial <= maximum:
            raise ValueError(f"need {minimum} <= initial ({initial}) <= maximum ({maximum})")
        self.limit = initial
        self.minimum = minimum
        self.maximum = maximum if enabled else initial
        self.rate = rate
        self.enabled = enabled
        self.latency_tolerance = latency_tolerance
        self.hold_windows = hold_windows
        self._on_change = on_change
        self._in_flight = 0
        self._waiters: deque[asyncio.Future[None]] = deque()
        self._epoch = 0
        self._samples: list[float] = []
        self._hold = 0
        self._probe_baseline: float | None = None

    @property
    def in_flight(self) -> int:
        return self._in_flight

    async def acquire(self) -> int:
        """Wait for a free slot; returns the epoch ticket to pass to ``release``."""
        loop = asyncio.get_running_loop()
        while self._in_flight >= self.limit:
            waiter: asyncio.Future[None] = loop.create_future()
            self._waiters.append(waiter)
            try:
                await waiter
            except asyncio.CancelledError:
                if waiter in self._waiters:
                    self._waiters.remove(waiter)
                else:  # woken and cancelled at the same time: hand the slot on
                    self._wake()
                raise
        self._in_flight += 1
        return self._epoch

    def release(self, ticket: int, latency: float | None, *, overloaded: bool = False) -> None:
        self._in_flight -= 1
        if self.enabled:
            self._observe(ticket, latency, overloaded)
        self._wake()

    def _wake(self) -> None:
        free = self.limit - self._in_flight
        while free > 0 and self._waiters:
            waiter = self._waiters.popleft()
            if not waiter.done():
                waiter.set_result(None)
                free -= 1

    def _set(self, limit: int, reason: str) -> None:
        limit = max(self.minimum, min(self.maximum, limit))
        self._epoch += 1
        self._samples.clear()
        if limit == self.limit:
            return
        log.info("concurrency %d -> %d (%s)", self.limit, limit, reason)
        self.limit = limit
        if self._on_change:
            self._on_change(limit, reason)

    def _observe(self, ticket: int, latency: float | None, overloaded: bool) -> None:
        if ticket != self._epoch:
            return  # started under an older limit: says nothing about the current one
        if overloaded:
            self._probe_baseline = None
            self._hold = self.hold_windows
            self._set(self.limit // 2, "server overloaded")
            return
        if latency is None:
            return
        self._samples.append(latency)
        if len(self._samples) < max(3, self.limit):
            return

        median = statistics.median(self._samples)
        target = max(self.minimum, min(self.maximum, math.ceil(self.rate * median)))
        baseline, self._probe_baseline = self._probe_baseline, None
        if baseline is not None and median > baseline * self.latency_tolerance:
            self._hold = self.hold_windows
            self._set(self.limit - 1, f"latency rose {baseline:.2f}s -> {median:.2f}s after increase")
        elif self.limit < target and self._hold == 0:
            self._probe_baseline = median
            self._set(self.limit + 1, f"median latency {median:.2f}s needs ~{target} in flight")
        elif self.limit > target + 1:
            self._set(self.limit - 1, f"median latency {median:.2f}s needs only ~{target}")
        else:
            self._hold = max(0, self._hold - 1)
            self._samples.clear()  # next window; in-flight requests still count (same limit)


class CircuitBreaker:
    """Opens after ``threshold`` consecutive requests failed even after retries."""

    def __init__(self, threshold: int) -> None:
        self.threshold = threshold
        self.consecutive_failures = 0
        self.open = False

    def success(self) -> None:
        self.consecutive_failures = 0

    def failure(self) -> bool:
        """Record a failure; returns True when the breaker is (now) open."""
        self.consecutive_failures += 1
        if self.consecutive_failures >= self.threshold:
            self.open = True
        return self.open


class TrafficControl:
    """Rate limit + in-flight limit + circuit breaker, shared by all clients of a run."""

    def __init__(
        self,
        settings: Settings,
        metrics: Metrics | None = None,
        *,
        limiter: RateLimiter | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.limiter = limiter or RateLimiter(settings.requests_per_second, sleep=sleep)
        self.concurrency = AdaptiveConcurrency(
            settings.concurrency,
            maximum=settings.max_concurrency,
            rate=settings.requests_per_second,
            enabled=settings.adaptive_concurrency,
            latency_tolerance=settings.latency_tolerance,
            on_change=metrics.record_concurrency_change if metrics else None,
        )
        self.breaker = CircuitBreaker(settings.circuit_breaker_threshold)
