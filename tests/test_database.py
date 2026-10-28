"""PostgreSQL persistence tests (embedded pgserver or CEM_TEST_DATABASE_URL)."""

from __future__ import annotations

from decimal import Decimal

import psycopg
import pytest

from cem_scraper.database import PostgresRepository
from cem_scraper.models import (
    AnswerStatistics,
    AttemptStatus,
    CollectionStatus,
    CorrectAnswer,
    ExamSession,
    QuestionData,
    RunStatus,
    ScrapedQuestion,
)

pytestmark = pytest.mark.db

SESSION = ExamSession.from_code("20031")


def make_question(number: int = 1, text: str = "Pytanie?", answer: str = "A") -> ScrapedQuestion:
    return ScrapedQuestion.build(
        SESSION,
        number,
        question_data=QuestionData(text=text, options={k: f"opcja {k}" for k in "ABCDE"}),
        correct_answer=CorrectAnswer(option=answer),  # type: ignore[arg-type]
        answer_statistics=AnswerStatistics(
            A=Decimal("84.2"), B=Decimal("8.8"), C=Decimal("1.8"), D=Decimal("3.5"), E=Decimal("1.8"),
            difficulty_index=Decimal("0.900"),
        ),
        extra_statistics={"M.D.": "0.12"},
        source_url="https://example.invalid/q",
    )


async def test_migrations_are_idempotent(pg_repo: PostgresRepository) -> None:
    assert await pg_repo.migrate() == []


async def test_ensure_session_is_idempotent(pg_repo: PostgresRepository) -> None:
    first = await pg_repo.ensure_session(SESSION)
    second = await pg_repo.ensure_session(SESSION)
    assert first.id == second.id
    assert first.collection_status is CollectionStatus.PENDING


async def test_upsert_prevents_duplicates_and_detects_changes(pg_repo: PostgresRepository, postgres_dsn: str) -> None:
    state = await pg_repo.ensure_session(SESSION)
    run = await pg_repo.start_run(["20031"], "test")
    assert await pg_repo.upsert_question(state.id, make_question(), run) is True
    assert await pg_repo.upsert_question(state.id, make_question(), run) is False
    assert await pg_repo.upsert_question(state.id, make_question(text="Pytanie zmienione?"), run) is True

    async with await psycopg.AsyncConnection.connect(postgres_dsn) as conn:
        cur = await conn.execute(
            "SELECT count(*), min(question_data->>'text'), min(answer_statistics->>'difficulty_index'),"
            " min(correct_answer->>'option'), min(extra_statistics->>'M.D.') FROM questions"
        )
        assert await cur.fetchone() == (1, "Pytanie zmienione?", "0.9", "A", "0.12")
        cur = await conn.execute("SELECT attempts, status FROM question_attempts")
        assert await cur.fetchone() == (3, "stored")
        cur = await conn.execute(
            "SELECT jsonb_typeof(question_data), jsonb_typeof(correct_answer), jsonb_typeof(answer_statistics),"
            " (answer_statistics->'A')::numeric FROM questions"
        )
        assert await cur.fetchone() == ("object", "object", "object", Decimal("84.2"))


async def test_unique_constraint_enforced_at_db_level(pg_repo: PostgresRepository, postgres_dsn: str) -> None:
    state = await pg_repo.ensure_session(SESSION)
    run = await pg_repo.start_run(["20031"], "test")
    await pg_repo.upsert_question(state.id, make_question(), run)
    async with await psycopg.AsyncConnection.connect(postgres_dsn) as conn:
        with pytest.raises(psycopg.errors.UniqueViolation):
            await conn.execute(
                "INSERT INTO questions (exam_session_id, question_number, question_data, validation_status,"
                " content_hash, fetched_at) VALUES (%s, 1, '{}', 'valid', 'x', now())",
                (state.id,),
            )


async def test_attempt_statuses_and_summaries(pg_repo: PostgresRepository) -> None:
    state = await pg_repo.ensure_session(SESSION)
    run = await pg_repo.start_run(["20031"], "test")
    await pg_repo.upsert_question(state.id, make_question(1), run)
    await pg_repo.record_attempt(state.id, 2, AttemptStatus.PARSE_ERROR, "boom", run)
    await pg_repo.record_attempt(state.id, 3, AttemptStatus.MISSING, None, run)
    await pg_repo.record_attempt(state.id, 2, AttemptStatus.SERVER_ERROR, "again", run)
    statuses = await pg_repo.attempt_statuses(state.id)
    assert statuses == {1: AttemptStatus.STORED, 2: AttemptStatus.SERVER_ERROR, 3: AttemptStatus.MISSING}

    await pg_repo.record_attempt(state.id, 9, AttemptStatus.MISSING, None, run)  # past the end: not a gap
    await pg_repo.set_session_status(state.id, CollectionStatus.FAILED, discovered_question_count=4, last_error="x")
    (summary,) = await pg_repo.summaries()
    assert (summary.stored, summary.failed, summary.missing, summary.discovered_question_count) == (1, 1, 1, 4)


async def test_runs_and_export_records(pg_repo: PostgresRepository) -> None:
    state = await pg_repo.ensure_session(SESSION)
    run = await pg_repo.start_run(["20031"], "test")
    await pg_repo.upsert_question(state.id, make_question(2, answer="B"), run)
    await pg_repo.upsert_question(state.id, make_question(1), run)
    await pg_repo.finish_run(run, RunStatus.COMPLETED, {"questions_requested": 2, "questions_succeeded": 2, "retries": 1})
    records = await pg_repo.fetch_records()
    assert [r["question_number"] for r in records] == [1, 2]
    assert records[0]["session_id"] == "20031" and records[0]["season"] == "spring"
    assert records[0]["answer_statistics"]["A"] == 84.2
    assert await pg_repo.fetch_records(["20262"]) == []
    (last,) = await pg_repo.last_runs()
    assert (last["status"], last["total_succeeded"], last["retry_count"]) == ("completed", 2, 1)


async def test_check_constraints_reject_bad_data(pg_repo: PostgresRepository, postgres_dsn: str) -> None:
    async with await psycopg.AsyncConnection.connect(postgres_dsn) as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            await conn.execute(
                "INSERT INTO exam_sessions (exam_type, specialization, specialization_code, year, season, session_code)"
                " VALUES ('PES', 'Psychiatria', '725', 2003, 'spring', '20033')"
            )
