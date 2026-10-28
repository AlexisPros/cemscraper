"""Orchestrator integration tests over an in-process scripted transport (no network)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from cem_scraper.client import CemClient
from cem_scraper.config import Settings
from cem_scraper.database import InMemoryRepository, PostgresRepository
from cem_scraper.exporter import export_records
from cem_scraper.metrics import Metrics
from cem_scraper.models import AttemptStatus, CollectionStatus, ExamSession, RunStatus
from cem_scraper.orchestrator import Orchestrator, SessionOutcome
from cem_scraper.session_manager import (
    AccessBlocked,
    AccessGrant,
    AccessProvider,
    FixtureAccessProvider,
    SessionManager,
)
from tests.conftest import EDGE, SYNTHETIC, load, question_page

NO_QUESTION = '<html><body><div class="marginesy"><div><p>Brak</p></div></div></body></html>'
ENTRY_FORM = load(EDGE / "entry_form.html")
Responder = Callable[[str, int, "ScriptedProvider"], httpx.Response | str]


def default_responder(total: int) -> Responder:
    def respond(session: str, number: int, provider: ScriptedProvider) -> str:
        return question_page(number=number) if number <= total else NO_QUESTION

    return respond


class ScriptedProvider(AccessProvider):
    """Test access provider: every grant succeeds unless ``grants_left`` runs out."""

    name = "scripted"

    def __init__(self, responder: Responder, *, grants_left: int | None = None, first_page: bool = False) -> None:
        self.responder = responder
        self.grants_left = grants_left
        self.first_page = first_page
        self.grants = 0
        self.requested: list[tuple[str, int]] = []

    def _handler(self, request: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        session, number = form["sesja"], int(form["nr_pytania"])
        self.requested.append((session, number))
        result = self.responder(session, number, self)
        return result if isinstance(result, httpx.Response) else httpx.Response(200, html=result)

    def base_transport(self) -> httpx.AsyncBaseTransport:
        return httpx.MockTransport(self._handler)

    async def acquire(self, session: ExamSession, client: CemClient) -> AccessGrant:
        if self.grants_left is not None:
            if self.grants_left <= 0:
                raise AccessBlocked("scripted: no authorization available")
            self.grants_left -= 1
        self.grants += 1
        first = question_page(number=1) if self.first_page else None
        return AccessGrant(session_code=session.session_code, provider=self.name, first_page_html=first)

    def numbers(self, session: str = "20031") -> list[int]:
        return [n for s, n in self.requested if s == session]


@pytest.fixture
def fast(settings: Settings) -> Settings:
    # model_copy skips validation, allowing a test-only request rate.
    return settings.model_copy(update={"requests_per_second": 10_000, "missing_streak_to_stop": 3})


def build(settings: Settings, provider: AccessProvider, repo=None) -> tuple[Orchestrator, InMemoryRepository]:
    repo = repo or InMemoryRepository()
    metrics = Metrics()
    return Orchestrator(settings, repo, SessionManager(settings, provider, metrics), metrics), repo


async def test_discovery_with_gap_using_fixtures(fast: Settings) -> None:
    provider = FixtureAccessProvider(SYNTHETIC)
    orchestrator, repo = build(fast, provider)
    report = await orchestrator.run(["20031"])
    (session,) = report.sessions
    assert report.status is RunStatus.COMPLETED
    assert session.outcome is SessionOutcome.COMPLETED
    assert (session.stored, session.discovered_question_count, session.missing) == (4, 5, [4])
    records = await repo.fetch_records()
    assert [r["question_number"] for r in records] == [1, 2, 3, 5]
    assert records[0]["correct_answer"] == {"option": "A"}
    assert report.metrics["questions_succeeded"] == 4
    assert report.metrics["per_session"]["20031"]["stored"] == 4


async def test_question_count_not_assumed(fast: Settings) -> None:
    provider = ScriptedProvider(default_responder(13))
    orchestrator, repo = build(fast, provider)
    report = await orchestrator.run(["20031"])
    assert report.sessions[0].discovered_question_count == 13
    assert max(provider.numbers()) <= 13 + 3 + fast.concurrency


async def test_reported_total_bounds_discovery(fast: Settings) -> None:
    def respond(session: str, number: int, _: ScriptedProvider) -> str:
        return question_page(number=number, center=f"Pytanie nr {number} z 4") if number <= 4 else NO_QUESTION

    provider = ScriptedProvider(respond)
    orchestrator, _ = build(fast.model_copy(update={"missing_streak_to_stop": 1}), provider)
    await orchestrator.run(["20031"])
    assert max(provider.numbers()) == 5


async def test_first_page_from_grant_is_not_refetched(fast: Settings) -> None:
    provider = ScriptedProvider(default_responder(4), first_page=True)
    orchestrator, repo = build(fast, provider)
    await orchestrator.run(["20031"])
    assert 1 not in provider.numbers()
    assert len(await repo.fetch_records()) == 4


async def test_resume_skips_stored_and_retries_failures(fast: Settings) -> None:
    broken = {3}

    def respond(session: str, number: int, _: ScriptedProvider) -> httpx.Response | str:
        if number in broken:
            return httpx.Response(500)
        return default_responder(6)(session, number, _)

    provider = ScriptedProvider(respond)
    orchestrator, repo = build(fast, provider)
    first = await orchestrator.run(["20031"])
    assert first.status is RunStatus.PARTIAL
    assert first.sessions[0].outcome is SessionOutcome.INCOMPLETE
    assert first.sessions[0].failed.keys() == {3}
    assert first.metrics["retries"] == fast.max_retries  # one failing question used its full retry budget

    broken.clear()
    provider.requested.clear()
    orchestrator2, _ = build(fast, provider, repo)
    second = await orchestrator2.run(["20031"])
    assert second.status is RunStatus.COMPLETED
    assert 1 not in provider.numbers() and 2 not in provider.numbers()
    assert 3 in provider.numbers()
    assert len(await repo.fetch_records()) == 6


async def test_retry_failed_only_reprocesses_failures(fast: Settings) -> None:
    bad = {2: "<p>x</p>"}  # unrecognisable page -> unexpected_content

    def respond(session: str, number: int, _: ScriptedProvider) -> str:
        return bad.get(number) or default_responder(4)(session, number, _)

    provider = ScriptedProvider(respond)
    orchestrator, repo = build(fast, provider)
    await orchestrator.run(["20031"])
    state = await repo.ensure_session(ExamSession.from_code("20031"))
    assert (await repo.attempt_statuses(state.id))[2] is AttemptStatus.UNEXPECTED_CONTENT
    assert state.collection_status is CollectionStatus.FAILED

    bad.clear()
    provider.requested.clear()
    orchestrator2, _ = build(fast, provider, repo)
    report = await orchestrator2.run(["20031"], only_failed=True)
    assert provider.numbers() == [2]
    assert report.sessions[0].outcome is SessionOutcome.COMPLETED


async def test_completed_session_skipped_unless_refetch(fast: Settings) -> None:
    provider = ScriptedProvider(default_responder(3))
    orchestrator, repo = build(fast, provider)
    await orchestrator.run(["20031"])
    provider.requested.clear()
    report = await build(fast, provider, repo)[0].run(["20031"])
    assert report.sessions[0].outcome is SessionOutcome.SKIPPED and provider.requested == []
    await build(fast, provider, repo)[0].run(["20031"], refetch=True)
    assert set(provider.numbers()) >= {1, 2, 3}


async def test_parse_failures_classified(fast: Settings) -> None:
    pages = {
        2: question_page(number=9),  # wrong number
        3: question_page(number=3, stats_rows=[["Procent odp. A", "x"], ["y", "z"]]),  # bad stats
    }

    def respond(session: str, number: int, _: ScriptedProvider) -> str:
        return pages.get(number) or default_responder(5)(session, number, _)

    orchestrator, repo = build(fast, ScriptedProvider(respond))
    report = await orchestrator.run(["20031"])
    state = await repo.ensure_session(ExamSession.from_code("20031"))
    statuses = await repo.attempt_statuses(state.id)
    assert statuses[2] is AttemptStatus.NUMBER_MISMATCH
    assert statuses[3] is AttemptStatus.PARSE_ERROR
    assert report.sessions[0].discovered_question_count == 5  # one failure does not stop collection


async def test_entry_form_for_out_of_range_number_with_valid_session(fast: Settings) -> None:
    """Server answers past-the-end numbers with the entry form, but the session still works."""

    def respond(session: str, number: int, _: ScriptedProvider) -> str:
        return question_page(number=number) if number <= 3 else ENTRY_FORM

    provider = ScriptedProvider(respond)
    orchestrator, repo = build(fast, provider)
    report = await orchestrator.run(["20031"])
    assert report.status is RunStatus.COMPLETED
    assert report.sessions[0].discovered_question_count == 3
    assert provider.grants == 1


async def test_expired_authorization_is_reacquired(fast: Settings) -> None:
    state = {"expired": False}

    def respond(session: str, number: int, provider: ScriptedProvider) -> str:
        if number == 3 and provider.grants == 1:
            state["expired"] = True
        if state["expired"] and provider.grants == 1:
            return ENTRY_FORM
        return default_responder(5)(session, number, provider)

    provider = ScriptedProvider(respond)
    orchestrator, repo = build(fast.model_copy(update={"max_reauthorizations_per_session": 1}), provider)
    report = await orchestrator.run(["20031"])
    assert provider.grants == 2
    assert report.status is RunStatus.COMPLETED
    assert [r["question_number"] for r in await repo.fetch_records()] == [1, 2, 3, 4, 5]


async def test_expired_authorization_without_reauth_blocks_run(fast: Settings) -> None:
    def respond(session: str, number: int, _: ScriptedProvider) -> str:
        return question_page(number=number) if number < 3 else ENTRY_FORM

    provider = ScriptedProvider(respond, grants_left=1)
    orchestrator, repo = build(fast.model_copy(update={"max_reauthorizations_per_session": 0}), provider)
    # Probe of a known question also fails once authorization is gone:
    provider.responder = lambda s, n, p: ENTRY_FORM if p.requested.count((s, n)) > 1 or n >= 3 else question_page(number=n)
    report = await orchestrator.run(["20031", "20262"])
    assert report.status is RunStatus.BLOCKED
    assert [s.session_code for s in report.sessions] == ["20031"]  # stops; does not continue to 20262
    assert report.sessions[0].outcome is SessionOutcome.BLOCKED
    state = await repo.ensure_session(ExamSession.from_code("20031"))
    assert state.collection_status is CollectionStatus.BLOCKED
    assert len(await repo.fetch_records()) == 2  # progress kept


async def test_access_blocked_at_entry(fast: Settings) -> None:
    provider = ScriptedProvider(default_responder(3), grants_left=0)
    orchestrator, _ = build(fast, provider)
    report = await orchestrator.run(["20031", "20262"])
    assert report.status is RunStatus.BLOCKED
    assert "no authorization available" in (report.sessions[0].error or "")
    assert provider.requested == []


async def test_server_failure_aborts_run(fast: Settings) -> None:
    provider = ScriptedProvider(lambda s, n, p: httpx.Response(503))
    orchestrator, repo = build(fast.model_copy(update={"max_retries": 0, "circuit_breaker_threshold": 2}), provider)
    report = await orchestrator.run(["20031", "20262"])
    assert report.status is RunStatus.FAILED
    assert report.sessions[0].outcome is SessionOutcome.ABORTED
    assert len(report.sessions) == 1


async def test_cancellation_marks_run_interrupted(fast: Settings) -> None:
    gate = asyncio.Event()

    class SlowProvider(ScriptedProvider):
        def base_transport(self) -> httpx.AsyncBaseTransport:
            outer = self

            class Slow(httpx.AsyncBaseTransport):
                async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
                    response = outer._handler(request)
                    if outer.numbers()[-1] >= 3:
                        gate.set()
                        await asyncio.sleep(30)
                    return response

            return Slow()

    provider = SlowProvider(default_responder(10))
    orchestrator, repo = build(fast, provider)
    task = asyncio.create_task(orchestrator.run(["20031"]))
    await gate.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    (run,) = repo.runs.values()
    assert run["status"] is RunStatus.INTERRUPTED
    state = await repo.ensure_session(ExamSession.from_code("20031"))
    assert state.collection_status is CollectionStatus.IN_PROGRESS
    assert len(await repo.fetch_records()) == 2


@pytest.mark.db
async def test_resume_with_postgres(fast: Settings, pg_repo: PostgresRepository) -> None:
    crash_at = {4}

    def respond(session: str, number: int, _: ScriptedProvider) -> str:
        if number in crash_at:
            raise RuntimeError("simulated crash")
        return default_responder(6)(session, number, _)

    provider = ScriptedProvider(respond)
    orchestrator, _ = build(fast.model_copy(update={"concurrency": 1}), provider, pg_repo)
    with pytest.raises(BaseException):
        await orchestrator.run(["20031"])
    assert len(await pg_repo.fetch_records()) == 3

    crash_at.clear()
    provider.requested.clear()
    report = await build(fast, provider, pg_repo)[0].run(["20031"])
    assert report.status is RunStatus.COMPLETED
    assert min(provider.numbers()) == 4  # 1-3 not downloaded again
    assert [r["question_number"] for r in await pg_repo.fetch_records()] == [1, 2, 3, 4, 5, 6]
    runs = await pg_repo.last_runs()
    assert [r["status"] for r in runs] == ["completed", "failed"]


async def test_export_json_and_jsonl(fast: Settings, tmp_path: Path) -> None:
    orchestrator, repo = build(fast, FixtureAccessProvider(SYNTHETIC))
    await orchestrator.run(["20031", "20262"])
    records = await repo.fetch_records()
    assert export_records(records, tmp_path / "out.json", "json") == 6
    assert export_records(records, tmp_path / "out.jsonl", "jsonl", include_validation=True) == 6
    data = json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))
    assert data[0]["question_data"]["options"]["A"] == "pląsawicy Huntingtona."
    assert "validation_status" not in data[0]
    lines = (tmp_path / "out.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[-1])["validation_status"] == "valid"
    assert "ą" in (tmp_path / "out.json").read_text(encoding="utf-8")  # not \u-escaped
