"""Pydantic v2 data models.

Hard validation errors (``ValidationError``) mean the data is unusable.
Soft problems (unusual difficulty index, percentages not summing to ~100,
non-standard option set, ...) are collected as ``validation_issues`` and mark a
question ``needs_review`` instead of discarding it.
"""

from __future__ import annotations

import re
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    computed_field,
    field_validator,
    model_validator,
)

from cem_scraper.config import EXAM_TYPE, PSYCHIATRY_SPECIALIZATION_CODE, SPECIALIZATION_NAME

STANDARD_OPTION_LABELS = ("A", "B", "C", "D", "E")
PERCENT_SUM_TOLERANCE = Decimal("2.0")

# Decimals keep the exact source representation in Python but serialize to JSON
# numbers (not strings), so `0.900` becomes `0.9` and `84.2` stays `84.2`.
JsonDecimal = Annotated[Decimal, PlainSerializer(float, return_type=float, when_used="json")]


class Season(StrEnum):
    SPRING = "spring"
    AUTUMN = "autumn"


class ValidationStatus(StrEnum):
    VALID = "valid"
    NEEDS_REVIEW = "needs_review"


class ExamSession(BaseModel):
    """One examination session, e.g. Spring 2003 -> ``20031``.

    The ``YYYY1``/``YYYY2`` convention was observed for 20031 and 20262 only. It is
    treated as a hypothesis; ``verify-sessions`` checks it against the official
    form's ``<select name="sesja">`` options.
    """

    model_config = ConfigDict(frozen=True)

    exam_type: str = EXAM_TYPE
    specialization: str = SPECIALIZATION_NAME
    specialization_code: str = PSYCHIATRY_SPECIALIZATION_CODE
    year: int = Field(ge=1990, le=2100)
    season: Season

    @computed_field  # type: ignore[prop-decorator]
    @property
    def session_code(self) -> str:
        return f"{self.year}{1 if self.season is Season.SPRING else 2}"

    @classmethod
    def from_code(cls, code: str, specialization_code: str = PSYCHIATRY_SPECIALIZATION_CODE) -> ExamSession:
        match = re.fullmatch(r"(\d{4})([12])", code.strip())
        if not match:
            raise ValueError(f"Invalid session code {code!r}; expected YYYY1 or YYYY2")
        season = Season.SPRING if match.group(2) == "1" else Season.AUTUMN
        return cls(year=int(match.group(1)), season=season, specialization_code=specialization_code)

    @property
    def label(self) -> str:
        return f"{self.season.value.capitalize()} {self.year} ({self.session_code})"


class Attachment(BaseModel):
    """Non-text content found in a question (image, table...) kept as a reference."""

    kind: str
    src: str | None = None
    alt: str | None = None
    location: str


class QuestionData(BaseModel):
    text: str
    options: dict[str, str]
    attachments: list[Attachment] | None = None

    @field_validator("text")
    @classmethod
    def _text_nonempty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("question text is empty")
        return value

    @field_validator("options")
    @classmethod
    def _options_valid(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) < 2:
            raise ValueError(f"expected at least 2 options, got {len(value)}")
        labels = list(value)
        expected = [chr(ord("A") + i) for i in range(len(labels))]
        if labels != expected:
            raise ValueError(f"option labels must be consecutive from A, got {labels}")
        empty = [label for label, text in value.items() if not text.strip()]
        if empty:
            raise ValueError(f"empty option text for {empty}")
        return value

    @property
    def is_standard(self) -> bool:
        return tuple(self.options) == STANDARD_OPTION_LABELS


class CorrectAnswer(BaseModel):
    option: Literal["A", "B", "C", "D", "E"]


class AnswerStatistics(BaseModel):
    A: JsonDecimal
    B: JsonDecimal
    C: JsonDecimal
    D: JsonDecimal
    E: JsonDecimal
    difficulty_index: JsonDecimal

    @field_validator("A", "B", "C", "D", "E", "difficulty_index")
    @classmethod
    def _finite(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ValueError("statistic must be a finite number")
        return value

    @field_validator("A", "B", "C", "D", "E")
    @classmethod
    def _percent_range(cls, value: Decimal) -> Decimal:
        if not Decimal(0) <= value <= Decimal(100):
            raise ValueError(f"percentage {value} outside 0-100")
        return value

    def percent_sum(self) -> Decimal:
        return self.A + self.B + self.C + self.D + self.E

    def soft_issues(self) -> list[str]:
        issues: list[str] = []
        total = self.percent_sum()
        if abs(total - Decimal(100)) > PERCENT_SUM_TOLERANCE:
            issues.append(f"answer percentages sum to {total}, expected ~100")
        # CEM's "wskaźnik trudności" is the fraction of examinees answering
        # correctly, so the documented range is 0..1.
        if not Decimal(0) <= self.difficulty_index <= Decimal(1):
            issues.append(f"difficulty_index {self.difficulty_index} outside documented 0-1 range")
        return issues


class ScrapedQuestion(BaseModel):
    exam: str = EXAM_TYPE
    specialization: str = SPECIALIZATION_NAME
    year: int
    season: Season
    session_id: str
    question_number: int = Field(ge=1)

    question_data: QuestionData
    correct_answer: CorrectAnswer | None
    answer_statistics: AnswerStatistics | None

    extra_statistics: dict[str, str] | None = Field(default=None, exclude=True)
    source_url: str | None = Field(default=None, exclude=True)
    validation_issues: list[str] = Field(default_factory=list, exclude=True)

    @model_validator(mode="after")
    def _collect_soft_issues(self) -> ScrapedQuestion:
        issues = list(self.validation_issues)
        if not self.question_data.is_standard:
            issues.append(f"non-standard option set {list(self.question_data.options)}")
        if self.question_data.attachments:
            issues.append("question contains non-text content (see attachments)")
        if self.correct_answer is None:
            issues.append("correct answer missing")
        elif self.correct_answer.option not in self.question_data.options:
            issues.append(f"correct answer {self.correct_answer.option} is not among the options")
        if self.answer_statistics is None:
            issues.append("answer statistics missing")
        else:
            issues.extend(self.answer_statistics.soft_issues())
        # dedupe while preserving order (validators may run more than once)
        self.validation_issues = list(dict.fromkeys(issues))
        return self

    @property
    def validation_status(self) -> ValidationStatus:
        return ValidationStatus.NEEDS_REVIEW if self.validation_issues else ValidationStatus.VALID

    @classmethod
    def build(cls, session: ExamSession, question_number: int, **parts: Any) -> ScrapedQuestion:
        return cls(
            exam=session.exam_type,
            specialization=session.specialization,
            year=session.year,
            season=session.season,
            session_id=session.session_code,
            question_number=question_number,
            **parts,
        )

    def to_record(self) -> dict[str, Any]:
        """The public JSON shape (metadata + the three independent objects)."""
        return self.model_dump(mode="json", exclude_none=False, exclude={"question_data": {"attachments"}}) | {
            "question_data": self.question_data.model_dump(mode="json", exclude_none=True)
        }


class AttemptStatus(StrEnum):
    """Per-question outcome. Distinguishes missing content from the failure kinds."""

    STORED = "stored"
    MISSING = "missing"
    PARSE_ERROR = "parse_error"
    NUMBER_MISMATCH = "number_mismatch"
    UNEXPECTED_CONTENT = "unexpected_content"
    AUTH_ERROR = "auth_error"
    SERVER_ERROR = "server_error"


class CollectionStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    FAILED = "failed"


class RunStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"
    INTERRUPTED = "interrupted"
    BLOCKED = "blocked"
    FAILED = "failed"
