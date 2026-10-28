from __future__ import annotations

import json
from decimal import Decimal

import pytest
from pydantic import ValidationError

from cem_scraper.config import Settings, default_target_sessions
from cem_scraper.models import (
    AnswerStatistics,
    CorrectAnswer,
    ExamSession,
    QuestionData,
    ScrapedQuestion,
    Season,
    ValidationStatus,
)

OPTIONS = {k: f"opcja {k}" for k in "ABCDE"}


def stats(**overrides: str) -> AnswerStatistics:
    values = {"A": "20", "B": "20", "C": "20", "D": "20", "E": "20", "difficulty_index": "0.2"} | overrides
    return AnswerStatistics(**{k: Decimal(v) for k, v in values.items()})


def test_session_codes() -> None:
    assert ExamSession.from_code("20031").season is Season.SPRING
    assert ExamSession.from_code("20262").session_code == "20262"
    assert ExamSession(year=2013, season=Season.AUTUMN).session_code == "20132"
    for bad in ("2003", "20033", "abcd1", "200311"):
        with pytest.raises(ValueError):
            ExamSession.from_code(bad)


def test_default_targets_are_the_23_required_sessions() -> None:
    targets = default_target_sessions()
    assert len(targets) == 23
    assert targets[0] == "20031" and targets[21] == "20132" and targets[-1] == "20262"


def test_target_sessions_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CEM_TARGET_SESSIONS", "20031, 20262")
    assert Settings(_env_file=None).target_sessions == ["20031", "20262"]  # type: ignore[call-arg]


def test_question_data_validation() -> None:
    QuestionData(text="Pytanie?", options=OPTIONS)
    with pytest.raises(ValidationError, match="empty"):
        QuestionData(text="   ", options=OPTIONS)
    with pytest.raises(ValidationError, match="consecutive"):
        QuestionData(text="Pytanie?", options={"A": "a", "C": "c"})
    with pytest.raises(ValidationError, match="empty option"):
        QuestionData(text="Pytanie?", options=OPTIONS | {"E": ""})


def test_correct_answer_must_be_a_to_e() -> None:
    assert CorrectAnswer(option="E").option == "E"
    with pytest.raises(ValidationError):
        CorrectAnswer(option="F")  # type: ignore[arg-type]


def test_statistics_hard_limits() -> None:
    with pytest.raises(ValidationError, match="outside 0-100"):
        stats(A="-1")
    with pytest.raises(ValidationError, match="finite"):
        stats(B="NaN")


def test_statistics_soft_issues() -> None:
    assert stats().soft_issues() == []
    assert stats(A="20.6", B="20.6").soft_issues() == []  # rounding tolerance
    assert "sum to" in stats(A="50").soft_issues()[0]
    assert "outside documented 0-1" in stats(difficulty_index="1.7").soft_issues()[0]


def test_decimal_serialises_as_json_number() -> None:
    payload = json.dumps(stats(A="84.2", difficulty_index="0.900").model_dump(mode="json"))
    assert '"A": 84.2' in payload and '"difficulty_index": 0.9' in payload


def test_scraped_question_flags_answer_not_in_options() -> None:
    question = ScrapedQuestion.build(
        ExamSession.from_code("20031"),
        3,
        question_data=QuestionData(text="P?", options={"A": "a", "B": "b", "C": "c"}),
        correct_answer=CorrectAnswer(option="E"),
        answer_statistics=stats(),
    )
    assert question.validation_status is ValidationStatus.NEEDS_REVIEW
    assert any("not among the options" in i for i in question.validation_issues)


def test_record_shape_has_three_independent_objects() -> None:
    question = ScrapedQuestion.build(
        ExamSession.from_code("20262"),
        5,
        question_data=QuestionData(text="P?", options=OPTIONS),
        correct_answer=CorrectAnswer(option="A"),
        answer_statistics=stats(),
        extra_statistics={"M.D.": "0.1"},
    )
    record = question.to_record()
    assert list(record) == [
        "exam", "specialization", "year", "season", "session_id", "question_number",
        "question_data", "correct_answer", "answer_statistics",
    ]
    assert record["question_data"] == {"text": "P?", "options": OPTIONS}
