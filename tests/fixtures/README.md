# Test fixtures

**Everything in this directory is SYNTHETIC.** No file here is an authentic
snapshot of cem.edu.pl. The HTML was hand-written from the structure documented
in the project specification (`.marginesy` container, `<p><b>A.</b> ...</p>`
options, hidden `#odpowiedz1` and `#statystyki1 table`). Question texts,
options, answers and statistics of `20031_1` and `20262_1` reproduce the two
reference questions that were verified manually in the browser; the
surrounding markup, the `<center>` metadata wording ("Pytanie nr N") and all
other questions are invented.

- `synthetic/` – pages served by the offline `fixtures` access provider
  (`cem-scraper scrape --dry-run`). Naming: `<session>_<question>.html`.
  Missing files are served as an empty "no question" page.
- `edge/` – deliberately broken or unusual pages for parser tests.

## Supplying authentic snapshots

Run a real scrape with `CEM_SAVE_HTML_DIR=html_snapshots`; every fetched page is
saved after sanitisation (hidden token/captcha inputs blanked). Copy a few into
`tests/fixtures/authentic/` and add expectations to `tests/test_parser.py`.
Check in particular the real `<center>` wording, because question-number
verification relies on it (see `QUESTION_NUMBER_PATTERNS` in `parser.py`).
