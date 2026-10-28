"""In-process collection metrics (request timings, outcomes, throughput)."""

from __future__ import annotations

import statistics
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any


def _percentile(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    rank = max(0, min(len(ordered) - 1, round(pct / 100 * len(ordered) + 0.5) - 1))
    return ordered[rank]


@dataclass
class Metrics:
    started: float = field(default_factory=time.monotonic)
    finished: float | None = None
    requests: int = 0
    request_successes: int = 0
    request_failures: int = 0
    retries: int = 0
    durations: list[float] = field(default_factory=list)
    status_codes: Counter[int] = field(default_factory=Counter)
    questions_requested: int = 0
    questions_succeeded: int = 0
    questions_failed: int = 0
    outcomes: Counter[str] = field(default_factory=Counter)
    per_session: dict[str, Counter[str]] = field(default_factory=lambda: defaultdict(Counter))
    concurrency_changes: list[dict[str, Any]] = field(default_factory=list)
    captcha_solves: int = 0
    captcha_cost: Decimal = Decimal(0)

    def record_request(self, duration: float, status_code: int | None, ok: bool) -> None:
        self.requests += 1
        self.durations.append(duration)
        if status_code is not None:
            self.status_codes[status_code] += 1
        if ok:
            self.request_successes += 1
        else:
            self.request_failures += 1

    def record_concurrency_change(self, limit: int, reason: str) -> None:
        self.concurrency_changes.append({"t": round(self.elapsed, 1), "limit": limit, "reason": reason})

    def record_captcha(self, cost: Decimal) -> None:
        self.captcha_solves += 1
        self.captcha_cost += cost

    def record_retry(self) -> None:
        self.retries += 1

    def record_question(self, session_code: str, outcome: str, success: bool) -> None:
        self.questions_requested += 1
        self.outcomes[outcome] += 1
        self.per_session[session_code][outcome] += 1
        if success:
            self.questions_succeeded += 1
        elif outcome != "missing":
            self.questions_failed += 1

    def reclassify_question(self, session_code: str, old: str, new: str) -> None:
        """Move an already-counted question outcome (e.g. auth_error -> missing)."""
        for counter in (self.outcomes, self.per_session[session_code]):
            counter[old] -= 1
            counter[new] += 1
        if old != "missing":
            self.questions_failed -= 1
        if new not in ("missing", "stored"):
            self.questions_failed += 1

    def stop(self) -> None:
        self.finished = time.monotonic()

    @property
    def elapsed(self) -> float:
        return (self.finished or time.monotonic()) - self.started

    def snapshot(self) -> dict[str, Any]:
        d = self.durations
        minutes = self.elapsed / 60
        return {
            "requests": self.requests,
            "request_successes": self.request_successes,
            "request_failures": self.request_failures,
            "retries": self.retries,
            "status_codes": {str(k): v for k, v in sorted(self.status_codes.items())},
            "duration_avg_s": round(statistics.fmean(d), 3) if d else None,
            "duration_median_s": round(statistics.median(d), 3) if d else None,
            "duration_p95_s": round(_percentile(d, 95), 3) if d else None,
            "questions_requested": self.questions_requested,
            "questions_succeeded": self.questions_succeeded,
            "questions_failed": self.questions_failed,
            "outcomes": dict(self.outcomes),
            "questions_per_minute": round(self.questions_succeeded / minutes, 2) if minutes > 0 else None,
            "total_duration_s": round(self.elapsed, 2),
            "per_session": {code: dict(c) for code, c in sorted(self.per_session.items())},
            "concurrency_changes": self.concurrency_changes,
            "captcha_solves": self.captcha_solves,
            "captcha_cost": float(self.captcha_cost),
        }

    def summary_line(self) -> str:
        s = self.snapshot()
        return (
            f"requests={s['requests']} ok={s['request_successes']} failed={s['request_failures']} "
            f"retries={s['retries']} avg={s['duration_avg_s']}s median={s['duration_median_s']}s "
            f"p95={s['duration_p95_s']}s stored={s['questions_succeeded']} "
            f"q/min={s['questions_per_minute']} total={s['total_duration_s']}s"
        )
