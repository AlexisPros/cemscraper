"""Offline HTML parser for CEM PES question pages.

Pure functions only: no HTTP, no database. Saved HTML files can be parsed
completely offline (``cem-scraper parse page.html``).

Strategy (a port of the manually verified JavaScript parser):

1. Collect ``<p>`` elements inside ``.marginesy`` that are not inside the
   hidden answer/statistics containers.
2. Find the paragraph whose *direct* ``<b>`` child is the label ``A.``.
3. The question is the nonempty paragraph(s) immediately preceding it
   (back to the ``<center>`` metadata line, if present).
4. Options are the following paragraphs labelled ``B.``, ``C.``, ...
5. ``<br>`` becomes ``\\n``; a trailing ``Prawidłowa odpowiedź to:`` is removed.
6. Correct answer comes from the hidden ``#odpowiedz1`` block, statistics from
   ``#statystyki1 table`` matched by normalised header names.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import StrEnum

from bs4 import BeautifulSoup, NavigableString, Tag
from pydantic import ValidationError

from cem_scraper.models import (
    AnswerStatistics,
    Attachment,
    CorrectAnswer,
    ExamSession,
    QuestionData,
    ScrapedQuestion,
)

ANSWER_PHRASE_RE = re.compile(r"\s*Prawidłowa\s+odpowiedź\s+to\s*:?\s*$", re.IGNORECASE)
ANSWER_LETTER_RE = re.compile(r"Prawidłowa\s+odpowiedź\s+to\s*:?\s*([A-J])\b", re.IGNORECASE)
OPTION_LABEL_RE = re.compile(r"^\s*([A-J])\s*[.)]\s*$")
HIDDEN_CONTAINER_ID_RE = re.compile(r"^(odpowiedz|statystyki)", re.IGNORECASE)
QUESTION_NUMBER_PATTERNS = [
    re.compile(r"pytani\w*\s*(?:nr\.?|numer)?\s*:?\s*(\d{1,3})\b", re.IGNORECASE),
    re.compile(r"\bnr\.?\s*(\d{1,3})\b", re.IGNORECASE),
]
QUESTION_TOTAL_RE = re.compile(r"\b(\d{1,3})\s*(?:z|/|of)\s*(\d{1,3})\b", re.IGNORECASE)
PERCENT_HEADER_RE = re.compile(r"^procent\s*odp\w*\s*([a-e])$")
DIFFICULTY_HEADER_RE = re.compile(r"^wskaznik\s*trudnosci")


class PageKind(StrEnum):
    QUESTION = "question"
    EULA = "eula"
    ENTRY_FORM = "entry_form"  # session selection + reCAPTCHA: authorization is missing/expired
    NO_QUESTION = "no_question"  # recognisable layout, but no question content
    UNKNOWN = "unknown"


class ParseError(Exception):
    """Base class for parser failures."""


class AuthorizationPageError(ParseError):
    def __init__(self, kind: PageKind) -> None:
        super().__init__(f"received {kind.value} page instead of a question (authorization missing or expired)")
        self.kind = kind


class NoQuestionContent(ParseError):
    """The page has the question layout but no question: treated as a missing question."""


class UnexpectedContent(ParseError):
    """The page is not recognisable at all."""


class MalformedQuestion(ParseError):
    """A question was found but could not be turned into valid structured data."""


class QuestionNumberMismatch(ParseError):
    def __init__(self, requested: int, found: int) -> None:
        super().__init__(f"requested question {requested} but page shows question {found}")
        self.requested = requested
        self.found = found


@dataclass
class ParsedPage:
    question_data: QuestionData
    correct_answer: CorrectAnswer | None
    answer_statistics: AnswerStatistics | None
    extra_statistics: dict[str, str] | None
    detected_number: int | None
    reported_total: int | None
    issues: list[str] = field(default_factory=list)

    def to_scraped(self, session: ExamSession, question_number: int, source_url: str | None = None) -> ScrapedQuestion:
        try:
            return ScrapedQuestion.build(
                session,
                question_number,
                question_data=self.question_data,
                correct_answer=self.correct_answer,
                answer_statistics=self.answer_statistics,
                extra_statistics=self.extra_statistics,
                source_url=source_url,
                validation_issues=self.issues,
            )
        except ValidationError as exc:
            raise MalformedQuestion(str(exc)) from exc


@dataclass
class EntryForm:
    action: str | None
    fields: dict[str, str]
    recaptcha_sitekey: str | None
    session_options: dict[str, str]
    specialization_options: dict[str, str]


# --------------------------------------------------------------------------- helpers


def decode_html(content: bytes, header_encoding: str | None = None) -> str:
    """Decode bytes using the HTTP charset, then <meta charset>, then UTF-8/CP1250."""
    candidates: list[str] = []
    if header_encoding:
        candidates.append(header_encoding)
    meta = re.search(rb"<meta[^>]+charset\s*=\s*[\"']?([A-Za-z0-9_\-]+)", content[:4096], re.IGNORECASE)
    if meta:
        candidates.append(meta.group(1).decode("ascii", "ignore"))
    candidates += ["utf-8", "cp1250", "iso-8859-2"]
    for encoding in candidates:
        try:
            return content.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return content.decode("utf-8", errors="replace")


def make_soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


def attr(tag: Tag, name: str) -> str:
    """A tag attribute as a string (BeautifulSoup returns lists for multi-valued attributes)."""
    value = tag.get(name)
    if value is None:
        return ""
    return value if isinstance(value, str) else " ".join(value)


def normalize_header(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.replace("ł", "l").replace("Ł", "L"))
    ascii_only = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    ascii_only = re.sub(r"[^a-zA-Z0-9]+", " ", ascii_only)
    return " ".join(ascii_only.lower().split())


def _clean_lines(raw: str) -> str:
    raw = unicodedata.normalize("NFC", raw.replace("\xa0", " "))
    lines = [" ".join(line.split()) for line in raw.split("\n")]
    text = "\n".join(lines).strip("\n ")
    return re.sub(r"\n{3,}", "\n\n", text)


def element_text(
    element: Tag, attachments: list[Attachment] | None = None, location: str = "", skip: Tag | None = None
) -> str:
    """Text of an element with ``<br>`` -> newline and whitespace normalised.

    Images and tables are recorded as attachments instead of being silently dropped;
    table cell text is kept inline (one row per line, cells separated by `` | ``).
    ``skip`` excludes one descendant (the option label) without copying the tree.
    """
    parts: list[str] = []

    def walk(node: Tag) -> None:
        for child in node.children:
            if child is skip:
                continue
            if isinstance(child, NavigableString):
                if child.__class__.__name__ in ("Comment", "Script", "Stylesheet"):
                    continue
                parts.append(re.sub(r"\s+", " ", str(child)))
            elif isinstance(child, Tag):
                name = child.name.lower()
                if name in ("script", "style"):
                    continue
                if name == "br":
                    parts.append("\n")
                elif name == "img":
                    if attachments is not None:
                        attachments.append(
                            Attachment(
                                kind="image",
                                src=attr(child, "src") or None,
                                alt=attr(child, "alt") or None,
                                location=location,
                            )
                        )
                elif name == "table":
                    if attachments is not None:
                        attachments.append(Attachment(kind="table", location=location))
                    for row in child.find_all("tr"):
                        cells = [element_text(cell) for cell in row.find_all(["td", "th"])]
                        parts.append("\n" + " | ".join(cells) + "\n")
                elif name in ("p", "div", "li"):
                    parts.append("\n")
                    walk(child)
                    parts.append("\n")
                else:
                    walk(child)

    walk(element)
    return _clean_lines("".join(parts))


def _in_hidden_container(tag: Tag, root: Tag) -> bool:
    for parent in tag.parents:
        if parent is root or parent is None:
            return False
        if HIDDEN_CONTAINER_ID_RE.match(attr(parent, "id")):
            return True
    return False


def _option_label(paragraph: Tag) -> str | None:
    """Label of an option paragraph, read from its *direct* ``<b>`` child."""
    bold = paragraph.find(["b", "strong"], recursive=False)
    if bold is None:
        return None
    match = OPTION_LABEL_RE.match(bold.get_text())
    return match.group(1).upper() if match else None


def _option_text(paragraph: Tag, attachments: list[Attachment], label: str) -> str:
    bold = paragraph.find(["b", "strong"], recursive=False)
    return element_text(paragraph, attachments, location=f"option {label}", skip=bold)


def _parse_decimal(raw: str) -> Decimal:
    cleaned = raw.strip().replace("%", "").replace("\xa0", "").replace(" ", "").replace(",", ".")
    try:
        value = Decimal(cleaned)
    except InvalidOperation as exc:
        raise MalformedQuestion(f"non-numeric statistic value {raw!r}") from exc
    if not value.is_finite():
        raise MalformedQuestion(f"non-finite statistic value {raw!r}")
    return value


# --------------------------------------------------------------------------- page classification


def _marginesy(soup: BeautifulSoup) -> Tag | None:
    return soup.select_one(".marginesy")


def _visible_paragraphs(root: Tag) -> list[Tag]:
    return [p for p in root.find_all("p") if not _in_hidden_container(p, root)]


def classify_page(soup: BeautifulSoup) -> PageKind:
    root = _marginesy(soup)
    if root is not None and any(_option_label(p) == "A" for p in _visible_paragraphs(root)):
        return PageKind.QUESTION
    if soup.select_one(".g-recaptcha, [data-sitekey], select[name=sesja]") is not None:
        return PageKind.ENTRY_FORM
    if soup.select_one("input[name=akceptacja], button[name=akceptacja]") is not None:
        return PageKind.EULA
    if root is not None:
        return PageKind.NO_QUESTION
    return PageKind.UNKNOWN


# --------------------------------------------------------------------------- question page


def _find_question_number(root: Tag, option_a: Tag) -> tuple[int | None, int | None]:
    texts: list[str] = []
    for center in root.find_all("center"):
        if not _in_hidden_container(center, root):
            texts.append(center.get_text(" ", strip=True))
    for text in texts:
        total_match = QUESTION_TOTAL_RE.search(text)
        for pattern in QUESTION_NUMBER_PATTERNS:
            match = pattern.search(text)
            if match:
                number = int(match.group(1))
                total = int(total_match.group(2)) if total_match and int(total_match.group(1)) == number else None
                return number, total
    return None, None


def _question_paragraphs(option_a: Tag) -> list[Tag]:
    """Nonempty ``<p>`` siblings preceding option A, back to the ``<center>`` line."""
    collected: list[Tag] = []
    for sibling in option_a.find_previous_siblings():
        if not isinstance(sibling, Tag):
            continue
        name = sibling.name.lower()
        if name == "center":
            break
        if name == "br":
            continue
        if name != "p":
            if collected:
                break
            continue
        if _option_label(sibling) is not None:
            break
        if not sibling.get_text(strip=True) and sibling.find(["img", "table"]) is None:
            continue
        collected.append(sibling)
        # Without a <center> marker we cannot tell the question from page headers,
        # so only the nearest paragraph is taken (the verified JS behaviour).
        if option_a.find_previous_sibling("center") is None:
            break
    collected.reverse()
    return collected


def _option_paragraphs(option_a: Tag) -> list[tuple[str, Tag]]:
    options: list[tuple[str, Tag]] = [("A", option_a)]
    expected = "B"
    for sibling in option_a.find_next_siblings():
        if not isinstance(sibling, Tag) or sibling.name.lower() == "br":
            continue
        if sibling.name.lower() != "p":
            break
        label = _option_label(sibling)
        if label is None:
            if not sibling.get_text(strip=True):
                continue
            break
        if label != expected:
            break
        options.append((label, sibling))
        expected = chr(ord(expected) + 1)
    return options


def _parse_correct_answer(soup: BeautifulSoup, issues: list[str]) -> CorrectAnswer | None:
    block = soup.select_one("#odpowiedz1") or soup.select_one("[id^=odpowiedz]")
    if block is None:
        return None
    text = " ".join(block.get_text(" ", strip=True).split())
    match = ANSWER_LETTER_RE.search(text)
    if match is None:
        issues.append(f"could not read correct answer from {text[:120]!r}")
        return None
    letter = match.group(1).upper()
    if letter not in "ABCDE":
        issues.append(f"correct answer letter {letter} outside A-E")
        return None
    return CorrectAnswer(option=letter)  # type: ignore[arg-type]


def _table_rows(table: Tag) -> list[list[str]]:
    rows = []
    for tr in table.find_all("tr"):
        cells = [" ".join(cell.get_text(" ", strip=True).split()) for cell in tr.find_all(["th", "td"])]
        if any(cells):
            rows.append(cells)
    return rows


def _statistics_pairs(rows: list[list[str]]) -> list[tuple[str, str]]:
    """Header/value pairs for horizontal (header row + value row) or vertical tables."""

    def is_known(header: str) -> bool:
        norm = normalize_header(header)
        return bool(PERCENT_HEADER_RE.match(norm) or DIFFICULTY_HEADER_RE.match(norm))

    for index, row in enumerate(rows[:-1]):
        if sum(is_known(cell) for cell in row) >= 2:
            values = rows[index + 1]
            if len(values) < len(row):
                raise MalformedQuestion(f"statistics value row has {len(values)} cells for {len(row)} headers")
            return list(zip(row, values, strict=False))
    if rows and all(len(row) >= 2 for row in rows) and sum(is_known(row[0]) for row in rows) >= 2:
        return [(row[0], row[1]) for row in rows]
    raise MalformedQuestion("statistics table headers not recognised")


def _parse_statistics(soup: BeautifulSoup, issues: list[str]) -> tuple[AnswerStatistics | None, dict[str, str] | None]:
    table = soup.select_one("#statystyki1 table") or soup.select_one("[id^=statystyki] table")
    if table is None:
        return None, None
    pairs = _statistics_pairs(_table_rows(table))
    values: dict[str, Decimal] = {}
    extra: dict[str, str] = {}
    for header, raw in pairs:
        norm = normalize_header(header)
        percent = PERCENT_HEADER_RE.match(norm)
        if percent:
            key = percent.group(1).upper()
        elif DIFFICULTY_HEADER_RE.match(norm):
            key = "difficulty_index"
        else:
            extra[header] = raw
            continue
        if key in values:
            raise MalformedQuestion(f"duplicate statistics column {header!r}")
        values[key] = _parse_decimal(raw)
    missing = [key for key in ("A", "B", "C", "D", "E", "difficulty_index") if key not in values]
    if missing:
        raise MalformedQuestion(f"statistics table lacks required columns {missing}")
    try:
        stats = AnswerStatistics(**values)
    except ValidationError as exc:
        raise MalformedQuestion(f"invalid statistics: {exc}") from exc
    return stats, extra or None


def parse_question_page(html: str, expected_number: int | None = None) -> ParsedPage:
    """Parse a question page. Raises a ``ParseError`` subclass on failure."""
    soup = make_soup(html)
    kind = classify_page(soup)
    if kind in (PageKind.EULA, PageKind.ENTRY_FORM):
        raise AuthorizationPageError(kind)
    if kind is PageKind.NO_QUESTION:
        raise NoQuestionContent("page layout present but no question content")
    if kind is PageKind.UNKNOWN:
        raise UnexpectedContent("page does not contain the expected .marginesy question layout")

    root = _marginesy(soup)
    assert root is not None
    option_a = next(p for p in _visible_paragraphs(root) if _option_label(p) == "A")
    issues: list[str] = []
    attachments: list[Attachment] = []

    detected_number, reported_total = _find_question_number(root, option_a)
    if expected_number is not None:
        if detected_number is None:
            issues.append("question number could not be verified from the page")
        elif detected_number != expected_number:
            raise QuestionNumberMismatch(expected_number, detected_number)

    question_paragraphs = _question_paragraphs(option_a)
    if not question_paragraphs:
        raise MalformedQuestion("no question paragraph found before option A")
    text = "\n".join(element_text(p, attachments, location="question") for p in question_paragraphs)
    text = ANSWER_PHRASE_RE.sub("", text).strip()

    options = {label: _option_text(paragraph, attachments, label) for label, paragraph in _option_paragraphs(option_a)}

    try:
        question_data = QuestionData(text=text, options=options, attachments=attachments or None)
    except ValidationError as exc:
        raise MalformedQuestion(f"invalid question data: {exc}") from exc

    correct_answer = _parse_correct_answer(soup, issues)
    statistics, extra = _parse_statistics(soup, issues)

    return ParsedPage(
        question_data=question_data,
        correct_answer=correct_answer,
        answer_statistics=statistics,
        extra_statistics=extra,
        detected_number=detected_number,
        reported_total=reported_total,
        issues=issues,
    )


# --------------------------------------------------------------------------- entry form


def parse_entry_form(html: str) -> EntryForm:
    """Read the official session-selection form (fields, select options, reCAPTCHA sitekey).

    Used to *verify* session codes (``YYYY1``/``YYYY2``) and the specialization code
    instead of assuming them. Never returns a ``g-recaptcha-response`` value.
    """
    soup = make_soup(html)
    form = None
    for candidate in soup.find_all("form"):
        if candidate.select_one("select[name=sesja], .g-recaptcha, [data-sitekey]"):
            form = candidate
            break
    scope: Tag = form or soup
    fields: dict[str, str] = {}
    for element in scope.find_all(["input", "select", "textarea"]):
        name = attr(element, "name")
        if not name or name == "g-recaptcha-response":
            continue
        if element.name == "select":
            selected = element.find("option", selected=True) or element.find("option")
            fields[name] = attr(selected, "value") if selected else ""
        elif attr(element, "type").lower() in ("submit", "button", "image"):
            continue
        else:
            fields[name] = attr(element, "value")

    def options_of(name: str) -> dict[str, str]:
        select = scope.find("select", attrs={"name": name})
        if select is None:
            return {}
        return {
            attr(opt, "value").strip(): " ".join(opt.get_text(" ", strip=True).split())
            for opt in select.find_all("option")
            if attr(opt, "value").strip()
        }

    sitekey_el = soup.select_one("[data-sitekey]")
    return EntryForm(
        action=attr(form, "action") or None if form else None,
        fields=fields,
        recaptcha_sitekey=attr(sitekey_el, "data-sitekey") or None if sitekey_el else None,
        session_options=options_of("sesja"),
        specialization_options=options_of("specjalizacja"),
    )
