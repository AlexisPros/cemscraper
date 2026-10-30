"""Collection orchestrator: session pipeline, question discovery, resume, failure handling.

Sessions: up to ``pipeline_depth`` examination sessions per lane (exit IP) are
open at once. Authorizations are serialized per lane by the ``SessionManager``,
so while session N is being collected the next session is already being
authorized (its CAPTCHA is solved meanwhile) and its grant is used as soon as it
exists. Sessions of one lane share its ``TrafficControl``: parallel sessions
never raise one IP's request rate.

Questions: numbers are dispatched from 1 upwards through a sliding window of
in-flight requests (the adaptive concurrency limit), so one slow response does
not hold back the others. Outcomes are folded in question order: collection of
a session ends after ``missing_streak_to_stop`` consecutive *missing* questions
past the highest question found (gaps inside the range are recorded as missing
and do not stop collection), or when the page reports a total ("Pytanie nr N z T")
and T has been passed.

Every question gets a persisted attempt status, so a restart skips stored
questions and retries only failed or unknown ones.
"""

from __future__ import annotations

import asyncio
import heapq
import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from cem_scraper.client import (
    AccessDenied,
    LaneFailed,
    RequestFailed,
    ServerThrottled,
    ServerUnavailable,
    UnexpectedStatus,
)
from cem_scraper.config import Settings
from cem_scraper.database import Repository, SessionState
from cem_scraper.metrics import Metrics
from cem_scraper.models import AttemptStatus, CollectionStatus, ExamSession, RunStatus
from cem_scraper.parser import (
    AuthorizationPageError,
    MalformedQuestion,
    NoQuestionContent,
    PageKind,
    QuestionNumberMismatch,
    UnexpectedContent,
    classify_page,
    decode_html,
    make_soup,
    parse_question_page,
)
from cem_scraper.session_manager import AccessBlocked, AuthorizedSession, OpenCancelled, SessionManager

log = logging.getLogger(__name__)

# Outcomes proving that a question exists at that number (content was served).
EXISTING_STATUSES = {
    AttemptStatus.STORED,
    AttemptStatus.PARSE_ERROR,
    AttemptStatus.NUMBER_MISMATCH,
    AttemptStatus.UNEXPECTED_CONTENT,
}

# The same HTML always fails the same way: downloading it again (and paying for a CAPTCHA
# to do so) cannot help. These are fixed offline with a parser change + `reparse`.
OFFLINE_FIXABLE = {AttemptStatus.PARSE_ERROR}


class ProbeResult(StrEnum):
    VALID = "valid"  # a known question still loads: the session works
    EXPIRED = "expired"  # the site answered with the EULA / entry form or 401/403
    UNKNOWN = "unknown"  # the probe itself failed (timeout, 5xx): no conclusion


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
    NOT_STARTED = "not_started"  # the run stopped before this session was authorized


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
    def __init__(
        self,
        settings: Settings,
        repository: Repository,
        sessions: SessionManager,
        metrics: Metrics,
        *,
        on_session_done: Callable[[ExamSession, SessionReport, int, int], Awaitable[None]] | None = None,
    ) -> None:
        self.settings = settings
        self.repo = repository
        self.sessions = sessions
        self.metrics = metrics
        # Called after each examination session that was actually worked on (e.g. a notification);
        # arguments: session, report, sessions finished so far, sessions in the run.
        self.on_session_done = on_session_done
        self._finished = 0
        self._total = 0

    async def run(self, session_codes: list[str], *, refetch: bool = False, only_failed: bool = False) -> RunReport:
        run_id = await self.repo.start_run(session_codes, self.sessions.provider.name)
        lanes = self.sessions.lanes
        log.info(
            "run %s started for %d session(s), up to %d at a time over %d lane(s): %s",
            run_id,
            len(session_codes),
            self.settings.pipeline_depth * len(lanes),
            len(lanes),
            ", ".join(lane.name for lane in lanes),
        )
        tasks: dict[str, asyncio.Task[SessionReport]] = {}
        reports: dict[str, SessionReport] = {}
        self._finished, self._total = 0, len(session_codes)
        slots = asyncio.Semaphore(self.settings.pipeline_depth * len(lanes))
        stop = asyncio.Event()
        status = RunStatus.COMPLETED
        try:
            for code in session_codes:
                await slots.acquire()
                if stop.is_set():
                    slots.release()
                    break
                session = ExamSession.from_code(code, self.settings.specialization_code)
                tasks[code] = asyncio.create_task(
                    self._session_worker(session, run_id, slots, stop, tasks, refetch=refetch, only_failed=only_failed),
                    name=f"session-{code}",
                )
            results = await asyncio.gather(*tasks.values(), return_exceptions=True)
            for code, result in zip(tasks, results, strict=True):
                if isinstance(result, SessionReport):
                    reports[code] = result
                elif isinstance(result, asyncio.CancelledError):
                    message = "stopped because another session hit a server failure; rerun to resume"
                    reports[code] = SessionReport(code, SessionOutcome.ABORTED, error=message)
                else:
                    raise result
            status = self._run_status(reports.values())
        except asyncio.CancelledError:
            status = RunStatus.INTERRUPTED
            log.warning("run interrupted; progress is saved, rerun to resume")
            raise
        except BaseException:
            status = RunStatus.FAILED
            raise
        finally:
            for task in tasks.values():
                task.cancel()
            await asyncio.gather(*tasks.values(), return_exceptions=True)
            for code, task in tasks.items():
                if code not in reports and not task.cancelled() and task.exception() is None:
                    reports[code] = task.result()  # finished before an interruption
            self.metrics.stop()
            snapshot = self.metrics.snapshot()
            snapshot["sessions"] = {
                code: {
                    "outcome": r.outcome.value,
                    "discovered": r.discovered_question_count,
                    "missing": r.missing,
                    "failed": {str(k): v for k, v in r.failed.items()},
                    "error": r.error,
                }
                for code, r in reports.items()
            }
            await asyncio.shield(self.repo.finish_run(run_id, status, snapshot))
        ordered = [reports[code] for code in session_codes if code in reports]
        return RunReport(run_id=str(run_id), status=status, sessions=ordered, metrics=snapshot)

    async def _session_worker(
        self,
        session: ExamSession,
        run_id: object,
        slots: asyncio.Semaphore,
        stop: asyncio.Event,
        siblings: dict[str, asyncio.Task[SessionReport]],
        *,
        refetch: bool,
        only_failed: bool,
    ) -> SessionReport:
        try:
            report = await self.scrape_session(
                session, run_id, refetch=refetch, only_failed=only_failed, abort_if=stop.is_set
            )
        except BaseException:
            stop.set()
            raise
        finally:
            slots.release()
        log.info(
            "%s: %s (stored=%d, missing=%d, failed=%d) | %s",
            session.label,
            report.outcome.value,
            report.stored,
            len(report.missing),
            len(report.failed),
            self.metrics.summary_line(),
        )
        self._finished += 1
        if self.on_session_done is not None and report.outcome not in (
            SessionOutcome.SKIPPED,
            SessionOutcome.NOT_STARTED,
        ):
            try:
                await self.on_session_done(session, report, self._finished, self._total)
            except Exception:  # a notification must never break collection
                log.exception("session callback failed")
        if report.outcome in (SessionOutcome.BLOCKED, SessionOutcome.ABORTED):
            stop.set()  # launch no further sessions; authorizations not yet started are skipped
        if report.outcome is SessionOutcome.ABORTED:
            # The server is in trouble: stop every session now instead of letting them finish.
            current = asyncio.current_task()
            for task in siblings.values():
                if task is not current:
                    task.cancel()
        return report

    @staticmethod
    def _run_status(reports: Iterable[SessionReport]) -> RunStatus:
        outcomes = {r.outcome for r in reports}
        if SessionOutcome.ABORTED in outcomes:
            return RunStatus.FAILED
        if SessionOutcome.BLOCKED in outcomes:
            return RunStatus.BLOCKED
        if SessionOutcome.INCOMPLETE in outcomes:
            return RunStatus.PARTIAL
        return RunStatus.COMPLETED

    async def scrape_session(
        self,
        session: ExamSession,
        run_id: object,
        *,
        refetch: bool = False,
        only_failed: bool = False,
        abort_if: Callable[[], bool] | None = None,
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
        offline = sorted(n for n, s in statuses.items() if s in OFFLINE_FIXABLE)
        networked = sorted(n for n, s in statuses.items() if s in FAILURE_STATUSES - OFFLINE_FIXABLE)
        if only_failed:
            todo = networked + [n for n in offline if not self._has_snapshot(session, n)]
            if not todo:
                return self._skip_without_network(session, state, statuses, report, offline)
        elif state.collection_status is CollectionStatus.FAILED and not refetch and not networked:
            # Discovery already finished; nothing a new download (and a new CAPTCHA) could fix.
            return self._skip_without_network(session, state, statuses, report, offline)

        try:
            auth = await self.sessions.open(session, abort_if=abort_if)
        except OpenCancelled as exc:
            report.outcome, report.error = SessionOutcome.NOT_STARTED, str(exc)
            return report
        except AccessBlocked as exc:
            log.error("%s: access blocked: %s", session.label, exc)
            await self.repo.set_session_status(state.id, CollectionStatus.BLOCKED, last_error=str(exc))
            report.outcome, report.error = SessionOutcome.BLOCKED, str(exc)
            return report
        except LaneFailed as exc:  # the lane is already disabled by the session manager
            return await self._fail_over(session, run_id, exc, only_failed=only_failed, abort_if=abort_if)
        await self.repo.set_session_status(
            state.id, CollectionStatus.IN_PROGRESS, expected_question_count=auth.grant.question_limit
        )

        try:
            candidates = todo if only_failed else None
            auth, highest = await self._collect(auth, state.id, run_id, report, done, candidates=candidates)
            if only_failed:
                highest = state.discovered_question_count or 0  # refined below from stored statuses
        except _StopRun as stop:
            final = CollectionStatus.BLOCKED if stop.outcome is SessionOutcome.BLOCKED else CollectionStatus.FAILED
            await self.repo.set_session_status(state.id, final, last_error=str(stop))
            report.outcome, report.error = stop.outcome, str(stop)
            return report
        except LaneFailed as exc:
            self.sessions.disable(auth.lane, str(exc))
            lane_failure: LaneFailed | None = exc
        else:
            lane_failure = None
        finally:
            await self.sessions.release(auth)
        if lane_failure is not None:
            return await self._fail_over(session, run_id, lane_failure, only_failed=only_failed, abort_if=abort_if)

        return await self._finalize(state.id, session, report, highest, refine=only_failed)

    async def _fail_over(
        self,
        session: ExamSession,
        run_id: object,
        failure: LaneFailed,
        *,
        only_failed: bool,
        abort_if: Callable[[], bool] | None,
    ) -> SessionReport:
        """Continue a session on another lane after its proxy failed.

        Stored questions are kept, so only the rest is fetched (one new CAPTCHA).
        Each failure disables a lane, so this ends: with no lane left, ``open``
        reports the session as blocked.
        """
        log.warning("%s: %s; continuing on another lane", session.label, failure)
        return await self.scrape_session(session, run_id, refetch=False, only_failed=only_failed, abort_if=abort_if)

    def _has_snapshot(self, session: ExamSession, number: int) -> bool:
        directory = self.settings.save_html_dir
        return directory is not None and (directory / f"{session.session_code}_{number}.html").exists()

    @staticmethod
    def _skip_without_network(
        session: ExamSession,
        state: SessionState,
        statuses: dict[int, AttemptStatus],
        report: SessionReport,
        offline: list[int],
    ) -> SessionReport:
        report.stored = sum(s is AttemptStatus.STORED for s in statuses.values())
        report.discovered_question_count = state.discovered_question_count or 0
        if offline:
            report.outcome = SessionOutcome.INCOMPLETE
            report.failed = {n: statuses[n].value for n in offline}
            report.error = (
                f"{len(offline)} question(s) failed to parse {offline[:10]}; downloading them again would return "
                "the same page: fix the parser, then run `cem-scraper reparse` (no CAPTCHA, no network)"
            )
        elif not report.discovered_question_count and state.collection_status is CollectionStatus.FAILED:
            report.outcome = SessionOutcome.INCOMPLETE
            report.error = "no questions were found in an earlier run; use --refetch to try this session again"
        else:
            report.outcome = SessionOutcome.SKIPPED
        log.info("%s: not opened (no CAPTCHA needed): %s", session.label, report.error or "nothing to do")
        return report

    async def _finalize(
        self, session_id: int, session: ExamSession, report: SessionReport, highest: int, *, refine: bool
    ) -> SessionReport:
        """Derive the session's collection status from the persisted per-question statuses."""
        all_statuses = await self.repo.attempt_statuses(session_id)
        if refine:  # partial pass (retry/reparse): the questions seen so far define the range
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
                session_id, CollectionStatus.FAILED, discovered_question_count=0, last_error=message
            )
        elif failed:
            report.outcome = SessionOutcome.INCOMPLETE
            report.failed |= {n: report.failed.get(n, all_statuses[n].value) for n in failed}
            message = f"{len(failed)} question(s) failed: {failed[:20]}; rerun to retry"
            await self.repo.set_session_status(
                session_id, CollectionStatus.FAILED, discovered_question_count=highest, last_error=message
            )
        else:
            await self.repo.set_session_status(
                session_id, CollectionStatus.COMPLETED, discovered_question_count=highest
            )
        return report

    # ------------------------------------------------------------------ offline reprocessing

    async def reparse(self, session_codes: list[str], directory: Path, *, everything: bool = False) -> RunReport:
        """Re-process saved HTML snapshots (``CEM_SAVE_HTML_DIR``) without contacting CEM.

        By default only questions that failed on their *content* (parse error, number
        mismatch, unexpected content) are reprocessed, e.g. after a parser fix. With
        ``everything`` every question that had content is parsed again. Network
        failures (auth/server errors) need ``scrape --retry-failed``.
        """
        run_id = await self.repo.start_run(session_codes, "snapshots")
        reports: list[SessionReport] = []
        status = RunStatus.FAILED
        content_failures = EXISTING_STATUSES - {AttemptStatus.STORED}
        try:
            for code in session_codes:
                session = ExamSession.from_code(code, self.settings.specialization_code)
                state = await self.repo.ensure_session(session)
                statuses = await self.repo.attempt_statuses(state.id)
                if not statuses:
                    continue  # never collected: nothing saved to reparse
                wanted = EXISTING_STATUSES if everything else content_failures
                numbers = sorted(n for n, s in statuses.items() if s in wanted)
                report = SessionReport(session_code=code, outcome=SessionOutcome.COMPLETED)
                for number in numbers:
                    path = directory / f"{code}_{number}.html"
                    if not path.exists():
                        log.warning("%s q%d: no snapshot at %s", code, number, path)
                        continue
                    html = decode_html(path.read_bytes())
                    if classify_page(make_soup(html)) is not PageKind.QUESTION:
                        log.warning("%s q%d: snapshot is not a question page; skipped", code, number)
                        continue
                    self._note_result(report, await self._handle_html(session, state.id, run_id, number, html))
                reports.append(
                    await self._finalize(state.id, session, report, state.discovered_question_count or 0, refine=True)
                )
                log.info("%s: reparsed %d snapshot(s) -> %s", session.label, len(numbers), reports[-1].outcome.value)
            status = self._run_status(reports)
        finally:
            self.metrics.stop()
            snapshot = self.metrics.snapshot()
            await asyncio.shield(self.repo.finish_run(run_id, status, snapshot))
        return RunReport(run_id=str(run_id), status=status, sessions=reports, metrics=snapshot)

    # ------------------------------------------------------------------ question engine

    async def _collect(
        self,
        auth: AuthorizedSession,
        session_id: int,
        run_id: object,
        report: SessionReport,
        done: set[int],
        *,
        candidates: list[int] | None = None,
    ) -> tuple[AuthorizedSession, int]:
        """Fetch questions through a sliding window of in-flight requests.

        ``candidates=None`` means discovery (1, 2, ... until the end is detected);
        otherwise exactly those numbers are fetched (``--retry-failed``).
        """
        settings = self.settings
        discovery = candidates is None
        pending = iter(candidates or [])
        limit = settings.max_question_number
        form_limit = auth.grant.question_limit
        if discovery and form_limit:
            # The official form lists question numbers 1..N: nothing exists beyond N, so there is
            # no need to probe past it (which would only return the EULA page).
            limit = min(limit, form_limit)
        highest = max(done, default=0)
        streak = 0
        frontier = 1  # discovery: next number whose outcome is folded into the streak
        next_number = 1
        stopped = False
        outcomes: dict[int, AttemptStatus] = {}
        retry: list[int] = []  # heap: numbers to fetch again after re-authorization
        in_flight: dict[asyncio.Task[QuestionResult], int] = {}
        auth_failures: list[QuestionResult] = []

        def take_number() -> int | None:
            nonlocal next_number
            if retry:
                return heapq.heappop(retry)
            if not discovery:
                return next(pending, None)
            if stopped:
                return None
            while next_number <= limit and next_number in done:
                next_number += 1
            if next_number > limit:
                return None
            next_number += 1
            return next_number - 1

        def fold() -> None:
            nonlocal frontier, highest, streak, stopped
            while not stopped:
                if frontier in done:
                    status = AttemptStatus.STORED
                elif frontier in outcomes:
                    status = outcomes[frontier]
                else:
                    return  # still in flight (or waiting for a retry)
                if status in EXISTING_STATUSES:
                    highest, streak = max(highest, frontier), 0
                elif status is AttemptStatus.MISSING:
                    streak += 1
                # SERVER_ERROR: unknown whether the question exists; the streak is unchanged.
                frontier += 1
                if streak >= settings.missing_streak_to_stop and frontier - 1 > highest:
                    stopped = True
                    log.info("%s: end of questions after %d (missing streak %d)", auth.session.label, highest, streak)

        def absorb(result: QuestionResult) -> None:
            nonlocal limit
            self._note_result(report, result)
            if result.status is AttemptStatus.AUTH_ERROR:
                auth_failures.append(result)
                return
            outcomes[result.number] = result.status
            if result.status is AttemptStatus.STORED:
                done.add(result.number)
                if result.reported_total:
                    limit = min(limit, result.reported_total + settings.missing_streak_to_stop)

        # The page returned by the official form submission is question 1: use it, no extra request.
        if auth.grant.first_page_html and 1 not in done and discovery:
            absorb(await self._handle_html(auth.session, session_id, run_id, 1, auth.grant.first_page_html))

        try:
            while True:
                if auth_failures and not in_flight:
                    auth = await self._resolve_auth_failures(auth, session_id, run_id, auth_failures, done)
                    for failure in auth_failures:
                        if failure.status in (AttemptStatus.MISSING, AttemptStatus.SERVER_ERROR):
                            outcomes[failure.number] = failure.status
                            self._note_result(report, failure)
                        else:  # re-authorized: fetch it again with the new grant
                            heapq.heappush(retry, failure.number)
                    auth_failures.clear()
                if discovery:
                    fold()
                # Dispatch nothing while authorization is in doubt; otherwise fill the window.
                while not auth_failures and len(in_flight) < auth.client.traffic.concurrency.limit:
                    number = take_number()
                    if number is None:
                        break
                    task = asyncio.create_task(self._fetch_one(auth, session_id, run_id, number))
                    in_flight[task] = number
                if not in_flight and not auth_failures:
                    break
                if in_flight:
                    finished, _ = await asyncio.wait(in_flight, return_when=asyncio.FIRST_COMPLETED)
                    for task in finished:
                        del in_flight[task]
                        absorb(task.result())  # re-raises _StopRun and unexpected errors
        finally:
            for task in in_flight:
                task.cancel()
            if in_flight:
                await asyncio.gather(*in_flight, return_exceptions=True)

        if discovery and not stopped and form_limit and limit == form_limit:
            log.info("%s: collected up to the form's last question number %d", auth.session.label, limit)
        elif discovery and not stopped and limit == settings.max_question_number:
            log.warning("%s: reached max question number %d without finding the end", auth.session.label, limit)
        return auth, highest

    @staticmethod
    def _note_result(report: SessionReport, result: QuestionResult) -> None:
        if result.status in FAILURE_STATUSES and result.status is not AttemptStatus.AUTH_ERROR:
            report.failed[result.number] = f"{result.status.value}: {result.error}"
        elif result.status is AttemptStatus.STORED:
            report.failed.pop(result.number, None)

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
        return await self._handle_html(auth.session, session_id, run_id, number, page.html, page.url)

    async def _handle_html(
        self,
        session: ExamSession,
        session_id: int,
        run_id: object,
        number: int,
        html: str,
        url: str | None = None,
    ) -> QuestionResult:
        code = session.session_code
        try:
            parsed = parse_question_page(html, expected_number=number)
            question = parsed.to_scraped(session, number, source_url=url)
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

    async def _resolve_auth_failures(
        self,
        auth: AuthorizedSession,
        session_id: int,
        run_id: object,
        auth_failures: list[QuestionResult],
        done: set[int],
    ) -> AuthorizedSession:
        """Distinguish 'out-of-range question' from 'authorization expired' and act on it.

        If a request yields the entry form / HTTP 401-403, re-request a question we
        know exists. If that still works, the session is fine and the number simply
        does not exist (recorded as missing). Otherwise authorization is gone:
        re-authorize within limits or stop.
        """
        probe = await self._probe(auth, min(done)) if done else ProbeResult.EXPIRED
        if probe is not ProbeResult.EXPIRED:
            # VALID: the numbers simply do not exist. UNKNOWN: the probe itself failed, so we
            # cannot tell; never pay for a re-authorization on a guess: retry them later.
            status, error = (
                (AttemptStatus.MISSING, "server returned the entry form; session still valid")
                if probe is ProbeResult.VALID
                else (AttemptStatus.SERVER_ERROR, "session could not be confirmed (probe failed); rerun to retry")
            )
            for r in auth_failures:
                r.status, r.error = status, error
                await self.repo.record_attempt(session_id, r.number, r.status, r.error, run_id)  # type: ignore[arg-type]
                self.metrics.reclassify_question(
                    auth.session.session_code, AttemptStatus.AUTH_ERROR.value, status.value
                )
            if probe is ProbeResult.UNKNOWN:
                log.warning("%s: could not confirm the session; not re-authorizing on a guess", auth.session.label)
            return auth
        try:
            return await self.sessions.reauthorize(auth)
        except AccessBlocked as exc:
            raise _StopRun(SessionOutcome.BLOCKED, str(exc)) from exc

    async def _probe(self, auth: AuthorizedSession, known_number: int) -> ProbeResult:
        """Re-request a question known to exist and classify the answer (not a full parse)."""
        try:
            page = await auth.client.fetch_question(auth.session, known_number)
        except AccessDenied:
            return ProbeResult.EXPIRED
        except (ServerUnavailable, ServerThrottled) as exc:
            raise _StopRun(SessionOutcome.ABORTED, str(exc)) from exc
        except (RequestFailed, UnexpectedStatus) as exc:
            log.info("probe of q%d failed: %s", known_number, exc)
            return ProbeResult.UNKNOWN
        kind = classify_page(make_soup(page.html))
        if kind is PageKind.QUESTION:
            return ProbeResult.VALID
        if kind in (PageKind.EULA, PageKind.ENTRY_FORM):
            return ProbeResult.EXPIRED
        return ProbeResult.UNKNOWN
