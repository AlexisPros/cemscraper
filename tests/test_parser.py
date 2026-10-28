"""Parser tests. All HTML is SYNTHETIC (see tests/fixtures/README.md); the expected
values of the two reference questions come from the manually verified browser runs."""

from __future__ import annotations

from decimal import Decimal

import pytest

from cem_scraper.models import ExamSession, ValidationStatus
from cem_scraper.parser import (
    AuthorizationPageError,
    MalformedQuestion,
    NoQuestionContent,
    PageKind,
    QuestionNumberMismatch,
    UnexpectedContent,
    classify_page,
    decode_html,
    detect_session_code,
    make_soup,
    parse_entry_form,
    parse_question_page,
)
from tests.conftest import EDGE, load, question_page

REFERENCE_A_TEXT = (
    "Zanik jąder ogoniastych i soczewkowatych daje charakterystyczny miseczkowaty kształt rogów "
    "czołowych komór bocznych w badaniu MR w:"
)
REFERENCE_B_TEXT = (
    "Wskaż prawdziwe stwierdzenia dotyczące zaburzeń obsesyjno-kompulsyjnych (OCD):\n\n"
    "1. według klasyfikacji ICD-10 objawy OCD powinny występować co najmniej przez godzinę dziennie;\n"
    "2. klomipramina ma porównywalną skuteczność w leczeniu OCD do leków z grupy SSRI;\n"
    "3. selektywne inhibitory wychwytu zwrotnego serotoniny (SSRI) są uważane za leki pierwszego rzutu "
    "w leczeniu OCD, z wyjątkiem citalopramu;\n"
    "4. przy braku odpowiedzi na jeden lek z grupy SSRI nie zaleca się zmiany na inny z tej grupy z powodu "
    "oporności krzyżowej;\n"
    "5. w przypadku OCD odpowiedź na leczenie SSRI występuje szybciej niż u pacjentów z zaburzeniami nastroju."
)


# ------------------------------------------------------------------ reference cases


def test_reference_a_spring_2003(html_2003: str) -> None:
    parsed = parse_question_page(html_2003, expected_number=1)
    record = parsed.to_scraped(ExamSession.from_code("20031"), 1).to_record()
    assert record == {
        "exam": "PES",
        "specialization": "Psychiatria",
        "year": 2003,
        "season": "spring",
        "session_id": "20031",
        "question_number": 1,
        "question_data": {
            "text": REFERENCE_A_TEXT,
            "options": {
                "A": "pląsawicy Huntingtona.",
                "B": "chorobie Parkinsona.",
                "C": "chorobie Alzheimera.",
                "D": "chorobie Picka.",
                "E": "encefalopatii Wernickego.",
            },
        },
        "correct_answer": {"option": "A"},
        "answer_statistics": {"A": 84.2, "B": 8.8, "C": 1.8, "D": 3.5, "E": 1.8, "difficulty_index": 0.9},
    }
    assert parsed.issues == []


def test_reference_b_autumn_2026(html_2026: str) -> None:
    parsed = parse_question_page(html_2026, expected_number=1)
    question = parsed.to_scraped(ExamSession.from_code("20262"), 1)
    assert question.question_data.text == REFERENCE_B_TEXT
    assert question.question_data.options == {"A": "1,2.", "B": "1,3.", "C": "2,5.", "D": "3,4.", "E": "2,3."}
    assert question.correct_answer is not None and question.correct_answer.option == "E"
    stats = question.answer_statistics
    assert stats is not None
    assert tuple(Decimal(v) for v in ("40.2", "29.3", "3.0", "3.0", "24.4")) == (
        stats.A,
        stats.B,
        stats.C,
        stats.D,
        stats.E,
    )
    assert stats.difficulty_index == Decimal("0.295")
    assert question.validation_status is ValidationStatus.VALID
    assert parsed.reported_total == 120


def test_trailing_answer_phrase_removed_and_not_used_as_anchor(html_2026: str, html_2003: str) -> None:
    assert "Prawidłowa odpowiedź" not in parse_question_page(html_2026).question_data.text
    # 2003 has no phrase at all and must still parse.
    assert parse_question_page(html_2003).question_data.text == REFERENCE_A_TEXT


def test_extra_statistics_columns_kept_separately(html_2026: str) -> None:
    parsed = parse_question_page(html_2026)
    assert parsed.extra_statistics is not None
    assert set(parsed.extra_statistics) == {"M.D.", "RPBI A", "RPBI B", "RPBI C", "RPBI D", "RPBI E"}
    dumped = parsed.answer_statistics.model_dump(mode="json")  # type: ignore[union-attr]
    assert set(dumped) == {"A", "B", "C", "D", "E", "difficulty_index"}


def test_header_paragraph_not_part_of_question(html_2003: str) -> None:
    assert "Baza pytań" not in parse_question_page(html_2003).question_data.text


# ------------------------------------------------------------------ text handling


def test_whitespace_normalised_and_br_preserved() -> None:
    html = question_page(
        question="  Pierwsza   linia\n\t ciągle ta sama<br>  druga&nbsp;linia  <br><br><br><br>trzecia"
    )
    assert parse_question_page(html).question_data.text == "Pierwsza linia ciągle ta sama\ndruga linia\n\ntrzecia"


def test_html_entities_and_polish_unicode_preserved() -> None:
    html = question_page(
        question="Stężenie &lt;5 mmol/l &amp; ŻÓŁĆ – „cytat” ≥ 2‰?",
        options={"A": "ąćęłńóśźż", "B": "ĄĆĘŁŃÓŚŹŻ", "C": "10&nbsp;mg", "D": "x &gt; y", "E": "μg/kg"},
    )
    parsed = parse_question_page(html)
    assert parsed.question_data.text == "Stężenie <5 mmol/l & ŻÓŁĆ – „cytat” ≥ 2‰?"
    assert parsed.question_data.options["A"] == "ąćęłńóśźż"
    assert parsed.question_data.options["C"] == "10 mg"
    assert parsed.question_data.options["D"] == "x > y"


def test_decode_cp1250_without_meta() -> None:
    raw = question_page(question="Zażółć gęślą jaźń?").replace('<meta charset="utf-8">', "").encode("cp1250")
    assert parse_question_page(decode_html(raw)).question_data.text == "Zażółć gęślą jaźń?"


def test_option_text_with_inline_formatting() -> None:
    html = question_page(options={k: f"opcja <i>{k}</i> z <b>pogrubieniem</b>" for k in "ABCDE"})
    assert parse_question_page(html).question_data.options["C"] == "opcja C z pogrubieniem"


def test_question_without_center_uses_nearest_paragraph() -> None:
    html = question_page(center="")
    parsed = parse_question_page(html, expected_number=1)
    assert parsed.question_data.text == "Treść pytania testowego?"
    assert "question number could not be verified from the page" in parsed.issues


def test_image_in_question_is_flagged_not_dropped() -> None:
    html = question_page(question='Na obrazie MR widać: <img src="img/q17.png" alt="MR">')
    question = parse_question_page(html).to_scraped(ExamSession.from_code("20031"), 1)
    assert question.question_data.attachments is not None
    assert question.question_data.attachments[0].src == "img/q17.png"
    assert question.validation_status is ValidationStatus.NEEDS_REVIEW
    assert "attachments" in question.to_record()["question_data"]


# ------------------------------------------------------------------ question number


def test_wrong_question_number_raises() -> None:
    with pytest.raises(QuestionNumberMismatch) as info:
        parse_question_page(question_page(number=7), expected_number=8)
    assert (info.value.requested, info.value.found) == (8, 7)


def test_number_is_not_confused_with_year() -> None:
    parsed = parse_question_page(question_page(center="Sesja: Jesień 2026 &nbsp; Pytanie nr 17"), expected_number=17)
    assert parsed.detected_number == 17


# ------------------------------------------------------------------ answers & statistics


def test_missing_answer_block_flags_review() -> None:
    question = parse_question_page(question_page(answer=None)).to_scraped(ExamSession.from_code("20031"), 1)
    assert question.correct_answer is None
    assert question.validation_status is ValidationStatus.NEEDS_REVIEW


def test_unreadable_answer_recorded_as_issue() -> None:
    parsed = parse_question_page(question_page(answer="Pytanie unieważnione"))
    assert parsed.correct_answer is None
    assert any("could not read correct answer" in issue for issue in parsed.issues)


def test_statistics_with_comma_decimals_and_percent_sign() -> None:
    rows = [
        [
            "Procent odp. A",
            "Procent odp. B",
            "Procent odp. C",
            "Procent odp. D",
            "Procent odp. E",
            "Wskaźnik trudności",
        ],
        ["10,5%", "59,5%", "10", "10", "10", "0,595"],
    ]
    stats = parse_question_page(question_page(stats_rows=rows)).answer_statistics
    assert stats is not None and Decimal("10.5") == stats.A and stats.difficulty_index == Decimal("0.595")


def test_statistics_headers_matched_by_name_not_position() -> None:
    rows = [
        [
            "Wskaźnik trudności",
            "M.D.",
            "Procent odp. E",
            "Procent odp. D",
            "Procent odp. C",
            "Procent odp. B",
            "Procent odp. A",
        ],
        ["0.5", "0.1", "5", "5", "10", "30", "50"],
    ]
    stats = parse_question_page(question_page(stats_rows=rows)).answer_statistics
    assert stats is not None and (stats.A, stats.E, stats.difficulty_index) == (50, 5, Decimal("0.5"))


def test_vertical_statistics_table() -> None:
    rows = [
        ["Procent odp. A", "20"],
        ["Procent odp. B", "20"],
        ["Procent odp. C", "20"],
        ["Procent odp. D", "20"],
        ["Procent odp. E", "20"],
        ["Wskaźnik trudności", "0.2"],
    ]
    assert parse_question_page(question_page(stats_rows=rows)).answer_statistics is not None


@pytest.mark.parametrize(
    ("rows", "message"),
    [
        (
            [
                [
                    "Procent odp. A",
                    "Procent odp. B",
                    "Procent odp. C",
                    "Procent odp. D",
                    "Procent odp. E",
                    "Wskaźnik trudności",
                ],
                ["abc", "1", "1", "1", "1", "0.1"],
            ],
            "non-numeric",
        ),
        (
            [
                ["Procent odp. A", "Procent odp. B", "Procent odp. C", "Procent odp. D", "Wskaźnik trudności"],
                ["1", "1", "1", "1", "0.1"],
            ],
            "lacks required columns",
        ),
        ([["Kolumna 1", "Kolumna 2"], ["1", "2"]], "headers not recognised"),
        (
            [
                [
                    "Procent odp. A",
                    "Procent odp. B",
                    "Procent odp. C",
                    "Procent odp. D",
                    "Procent odp. E",
                    "Wskaźnik trudności",
                ],
                ["120", "1", "1", "1", "1", "0.1"],
            ],
            "outside 0-100",
        ),
        (
            [
                [
                    "Procent odp. A",
                    "Procent odp. B",
                    "Procent odp. C",
                    "Procent odp. D",
                    "Procent odp. E",
                    "Wskaźnik trudności",
                ],
                ["1", "1", "1"],
            ],
            "value row has",
        ),
    ],
)
def test_malformed_statistics(rows: list[list[str]], message: str) -> None:
    with pytest.raises(MalformedQuestion, match=message):
        parse_question_page(question_page(stats_rows=rows))


def test_missing_statistics_table_flags_review() -> None:
    question = parse_question_page(question_page(stats_rows=[])).to_scraped(ExamSession.from_code("20031"), 1)
    assert question.answer_statistics is None
    assert "answer statistics missing" in question.validation_issues


# ------------------------------------------------------------------ structure


def test_non_standard_option_count_is_kept_for_review() -> None:
    html = question_page(options={"A": "a", "B": "b", "C": "c", "D": "d"}, answer="Prawidłowa odpowiedź to: D")
    question = parse_question_page(html).to_scraped(ExamSession.from_code("20031"), 1)
    assert list(question.question_data.options) == ["A", "B", "C", "D"]
    assert any("non-standard option set" in i for i in question.validation_issues)


def test_empty_option_is_malformed() -> None:
    with pytest.raises(MalformedQuestion, match="empty option"):
        parse_question_page(question_page(options={"A": "a", "B": " ", "C": "c", "D": "d", "E": "e"}))


def test_missing_question_text_is_malformed() -> None:
    html = question_page().replace("<p>Treść pytania testowego?</p>", "")
    with pytest.raises(MalformedQuestion, match="no question paragraph"):
        parse_question_page(html)


def test_paragraphs_in_hidden_containers_ignored() -> None:
    # An "A." label inside the hidden answer block must not be taken as option A.
    html = question_page(answer="<b>A.</b> nie opcja")
    parsed = parse_question_page(html)
    assert parsed.question_data.options["A"] == "opcja a"


# ------------------------------------------------------------------ page classification


def test_eula_and_entry_form_raise_authorization_error() -> None:
    with pytest.raises(AuthorizationPageError) as eula:
        parse_question_page(load(EDGE / "eula.html"))
    assert eula.value.kind is PageKind.EULA
    with pytest.raises(AuthorizationPageError) as form:
        parse_question_page(load(EDGE / "entry_form.html"))
    assert form.value.kind is PageKind.ENTRY_FORM


def test_layout_without_question_is_missing() -> None:
    html = '<div class="marginesy"><div><p>Brak pytania</p></div></div>'
    with pytest.raises(NoQuestionContent):
        parse_question_page(html)


def test_unknown_page() -> None:
    assert classify_page(make_soup("<html><body>Błąd serwera</body></html>")) is PageKind.UNKNOWN
    with pytest.raises(UnexpectedContent):
        parse_question_page("<html><body>Błąd serwera</body></html>")


def test_entry_form_parsing_never_returns_token() -> None:
    form = parse_entry_form(load(EDGE / "entry_form.html"))
    assert form.action == "wyswietl_pytania_pes.php"
    assert form.recaptcha_sitekey == "SYNTHETIC-SITEKEY"
    assert form.session_options["20031"] == "Wiosna 2003"
    assert form.specialization_options["725"] == "Psychiatria"
    assert form.fields["krok"] == "2"
    assert form.fields["sesja"] == "20262"
    assert "g-recaptcha-response" not in form.fields


def test_real_header_format_outside_center() -> None:
    """Header as seen on cem.edu.pl, here rendered without a <center> element."""
    header = "Pytanie nr <b>17</b> - PES <b>Psychiatria</b> w sesji <b>wiosna 2003</b>"
    html = question_page(center="").replace("<p>Nagłówek</p>", f"<p>Nagłówek</p><div>{header}</div>")
    parsed = parse_question_page(html, expected_number=17)
    assert parsed.detected_number == 17 and parsed.issues == []
    assert parse_question_page(question_page(center=f"{header}"), expected_number=17).detected_number == 17


@pytest.mark.parametrize(
    ("header", "code"),
    [
        ("Pytanie nr 1 - PES Psychiatria w sesji wiosna 2003", "20031"),
        ("Pytanie nr 1 - PES Psychiatria w sesji jesień 2026", "20262"),
        ("Pytanie nr 1 - PES Psychiatria w sesji JESIEN 2013", "20132"),
        ("Pytanie nr 1", None),
    ],
)
def test_detect_session_code(header: str, code: str | None) -> None:
    assert detect_session_code(question_page(center=header)) == code


LIVE_HEADERS = [
    "Procent odp. A", "Procent odp. B", "Procent odp. C", "Procent odp. D", "Procent odp. E",
    "Wskaźnik trudności", "M.D.*", "RPBI** A", "RPBI B", "RPBI C", "RPBI D", "RPBI E",
]  # fmt: skip


def test_annulled_question_is_kept_for_review() -> None:
    """Structure seen live (Spring 2003 q48): answer "X", every statistic "-"."""
    html = question_page(answer="Prawidłowa odpowiedź to: X", stats_rows=[LIVE_HEADERS, ["-"] * 12])
    question = parse_question_page(html).to_scraped(ExamSession.from_code("20031"), 48)
    assert question.correct_answer is None and question.answer_statistics is None
    assert question.extra_statistics is None  # dashes are not data
    assert "question annulled by CEM (correct answer marked X)" in question.validation_issues
    assert any("statistics not published by CEM" in i for i in question.validation_issues)
    assert question.validation_status is ValidationStatus.NEEDS_REVIEW


def test_live_extra_headers_with_asterisks() -> None:
    values = ["84.2", "8.8", "1.8", "3.5", "1.8", "0.900", "0.200", "0.107", "-0.088", "0.042", "0.033", "-0.196"]
    parsed = parse_question_page(question_page(stats_rows=[LIVE_HEADERS, values]))
    assert parsed.answer_statistics is not None and parsed.answer_statistics.difficulty_index == Decimal("0.900")
    assert parsed.extra_statistics == dict(zip(LIVE_HEADERS[6:], values[6:], strict=True))


def test_comments_block_is_ignored() -> None:
    html = question_page().replace(
        '<div id="odpowiedz1"',
        '<div id="uwagi1" style="display: none;"><p><b>A.</b> komentarz</p></div><div id="odpowiedz1"',
    )
    assert parse_question_page(html).question_data.options["A"] == "opcja a"


def test_entry_form_includes_named_submit_button_like_a_browser() -> None:
    """Live PES form: hidden typ/konfirm/autoryzacja and <input type=submit name=submit value="Pokaż pytanie">."""
    html = (
        '<form action="wyswietl_pytania_pes.php" method="post"><select name="sesja"><option value="20031">wiosna 2003'
        '</option></select><input type="hidden" name="typ" value="PES"><input type="hidden" name="konfirm" value="">'
        '<div class="g-recaptcha" data-sitekey="k"></div><input type="submit" name="submit" value="Pokaż pytanie">'
        '<input type="hidden" name="autoryzacja" value="1"></form>'
    )
    form = parse_entry_form(html)
    assert form.fields == {
        "sesja": "20031",
        "typ": "PES",
        "konfirm": "",
        "submit": "Pokaż pytanie",
        "autoryzacja": "1",
    }
    unnamed = parse_entry_form(load(EDGE / "entry_form.html"))
    assert "submit" not in unnamed.fields  # an unnamed button sends nothing
