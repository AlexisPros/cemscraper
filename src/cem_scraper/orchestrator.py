"""Collection orchestrator: session iteration, question discovery, resume, failure handling.

Discovery: questions are requested from 1 upwards in batches of
``concurrency``. Collection of a session ends after ``missing_streak_to_stop``
consecutive *missing* questions past the highest question found (gaps inside
the range are recorded as missing and do not stop collection), or when the page
reports a total ("Pytanie nr N z T") and T has been passed.

Every question gets a persisted attempt status, so a restart skips stored
questions and retries only failed or unknown ones.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from enum import StrEnum

from cem_scraper.client import (
    AccessDenied,
    RequestFailed,
    ServerThrottled,
    ServerUnavailable,
    UnexpectedStatus,
)
from cem_scraper.config import Settings
from cem_scraper.database import Repository
from cem_scraper.metrics import Metrics
from cem_scraper.models import AttemptStatus, CollectionStatus, ExamSession, RunStatus
from cem_scraper.parser import (
    AuthorizationPageError,
    MalformedQuestion,
    NoQuestionContent,
    QuestionNumberMismatch,
    UnexpectedContent,
    parse_question_page,
)
from cem_scraper.session_manager import AccessBlocked, AuthorizedSession, SessionManager

log = logging.getLogger(__name__)

# Outcomes proving that a question exists at that number (content was served).
EXISTING_STATUSES = {
    AttemptStatus.STORED,
    AttemptStatus.PARSE_ERROR,
    AttemptStatus.NUMBER_MISMATCH,
    AttemptStatus.UNEXPECTED_CONTENT,
}

FAILURE_STATUSES = {
    AttemptStatus.PARSE_ERROR,
    AttemptStatus.NUMBER_MISMATCH,
    AttemptStatus.UNEXPECTED_CONTENT,
    AttemptStatus.AUTH_ERROR,
    AttemptStatus.SERVER_ERROR,
}


class SessionOutcome(StrEnum):
    COMPLETED = "completed"
    SKIPPED = "skipped"
    INCOMPLETE = "incomplete"  # finished, but some questions failed: rerun to retry them
    BLOCKED = "blocked"  # authorization unavailable/expired: stop the run
    ABORTED = "aborted"  # server signalled trouble: stop the run


@dataclass
class QuestionResult:
    number: int
    status: AttemptStatus
    error: str | None = None
    reported_total: int | None = None


@dataclass
class SessionReport:
    session_code: str
    outcome: SessionOutcome
    discovered_question_count: int = 0
    stored: int = 0
    missing: list[int] = field(default_factory=list)
    failed: dict[int, str] = field(default_factory=dict)
    error: str | None = None


@dataclass
class RunReport:
    run_id: str
    status: RunStatus
    sessions: list[SessionReport]
    metrics: dict[str, object]


class _StopRun(Exception):
    def __init__(self, outcome: SessionOutcome, message: str) -> None:
        super().__init__(message)
        self.outcome = outcome


class Orchestrator:
    def __init__(self, settings: Settings, repository: Repository, sessions: SessionManager, metrics: Metrics) -> None:
        self.settings = settings
        self.repo = repository
        self.sessions = sessions
        self.metrics = metrics

    async def run(self, session_codes: list[str], *, refetch: bool = False, only_failed: bool = False) -> RunReport:
        run_id = await self.repo.start_run(session_codes, self.sessions.provider.name)
        log.info("run %s started for %d session(s)", run_id, len(session_codes))
        reports: list[SessionReport] = []
        status = RunStatus.COMPLETED
        try:
            for code in session_codes:
                session = ExamSession.from_code(code, self.settings.specialization_code)
                report = await self.scrape_session(session, run_id, refetch=refetch, only_failed=only_failed)
                reports.append(report)
                log.info(
                    "%s: %s (stored=%d, missing=%d, failed=%d) | %s",
                    session.label,
                    report.outcome.value,
                    report.stored,
                    len(report.missing),
                    len(report.failed),
                    self.metrics.summary_line(),
                )
                if report.outcome is SessionOutcome.BLOCKED:
                    status = RunStatus.BLOCKED
                    break
                if report.outcome is SessionOutcome.ABORTED:
                    status = RunStatus.FAILED
                    break
                if report.outcome is SessionOutcome.INCOMPLETE:
                    status = RunStatus.PARTIAL
        except asyncio.CancelledError:
            status = RunStatus.INTERRUPTED
            log.warning("run interrupted; progress is saved, rerun to resume")
            raise
        except BaseException:
            status = RunStatus.FAILED
            raise
        finally:
            self.metrics.stop()
            snapshot = self.metrics.snapshot()
            snapshot["sessions"] = {
                r.session_code: {
                    "outcome": r.outcome.value,
                    "discovered": r.discovered_question_count,
                    "missing": r.missing,
                    "failed": {str(k): v for k, v in r.failed.items()},
                    "error": r.error,
                }
                for r in reports
            }
            await asyncio.shield(self.repo.finish_run(run_id, status, snapshot))
        return RunReport(run_id=str(run_id), status=status, sessions=reports, metrics=snapshot)

    async def scrape_session(
        self, session: ExamSession, run_id: object, *, refetch: bool = False, only_failed: bool = False
    ) -> SessionReport:
        state = await self.repo.ensure_session(session)
        report = SessionReport(session_code=session.session_code, outcome=SessionOutcome.COMPLETED)
        if state.collection_status is CollectionStatus.COMPLETED and not refetch and not only_failed:
            log.info("%s already completed; skipping (use --refetch to collect again)", session.label)
            report.outcome = SessionOutcome.SKIPPED
            report.discovered_question_count = state.discovered_question_count or 0
            statuses = await self.repo.attempt_statuses(state.id)
            report.stored = sum(s is AttemptStatus.STORED for s in statuses.values())
            return report

        statuses = {} if refetch else await self.repo.attempt_statuses(state.id)
        done = {n for n, s in statuses.items() if s is AttemptStatus.STORED}
        if only_failed:
            todo = sorted(n for n, s in statuses.items() if s in FAILURE_STATUSES)
            if not todo:
                log.info("%s: no failed questions to reprocess", session.label)
                report.outcome = SessionOutcome.SKIPPED
                return report

        await self.repo.set_session_status(state.id, CollectionStatus.IN_PROGRESS)
        try:
            auth = await self.sessions.open(session)
        except AccessBlocked as exc:
            log.error("%s: access blocked: %s", session.label, exc)
            await self.repo.set_session_status(state.id, CollectionStatus.BLOCKED, last_error=str(exc))
            report.outcome, report.error = SessionOutcome.BLOCKED, str(exc)
            return report

        try:
            if only_failed:
                auth = await self._process_numbers(auth, state.id, run_id, todo, report, done)
                highest = state.discovered_question_count or 0  # refined below from stored statuses
            else:
                auth, highest = await self._discover(auth, state.id, run_id, report, done)
        except _StopRun as stop:
            final = CollectionStatus.BLOCKED if stop.outcome is SessionOutcome.BLOCKED else CollectionStatus.FAILED
            await self.repo.set_session_status(state.id, final, last_error=str(stop))
            report.outcome, report.error = stop.outcome, str(stop)
            return report
        finally:
            await self.sessions.release(auth)

        all_statuses = await self.repo.attempt_statuses(state.id)
        if only_failed:
            highest = max([highest, *(n for n, s in all_statuses.items() if s in EXISTING_STATUSES)], default=0)
        report.stored = sum(s is AttemptStatus.STORED for s in all_statuses.values())
        report.missing = sorted(n for n, s in all_statuses.items() if s is AttemptStatus.MISSING and n < highest)
        report.discovered_question_count = highest
        failed = sorted(n for n, s in all_statuses.items() if s in FAILURE_STATUSES)
        if highest == 0 and not failed:
            message = "no questions found; verify the session code and specialization (verify-sessions)"
            log.warning("%s: %s", session.label, message)
            report.outcome, report.error = SessionOutcome.INCOMPLETE, message
            await self.repo.set_session_status(
                state.id, CollectionStatus.FAILED, discovered_question_count=0, last_error=message
            )
        elif failed:
            report.outcome = SessionOutcome.INCOMPLETE
            report.failed |= {n: report.failed.get(n, all_statuses[n].value) for n in failed}
            message = f"{len(failed)} question(s) failed: {failed[:20]}; rerun to retry"
            await self.repo.set_session_status(
                state.id, CollectionStatus.FAILED, discovered_question_count=highest, last_error=message
            )
        else:
            await self.repo.set_session_status(state.id, CollectionStatus.COMPLETED, discovered_question_count=highest)
        return report

    # ------------------------------------------------------------------ discovery

    async def _discover(
        self, auth: AuthorizedSession, session_id: int, run_id: object, report: SessionReport, done: set[int]
    ) -> tuple[AuthorizedSession, int]:
        settings = self.settings
        limit = settings.max_question_number
        highest = max(done, default=0)
        streak = 0
        number = 1

        # The page returned by the official form submission is question 1: use it, no extra request.
        if auth.grant.first_page_html and 1 not in done:
            result = await self._handle_html(auth, session_id, run_id, 1, auth.grant.first_page_html)
            auth = await self._after_batch(auth, session_id, run_id, [result], done)
            if result.status is AttemptStatus.STORED:
                done.add(1)

        while number <= limit:
            batch: list[int] = []
            while len(batch) < settings.concurrency and number <= limit:
                if number in done:
                    highest, streak = max(highest, number), 0
                else:
                    batch.append(number)
                number += 1
            if not batch:
                continue
            results = await self._fetch_batch(auth, session_id, run_id, batch)
            auth = await self._after_batch(auth, session_id, run_id, results, done)
            retry_from: int | None = None
            for result in sorted(results, key=lambda r: r.number):
                self._collect(report, result)
                if result.status is AttemptStatus.STORED:
                    done.add(result.number)
                    highest, streak = max(highest, result.number), 0
                    if result.reported_total:
                        limit = min(limit, result.reported_total + settings.missing_streak_to_stop)
                elif result.status is AttemptStatus.MISSING:
                    streak += 1
                elif result.status is AttemptStatus.AUTH_ERROR:
                    retry_from = result.number if retry_from is None else min(retry_from, result.number)
                elif result.status is not AttemptStatus.SERVER_ERROR:
                    # Content was there but unusable: the question exists.
                    highest, streak = max(highest, result.number), 0
            if retry_from is not None:  # re-authorized: fetch those numbers again
                number = retry_from
                continue
            if streak >= settings.missing_streak_to_stop and number - 1 > highest:
                log.info("%s: end of questions after %d (missing streak %d)", auth.session.label, highest, streak)
                break
        else:
            if limit == settings.max_question_number:
                log.warning("%s: reached max question number %d without finding the end", auth.session.label, limit)
        return auth, highest

    async def _process_numbers(
        self,
        auth: AuthorizedSession,
        session_id: int,
        run_id: object,
        numbers: list[int],
        report: SessionReport,
        done: set[int],
    ) -> AuthorizedSession:
        queue = list(numbers)
        while queue:
            batch, queue = queue[: self.settings.concurrency], queue[self.settings.concurrency :]
            results = await self._fetch_batch(auth, session_id, run_id, batch)
            auth = await self._after_batch(auth, session_id, run_id, results, done)
            for result in results:
                self._collect(report, result)
                if result.status is AttemptStatus.STORED:
                    done.add(result.number)
                elif result.status is AttemptStatus.AUTH_ERROR:
                    queue.insert(0, result.number)
        return auth

    @staticmethod
    def _collect(report: SessionReport, result: QuestionResult) -> None:
        if result.status in FAILURE_STATUSES and result.status is not AttemptStatus.AUTH_ERROR:
            report.failed[result.number] = f"{result.status.value}: {result.error}"
        elif result.status is AttemptStatus.STORED:
            report.failed.pop(result.number, None)

    async def _fetch_batch(
        self, auth: AuthorizedSession, session_id: int, run_id: object, numbers: list[int]
    ) -> list[QuestionResult]:
        try:
            async with asyncio.TaskGroup() as group:
                tasks = [group.create_task(self._fetch_one(auth, session_id, run_id, n)) for n in numbers]
        except BaseExceptionGroup as group_error:
            stops = [e for e in group_error.exceptions if isinstance(e, _StopRun)]
            if stops:
                raise stops[0] from None
            raise
        return [task.result() for task in tasks]

    async def _fetch_one(self, auth: AuthorizedSession, session_id: int, run_id: object, number: int) -> QuestionResult:
        code = auth.session.session_code
        try:
            page = await auth.client.fetch_question(auth.session, number)
        except AccessDenied as exc:
            result = QuestionResult(number, AttemptStatus.AUTH_ERROR, str(exc))
            return await self._record(session_id, run_id, code, result)
        except (ServerUnavailable, ServerThrottled) as exc:
            await self.repo.record_attempt(session_id, number, AttemptStatus.SERVER_ERROR, str(exc), run_id)  # type: ignore[arg-type]
            raise _StopRun(SessionOutcome.ABORTED, str(exc)) from exc
        except (RequestFailed, UnexpectedStatus) as exc:
            result = QuestionResult(number, AttemptStatus.SERVER_ERROR, str(exc))
            return await self._record(session_id, run_id, code, result)
        return await self._handle_html(auth, session_id, run_id, number, page.html, page.url)

    async def _handle_html(
        self,
        auth: AuthorizedSession,
        session_id: int,
        run_id: object,
        number: int,
        html: str,
        url: str | None = None,
    ) -> QuestionResult:
        code = auth.session.session_code
        try:
            parsed = parse_question_page(html, expected_number=number)
            question = parsed.to_scraped(auth.session, number, source_url=url)
        except AuthorizationPageError as exc:
            result = QuestionResult(number, AttemptStatus.AUTH_ERROR, str(exc))
        except NoQuestionContent as exc:
            result = QuestionResult(number, AttemptStatus.MISSING, str(exc))
        except QuestionNumberMismatch as exc:
            result = QuestionResult(number, AttemptStatus.NUMBER_MISMATCH, str(exc))
        except UnexpectedContent as exc:
            result = QuestionResult(number, AttemptStatus.UNEXPECTED_CONTENT, str(exc))
        except MalformedQuestion as exc:
            result = QuestionResult(number, AttemptStatus.PARSE_ERROR, str(exc)[:500])
        else:
            await self.repo.upsert_question(session_id, question, run_id)  # type: ignore[arg-type]
            if question.validation_issues:
                log.info("%s q%d needs review: %s", code, number, "; ".join(question.validation_issues))
            self.metrics.record_question(code, AttemptStatus.STORED.value, success=True)
            return QuestionResult(number, AttemptStatus.STORED, reported_total=parsed.reported_total)
        return await self._record(session_id, run_id, code, result)

    async def _record(self, session_id: int, run_id: object, code: str, result: QuestionResult) -> QuestionResult:
        await self.repo.record_attempt(session_id, result.number, result.status, result.error, run_id)  # type: ignore[arg-type]
        self.metrics.record_question(code, result.status.value, success=False)
        if result.status is not AttemptStatus.MISSING:
            log.warning("%s q%d: %s (%s)", code, result.number, result.status.value, result.error)
        return result

    async def _after_batch(
        self,
        auth: AuthorizedSession,
        session_id: int,
        run_id: object,
        results: list[QuestionResult],
        done: set[int],
    ) -> AuthorizedSession:
        """Distinguish 'out-of-range question' from 'authorization expired' and act on it.

        If a request yields the entry form / HTTP 401-403, re-request a question we
        know exists. If that still works, the session is fine and the number simply
        does not exist (recorded as missing). Otherwise authorization is gone:
        re-authorize within limits or stop.
        """
        auth_failures = [r for r in results if r.status is AttemptStatus.AUTH_ERROR]
        if not auth_failures:
            return auth
        if done and await self._probe(auth, min(done)):
            for r in auth_failures:
                r.status, r.error = AttemptStatus.MISSING, "server returned the entry form; session still valid"
                await self.repo.record_attempt(session_id, r.number, r.status, r.error, run_id)  # type: ignore[arg-type]
                self.metrics.reclassify_question(
                    auth.session.session_code, AttemptStatus.AUTH_ERROR.value, AttemptStatus.MISSING.value
                )
            return auth
        try:
            return await self.sessions.reauthorize(auth)
        except AccessBlocked as exc:
            raise _StopRun(SessionOutcome.BLOCKED, str(exc)) from exc

    async def _probe(self, auth: AuthorizedSession, known_number: int) -> bool:
        try:
            page = await auth.client.fetch_question(auth.session, known_number)
            parse_question_page(page.html, expected_number=known_number)
        except Exception as exc:  # any failure means we cannot confirm the session
            log.info("probe of q%d failed: %s", known_number, exc)
            return False
        return True
