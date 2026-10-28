"""Persistence: a small repository interface with PostgreSQL and in-memory backends.

The in-memory backend powers ``--dry-run`` and orchestrator tests; the
PostgreSQL backend is the real store (JSONB columns, UPSERTs, migrations).
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import psycopg
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from cem_scraper.models import (
    AttemptStatus,
    CollectionStatus,
    ExamSession,
    RunStatus,
    ScrapedQuestion,
)

log = logging.getLogger(__name__)


class DatabaseUnavailable(Exception):
    """PostgreSQL cannot be reached with the configured DSN."""


def migrations_dir() -> Path:
    packaged = Path(__file__).parent / "migrations"
    if packaged.is_dir():
        return packaged
    return Path(__file__).resolve().parents[2] / "migrations"


def content_hash(question: ScrapedQuestion) -> str:
    record = question.to_record()
    payload = {k: record[k] for k in ("question_data", "correct_answer", "answer_statistics")}
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass
class SessionState:
    id: int
    session_code: str
    collection_status: CollectionStatus
    discovered_question_count: int | None
    expected_question_count: int | None


@dataclass
class SessionSummary:
    session_code: str
    year: int
    season: str
    collection_status: str
    discovered_question_count: int | None
    stored: int
    needs_review: int
    failed: int
    missing: int
    last_error: str | None


class Repository(Protocol):
    async def ensure_session(self, session: ExamSession) -> SessionState: ...
    async def set_session_status(
        self,
        session_id: int,
        status: CollectionStatus,
        *,
        discovered_question_count: int | None = None,
        expected_question_count: int | None = None,
        last_error: str | None = None,
    ) -> None: ...
    async def attempt_statuses(self, session_id: int) -> dict[int, AttemptStatus]: ...
    async def upsert_question(self, session_id: int, question: ScrapedQuestion, run_id: uuid.UUID) -> bool: ...
    async def record_attempt(
        self, session_id: int, number: int, status: AttemptStatus, error: str | None, run_id: uuid.UUID
    ) -> None: ...
    async def start_run(self, targets: list[str], access_provider: str) -> uuid.UUID: ...
    async def finish_run(self, run_id: uuid.UUID, status: RunStatus, metrics: dict[str, Any]) -> None: ...
    async def fetch_records(self, session_codes: list[str] | None = None) -> list[dict[str, Any]]: ...
    async def summaries(self) -> list[SessionSummary]: ...
    async def close(self) -> None: ...


def _record_from_parts(meta: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    return {
        "exam": meta["exam_type"],
        "specialization": meta["specialization"],
        "year": meta["year"],
        "season": meta["season"],
        "session_id": meta["session_code"],
        "question_number": row["question_number"],
        "question_data": row["question_data"],
        "correct_answer": row["correct_answer"],
        "answer_statistics": row["answer_statistics"],
        "validation_status": row["validation_status"],
        "validation_issues": row["validation_issues"],
    }


# --------------------------------------------------------------------------- PostgreSQL


class PostgresRepository:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    @classmethod
    async def connect(cls, dsn: str, *, min_size: int = 1, max_size: int = 4) -> PostgresRepository:
        try:  # fail fast with a clear message instead of the pool's background retry loop
            probe = await psycopg.AsyncConnection.connect(dsn, connect_timeout=5)
            await probe.close()
        except psycopg.OperationalError as exc:
            info = conninfo_to_dict(dsn)
            where = f"{info.get('host', 'localhost')}:{info.get('port', 5432)}/{info.get('dbname', '')}"
            raise DatabaseUnavailable(
                f"cannot connect to PostgreSQL at {where}: {str(exc).splitlines()[0]}. "
                "Start it with `docker compose up -d` or set CEM_DATABASE_URL."
            ) from exc
        pool = AsyncConnectionPool(dsn, min_size=min_size, max_size=max_size, open=False, kwargs={"autocommit": True})
        await pool.open(wait=True, timeout=15)
        return cls(pool)

    async def close(self) -> None:
        await self._pool.close()

    async def migrate(self) -> list[str]:
        """Apply pending ``migrations/*.sql`` files in order (each in its own transaction)."""
        applied: list[str] = []
        async with self._pool.connection() as conn:
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                " version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
            )
            # Serialise concurrent migrators.
            async with conn.transaction():
                await conn.execute("SELECT pg_advisory_xact_lock(727251)")
                cur = await conn.execute("SELECT version FROM schema_migrations")
                done = {row[0] for row in await cur.fetchall()}
                for path in sorted(migrations_dir().glob("*.sql")):
                    if path.stem in done:
                        continue
                    log.info("applying migration %s", path.name)
                    await conn.execute(path.read_text(encoding="utf-8"))  # type: ignore[arg-type]
                    await conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (path.stem,))
                    applied.append(path.stem)
        return applied

    async def ensure_session(self, session: ExamSession) -> SessionState:
        async with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                INSERT INTO exam_sessions (exam_type, specialization, specialization_code, year, season, session_code)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT ON CONSTRAINT exam_sessions_identity DO UPDATE SET updated_at = exam_sessions.updated_at
                RETURNING id, session_code, collection_status, discovered_question_count, expected_question_count
                """,
                (
                    session.exam_type,
                    session.specialization,
                    session.specialization_code,
                    session.year,
                    session.season.value,
                    session.session_code,
                ),
            )
            row = await cur.fetchone()
        assert row is not None
        return SessionState(
            id=row["id"],
            session_code=row["session_code"],
            collection_status=CollectionStatus(row["collection_status"]),
            discovered_question_count=row["discovered_question_count"],
            expected_question_count=row["expected_question_count"],
        )

    async def set_session_status(
        self,
        session_id: int,
        status: CollectionStatus,
        *,
        discovered_question_count: int | None = None,
        expected_question_count: int | None = None,
        last_error: str | None = None,
    ) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                """
                UPDATE exam_sessions SET
                    collection_status = %s,
                    discovered_question_count = COALESCE(%s, discovered_question_count),
                    expected_question_count = COALESCE(%s, expected_question_count),
                    last_error = %s,
                    updated_at = now()
                WHERE id = %s
                """,
                (status.value, discovered_question_count, expected_question_count, last_error, session_id),
            )

    async def attempt_statuses(self, session_id: int) -> dict[int, AttemptStatus]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT question_number, status FROM question_attempts WHERE exam_session_id = %s", (session_id,)
            )
            statuses = {number: AttemptStatus(status) for number, status in await cur.fetchall()}
            # Stored questions are authoritative even if the attempt row is missing.
            cur = await conn.execute("SELECT question_number FROM questions WHERE exam_session_id = %s", (session_id,))
            for (number,) in await cur.fetchall():
                statuses[number] = AttemptStatus.STORED
        return statuses

    async def upsert_question(self, session_id: int, question: ScrapedQuestion, run_id: uuid.UUID) -> bool:
        """Insert or update one question. Returns True if a row was inserted or its content changed."""
        record = question.to_record()
        digest = content_hash(question)
        async with self._pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                """
                INSERT INTO questions (
                    exam_session_id, question_number, question_data, correct_answer, answer_statistics,
                    extra_statistics, validation_status, validation_issues, source_url, content_hash,
                    fetched_at, last_run_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now(), %s)
                ON CONFLICT ON CONSTRAINT questions_session_number DO UPDATE SET
                    question_data = EXCLUDED.question_data,
                    correct_answer = EXCLUDED.correct_answer,
                    answer_statistics = EXCLUDED.answer_statistics,
                    extra_statistics = EXCLUDED.extra_statistics,
                    validation_status = EXCLUDED.validation_status,
                    validation_issues = EXCLUDED.validation_issues,
                    source_url = EXCLUDED.source_url,
                    fetched_at = EXCLUDED.fetched_at,
                    last_run_id = EXCLUDED.last_run_id,
                    updated_at = CASE WHEN questions.content_hash IS DISTINCT FROM EXCLUDED.content_hash
                                      THEN now() ELSE questions.updated_at END,
                    content_hash = EXCLUDED.content_hash
                RETURNING (xmax = 0) AS inserted,
                          (SELECT content_hash FROM questions q
                            WHERE q.exam_session_id = %s AND q.question_number = %s) AS previous_hash
                """,
                (
                    session_id,
                    question.question_number,
                    Jsonb(record["question_data"]),
                    Jsonb(record["correct_answer"]) if record["correct_answer"] is not None else None,
                    Jsonb(record["answer_statistics"]) if record["answer_statistics"] is not None else None,
                    Jsonb(question.extra_statistics) if question.extra_statistics else None,
                    question.validation_status.value,
                    Jsonb(question.validation_issues),
                    question.source_url,
                    digest,
                    run_id,
                    session_id,
                    question.question_number,
                ),
            )
            row = await cur.fetchone()
            await conn.execute(
                """
                INSERT INTO question_attempts (exam_session_id, question_number, status, last_error, last_run_id)
                VALUES (%s, %s, 'stored', NULL, %s)
                ON CONFLICT (exam_session_id, question_number) DO UPDATE SET
                    status = 'stored', last_error = NULL, last_run_id = EXCLUDED.last_run_id,
                    attempts = question_attempts.attempts + 1, updated_at = now()
                """,
                (session_id, question.question_number, run_id),
            )
        assert row is not None
        inserted, previous_hash = row
        return bool(inserted) or previous_hash != digest

    async def record_attempt(
        self, session_id: int, number: int, status: AttemptStatus, error: str | None, run_id: uuid.UUID
    ) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO question_attempts (exam_session_id, question_number, status, last_error, last_run_id)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (exam_session_id, question_number) DO UPDATE SET
                    status = EXCLUDED.status, last_error = EXCLUDED.last_error,
                    last_run_id = EXCLUDED.last_run_id,
                    attempts = question_attempts.attempts + 1, updated_at = now()
                """,
                (session_id, number, status.value, error, run_id),
            )

    async def start_run(self, targets: list[str], access_provider: str) -> uuid.UUID:
        run_id = uuid.uuid4()
        async with self._pool.connection() as conn:
            await conn.execute(
                "INSERT INTO scrape_runs (id, target_sessions, access_provider) VALUES (%s, %s, %s)",
                (run_id, targets, access_provider),
            )
        return run_id

    async def finish_run(self, run_id: uuid.UUID, status: RunStatus, metrics: dict[str, Any]) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                """
                UPDATE scrape_runs SET finished_at = now(), status = %s, total_requested = %s,
                    total_succeeded = %s, total_failed = %s, retry_count = %s, diagnostics = %s
                WHERE id = %s
                """,
                (
                    status.value,
                    metrics.get("questions_requested", 0),
                    metrics.get("questions_succeeded", 0),
                    metrics.get("questions_failed", 0),
                    metrics.get("retries", 0),
                    Jsonb(metrics),
                    run_id,
                ),
            )

    async def fetch_records(self, session_codes: list[str] | None = None) -> list[dict[str, Any]]:
        async with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT s.exam_type, s.specialization, s.year, s.season, s.session_code,
                       q.question_number, q.question_data, q.correct_answer, q.answer_statistics,
                       q.validation_status, q.validation_issues
                FROM questions q JOIN exam_sessions s ON s.id = q.exam_session_id
                WHERE %s::text[] IS NULL OR s.session_code = ANY(%s::text[])
                ORDER BY s.year, s.season DESC, q.question_number
                """,
                (session_codes, session_codes),
            )
            rows = await cur.fetchall()
        return [_record_from_parts(row, row) for row in rows]

    async def summaries(self) -> list[SessionSummary]:
        async with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT s.session_code, s.year, s.season, s.collection_status, s.discovered_question_count,
                       s.last_error,
                       (SELECT count(*) FROM questions q WHERE q.exam_session_id = s.id) AS stored,
                       (SELECT count(*) FROM questions q WHERE q.exam_session_id = s.id
                            AND q.validation_status = 'needs_review') AS needs_review,
                       (SELECT count(*) FROM question_attempts a WHERE a.exam_session_id = s.id
                            AND a.status NOT IN ('stored', 'missing')) AS failed,
                       (SELECT count(*) FROM question_attempts a WHERE a.exam_session_id = s.id
                            AND a.status = 'missing'
                            AND a.question_number < COALESCE(s.discovered_question_count, 0)) AS missing
                FROM exam_sessions s ORDER BY s.year, s.season DESC
                """
            )
            rows = await cur.fetchall()
        return [SessionSummary(**row) for row in rows]

    async def last_runs(self, limit: int = 5) -> list[dict[str, Any]]:
        async with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT id, started_at, finished_at, status, total_requested, total_succeeded, total_failed,"
                " retry_count FROM scrape_runs ORDER BY started_at DESC LIMIT %s",
                (limit,),
            )
            return list(await cur.fetchall())


# --------------------------------------------------------------------------- in-memory


@dataclass
class _MemSession:
    state: SessionState
    meta: dict[str, Any]
    last_error: str | None = None
    questions: dict[int, tuple[ScrapedQuestion, str]] = field(default_factory=dict)
    attempts: dict[int, tuple[AttemptStatus, int, str | None]] = field(default_factory=dict)


class InMemoryRepository:
    """Same semantics as ``PostgresRepository``; used for dry runs and tests."""

    def __init__(self) -> None:
        self._sessions: dict[str, _MemSession] = {}
        self._by_id: dict[int, _MemSession] = {}
        self.runs: dict[uuid.UUID, dict[str, Any]] = {}

    async def close(self) -> None:
        return None

    async def ensure_session(self, session: ExamSession) -> SessionState:
        key = session.session_code
        if key not in self._sessions:
            state = SessionState(len(self._sessions) + 1, key, CollectionStatus.PENDING, None, None)
            meta = {
                "exam_type": session.exam_type,
                "specialization": session.specialization,
                "year": session.year,
                "season": session.season.value,
                "session_code": key,
            }
            self._sessions[key] = self._by_id[state.id] = _MemSession(state, meta)
        state = self._sessions[key].state
        return SessionState(**vars(state))

    async def set_session_status(
        self,
        session_id: int,
        status: CollectionStatus,
        *,
        discovered_question_count: int | None = None,
        expected_question_count: int | None = None,
        last_error: str | None = None,
    ) -> None:
        mem = self._by_id[session_id]
        mem.state.collection_status = status
        if discovered_question_count is not None:
            mem.state.discovered_question_count = discovered_question_count
        if expected_question_count is not None:
            mem.state.expected_question_count = expected_question_count
        mem.last_error = last_error

    async def attempt_statuses(self, session_id: int) -> dict[int, AttemptStatus]:
        mem = self._by_id[session_id]
        statuses = {number: status for number, (status, _, _) in mem.attempts.items()}
        statuses.update({number: AttemptStatus.STORED for number in mem.questions})
        return statuses

    async def upsert_question(self, session_id: int, question: ScrapedQuestion, run_id: uuid.UUID) -> bool:
        mem = self._by_id[session_id]
        digest = content_hash(question)
        previous = mem.questions.get(question.question_number)
        mem.questions[question.question_number] = (question, digest)
        await self.record_attempt(session_id, question.question_number, AttemptStatus.STORED, None, run_id)
        return previous is None or previous[1] != digest

    async def record_attempt(
        self, session_id: int, number: int, status: AttemptStatus, error: str | None, run_id: uuid.UUID
    ) -> None:
        mem = self._by_id[session_id]
        attempts = mem.attempts.get(number, (status, 0, None))[1] + 1
        mem.attempts[number] = (status, attempts, error)

    async def start_run(self, targets: list[str], access_provider: str) -> uuid.UUID:
        run_id = uuid.uuid4()
        self.runs[run_id] = {"status": RunStatus.RUNNING, "targets": targets, "started_at": datetime.now(UTC)}
        return run_id

    async def finish_run(self, run_id: uuid.UUID, status: RunStatus, metrics: dict[str, Any]) -> None:
        self.runs[run_id].update(status=status, metrics=metrics, finished_at=datetime.now(UTC))

    async def fetch_records(self, session_codes: list[str] | None = None) -> list[dict[str, Any]]:
        records = []
        for mem in sorted(self._sessions.values(), key=lambda m: m.state.session_code):
            if session_codes and mem.state.session_code not in session_codes:
                continue
            for number in sorted(mem.questions):
                question, _ = mem.questions[number]
                record = question.to_record()
                records.append(
                    record
                    | {
                        "validation_status": question.validation_status.value,
                        "validation_issues": question.validation_issues,
                    }
                )
        return records

    async def summaries(self) -> list[SessionSummary]:
        result = []
        for mem in self._sessions.values():
            statuses = [s for s, _, _ in mem.attempts.values()]
            result.append(
                SessionSummary(
                    session_code=mem.state.session_code,
                    year=mem.meta["year"],
                    season=mem.meta["season"],
                    collection_status=mem.state.collection_status.value,
                    discovered_question_count=mem.state.discovered_question_count,
                    stored=len(mem.questions),
                    needs_review=sum(q.validation_issues != [] for q, _ in mem.questions.values()),
                    failed=sum(s not in (AttemptStatus.STORED, AttemptStatus.MISSING) for s in statuses),
                    missing=sum(
                        s is AttemptStatus.MISSING and n < (mem.state.discovered_question_count or 0)
                        for n, (s, _, _) in mem.attempts.items()
                    ),
                    last_error=mem.last_error,
                )
            )
        return result
