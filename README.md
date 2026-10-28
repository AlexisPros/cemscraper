# CEM PES Psychiatry Scraper

A local tool that collects the publicly published PES (Państwowy Egzamin
Specjalizacyjny) **Psychiatria** questions from CEM
(`https://www.cem.edu.pl/pytcem/`) for Spring 2003 – Autumn 2013 (22 sessions)
plus Autumn 2026. Results go into PostgreSQL (JSONB columns), and you can export them to JSON or JSONL.

> **Access status (blocking dependency).** Each examination session is entered
> through CEM's official form, which requires Google reCAPTCHA v2. There's no
> authorized automated mechanism (CEM permission, export or API) at the moment,
> so **fully autonomous live scraping is not implemented as working and has not been demonstrated.** The
> supported live mode is **semi-automated**: you solve one reCAPTCHA per session
> (23 in total) in a browser window that the tool opens, and the tool does everything else.
> The automated path (`http_form`) is fully built. It only proceeds once an
> *authorized* `CaptchaProvider` is plugged in, and until then it stops
> before contacting CEM.

## How it works

```
CLI (main.py)
  └─ Orchestrator ── for each exam session: discover questions 1..N, resume, classify failures
       ├─ SessionManager ── AccessProvider ─┬─ InteractiveBrowserAccessProvider (Playwright; you solve CAPTCHA)
       │                                    ├─ HttpFormAccessProvider (EULA + form; needs authorized CaptchaProvider)
       │                                    └─ FixtureAccessProvider (offline, local HTML)
       ├─ CemClient (httpx.AsyncClient: pooling, rate limit, concurrency cap, retries+jitter,
       │             Retry-After, circuit breaker, metrics)
       ├─ parser (pure: BeautifulSoup+lxml → Pydantic models; works on saved files)
       └─ Repository ─┬─ PostgresRepository (psycopg 3 async pool, migrations, UPSERT)
                      └─ InMemoryRepository (dry runs / tests)
```

| Module | Responsibility |
|---|---|
| `config.py` | `CEM_*` environment settings (pydantic-settings) |
| `models.py` | `ExamSession`, `QuestionData`, `CorrectAnswer`, `AnswerStatistics`, `ScrapedQuestion`, status enums |
| `parser.py` | Question page parsing, page classification, entry-form parsing, charset decoding |
| `client.py` | Async HTTP client and failure classes |
| `captcha.py` | `CaptchaProvider` interface + `DisabledCaptchaProvider` |
| `session_manager.py` | EULA/session entry, access providers, grant lifecycle, re-authorization limit |
| `orchestrator.py` | Discovery, resume, error classification, auth-expiry probing, run bookkeeping |
| `database.py` | Schema migrations, repository (Postgres + in-memory) |
| `exporter.py` | Atomic JSON/JSONL export |
| `metrics.py` | Request/throughput metrics |
| `logging_config.py` | Logging with secret redaction |

### Parsing strategy

This is a port of the JavaScript parser that was verified manually:

1. Take the `<p>` elements in `.marginesy`, excluding anything inside `#odpowiedz*` or `#statystyki*`.
2. Find the paragraph whose direct `<b>` child is `A.`.
3. The question is the nonempty paragraph(s) before it, back to the `<center>` metadata line.
4. Options are the following paragraphs labelled `B.`, `C.`, and so on.
5. `<br>` becomes `\n` and whitespace is normalized. A trailing `Prawidłowa odpowiedź to:` is removed. That phrase is **never** used as an anchor.
6. The answer letter comes from the hidden `#odpowiedz1`. Statistics come from `#statystyki1 table`, matched by **normalized header names**, so extra columns (`M.D.`, `RPBI A–E`) don't matter. Those extra columns are kept separately in `questions.extra_statistics`.
7. Images and tables inside a question are recorded as `attachments`, and the question is flagged `needs_review`. They are never dropped silently.

### Discovery and failure classification

Questions are requested from 1 upward in batches of `CEM_CONCURRENCY`. A session
ends after `CEM_MISSING_STREAK_TO_STOP` consecutive missing questions past the
highest one found. Gaps are recorded as `missing`, and one failure never stops
collection. If a page reports a total ("… z 120"), that total also bounds the search.

Each question gets one persisted status:

| Status | Meaning |
|---|---|
| `stored` | Parsed, validated, and upserted |
| `missing` | The page has the layout but no question, or the entry form came back while the session demonstrably still works |
| `parse_error` | A question is there but couldn't be turned into valid data |
| `number_mismatch` | The page shows a different question number than requested |
| `unexpected_content` | The page wasn't recognizable at all |
| `auth_error` | 401/403 or the EULA/entry form came back. The tool re-requests a known-good question: if that works, the number is `missing`; if not, it re-authorizes (up to the limit) or **stops with `blocked`** |
| `server_error` | Transient failure persisted after retries |

`HTTP 200` alone never counts as success.

## Setup

Requirements: Python 3.12+, [uv](https://docs.astral.sh/uv/), and Docker (for PostgreSQL).

```bash
uv sync                                   # core + dev dependencies
```

```bash
cp .env.example .env                      # then edit
```

```bash
docker compose up -d                      # PostgreSQL 16 on localhost:5432
```

```bash
uv run cem-scraper init-db                # apply migrations (idempotent)
```

For the live (interactive) mode, add the browser extra:

```bash
uv sync --extra browser
```

```bash
uv run playwright install chromium
```

## Usage

Try everything offline first. This run uses the synthetic fixtures, never touches the network, and stores results in memory:

```bash
uv run cem-scraper scrape --dry-run --session 20031 --session 20262
```

Use the same fixtures but write to PostgreSQL:

```bash
uv run cem-scraper scrape --dry-run --db --session 20031
```

Live collection, semi-automated. A browser window opens for each session: read and accept the terms, solve the reCAPTCHA, and submit. The tool then collects that session over HTTP.

```bash
uv run cem-scraper scrape --session 20031
```

Run all 23 sessions. The run resumes by default, and completed sessions are skipped.

```bash
uv run cem-scraper scrape
```

Other commands:

```bash
uv run cem-scraper scrape --retry-failed               # only reprocess failed questions
uv run cem-scraper scrape --refetch --session 20262    # collect a session again
uv run cem-scraper status                              # per-session progress + recent runs
uv run cem-scraper export --format json                # output/pes_psychiatria.json
uv run cem-scraper export --format jsonl --include-validation
uv run cem-scraper validate                            # re-validate stored records
uv run cem-scraper parse page.html --session 20031 --number 1   # offline parse
uv run cem-scraper verify-sessions --html form.html    # check YYYY1/YYYY2 codes against the form
```

Exit codes: `0` completed, `1` error, `2` blocked (access), `3` partial (some
questions failed; rerun to retry them). `Ctrl+C` stops the run cleanly. The run is
marked `interrupted`, and rerunning the same command resumes it.

### Configuration

Every setting is an environment variable with the `CEM_` prefix; see `.env.example`.
The defaults are conservative: **2 concurrent requests, 0.5 requests/s**, a 45 s read
timeout (CEM sometimes takes about 10 s), 4 retries with full-jitter exponential
backoff, `Retry-After` honored up to 300 s (anything longer stops the run), and a
circuit breaker after 5 consecutive failed requests. The validator caps concurrency
at 8 and the rate at 5 req/s.

| Setting | Meaning |
|---|---|
| `CEM_ACCESS_PROVIDER` | `interactive_browser` (default), `http_form`, or `fixtures` |
| `CEM_ACCEPT_EULA` | `false` by default. The tool never accepts the terms on your behalf unless you set this |
| `CEM_BROWSER_CONTEXT_REQUESTS` | `true` sends HTTP requests through the authorized browser context instead of copying its cookies |
| `CEM_SAVE_HTML_DIR` | Saves every fetched page with token-like inputs blanked; useful for building authentic fixtures |
| `CEM_MAX_REAUTHORIZATIONS_PER_SESSION` | How many times an expired session may be re-entered (each costs one reCAPTCHA) |

## Data

Each question exported:

```json
{
  "exam": "PES", "specialization": "Psychiatria", "year": 2003, "season": "spring",
  "session_id": "20031", "question_number": 1,
  "question_data": {"text": "Zanik jąder ogoniastych …", "options": {"A": "pląsawicy Huntingtona.", "B": "…", "C": "…", "D": "…", "E": "…"}},
  "correct_answer": {"option": "A"},
  "answer_statistics": {"A": 84.2, "B": 8.8, "C": 1.8, "D": 3.5, "E": 1.8, "difficulty_index": 0.9}
}
```

Statistics are parsed as `Decimal` (the exact source digits) and written as JSON numbers.

**Schema** (`migrations/0001_initial.sql`):

- `exam_sessions`: one row per (type, specialization, session code), with `collection_status`, `discovered_question_count`, and `last_error`.
- `questions`: `question_data`, `correct_answer`, and `answer_statistics` are **separate JSONB columns**. `UNIQUE (exam_session_id, question_number)`; UPSERTs track a `content_hash` and change `updated_at` only when the content changes. Also holds `validation_status`, `validation_issues`, and `extra_statistics`.
- `question_attempts`: per-question status, attempt count, and last error. Resume and `--retry-failed` are driven from this table.
- `scrape_runs`: start/end, status, totals, retries, and a `diagnostics` JSONB with the full metrics (avg/median/p95 latency, questions/min, per-session outcomes).

No CAPTCHA tokens, cookies, or other authorization state are stored anywhere.

### Validation

These are hard errors, and the question is recorded as `parse_error`:
- empty question text
- options that aren't consecutive from A, or an empty option
- a statistic that isn't numeric or finite
- a percentage outside 0–100
- missing required statistics columns

These are soft issues: the question is stored with `validation_status = needs_review` and the reason:
- an option set other than exactly A–E
- the correct answer is missing or unreadable
- statistics are missing
- percentages sum to more than ±2 away from 100
- `difficulty_index` is outside 0–1
- images or tables in the content
- the question number couldn't be verified

## Tests

```bash
uv run pytest
```

There are 89 tests: parser, models, HTTP client, access management, PostgreSQL, the orchestrator, and the browser provider. They need no network access, no CAPTCHA, and no paid services.

- **PostgreSQL tests** start a throwaway embedded PostgreSQL 16 (`pgserver`). Alternatively, set `CEM_TEST_DATABASE_URL` to a **dedicated** database; its `public` schema gets wiped.
- **Browser tests** run headless Chromium against a local stand-in server, not CEM. They're skipped if Playwright isn't installed.
- **Fixtures** in `tests/fixtures/` are **synthetic** (see its README). The two reference questions (Spring 2003 Q1, Autumn 2026 Q1) reproduce the manually verified content, but the surrounding markup is invented.

## Limitations and open questions

1. **Autonomous access is blocked** (see the top of this page). Removing the block takes CEM authorization or an official export/API. A permitted CAPTCHA integration can then implement `CaptchaProvider` (with `available = True`) and be registered in `build_captcha_provider`. No solving service is included, and none is called.
2. **Not yet verified against live pages.** No authentic HTML was available, so these assumptions need a check against a few saved real pages (`CEM_SAVE_HTML_DIR`):
   - the wording of the `<center>` line that question-number verification relies on (`QUESTION_NUMBER_PATTERNS`); if it differs, questions are stored as `needs_review` ("number could not be verified"), not rejected
   - what CEM returns for numbers past the end of a session
   - whether the session cookie works outside the browser; if it doesn't, set `CEM_BROWSER_CONTEXT_REQUESTS=true`
   - session codes other than 20031 and 20262; run `verify-sessions`
3. **Throttling.** The roughly 9–11 s responses seen in DevTools are unexplained. The tool doesn't try to get around them. It runs at low concurrency and stops when the server asks it to back off.
4. Each session needs its own CAPTCHA, and re-entering an expired session costs another one. Expect about 23 manual solves for a full run.

## Responsible use

Accept the terms of use yourself (`CEM_ACCEPT_EULA` is off by default). Keep
request rates low, and identify yourself with `CEM_CONTACT_EMAIL`. Content that is publicly
viewable is not the same as permission to collect it in bulk; ask CEM if in doubt.
The tool contains no CAPTCHA bypass, token replay, fingerprint evasion, or proxy rotation.
