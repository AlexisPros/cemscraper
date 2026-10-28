# CEM PES Psychiatry Scraper

A local tool that collects the publicly published PES (Państwowy Egzamin
Specjalizacyjny) **Psychiatria** questions from CEM
(`https://www.cem.edu.pl/pytcem/`) for Spring 2003 – Autumn 2013 (22 sessions)
plus Autumn 2026. Results go into PostgreSQL (JSONB columns), and you can export them to JSON or JSONL.

> **Access modes.** Each examination session is entered through CEM's official
> form, which requires Google reCAPTCHA v2. There are two live modes:
>
> - **Interactive (default):** you solve one reCAPTCHA per session (23 in total) in a
>   browser window that the tool opens, and the tool does everything else.
> - **Fully automated (opt-in, paid):** `http_form` + 2Captcha. The tool goes from the EULA
>   page to the last question with no human involved. It is tested end to end against
>   mocked CEM and 2Captcha services, but **it has not yet been run against the live site**.
>   Whether CEM's terms allow a solving service is your decision.

## How it works

```
CLI (main.py)
  └─ Orchestrator ── session pipeline; per session: discover questions 1..N, resume, classify failures
       ├─ SessionManager ── AccessProvider ─┬─ InteractiveBrowserAccessProvider (Playwright; you solve CAPTCHA)
       │   (one authorization at a time)    ├─ HttpFormAccessProvider (EULA + form; needs authorized CaptchaProvider)
       │                                    └─ FixtureAccessProvider (offline, local HTML)
       ├─ CemClient (httpx.AsyncClient: pooling, retries+jitter, Retry-After, metrics)
       │    └─ TrafficControl, shared by all clients: rate limit, adaptive concurrency, circuit breaker
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
| `throttle.py` | Shared rate limiter, adaptive concurrency controller, circuit breaker |
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

### Fully automated mode (2Captcha)

```bash
# .env
CEM_ACCESS_PROVIDER=http_form
CEM_CAPTCHA_PROVIDER=2captcha
CEM_TWOCAPTCHA_API_KEY=your-key       # from https://2captcha.com/enterpage; never logged
CEM_ACCEPT_EULA=true                  # your explicit acceptance of CEM's terms
CEM_CAPTCHA_MAX_SPEND=1.0             # per-run cap, account currency (USD)
CEM_CAPTCHA_MAX_SOLVES=40             # per-run cap
```

```bash
uv run cem-scraper captcha-balance    # free check of key and balance
```

```bash
uv run cem-scraper scrape             # unattended from here
```

Per session the flow is:
1. `GET eula_pes.php`, then `POST form_pytania_pes.php` with `akceptacja=tak`.
2. Read the session form and its `data-sitekey`.
3. 2Captcha `createTask` (`RecaptchaV2TaskProxyless`), then poll `getTaskResult` every 5 s.
4. Submit the official form with the token. The response is question 1.
5. Tell 2Captcha whether CEM accepted the token: `reportCorrect`, or `reportIncorrect` (refund).

A token is used once and never stored or logged.

With `CEM_PIPELINE_DEPTH=2`, the next session's CAPTCHA is solved while the current
session is collected, so its 20–60 s solve time is hidden. Price: 4.49–13.49 PLN per
1000 reCAPTCHA v2, so a full run (~23 sessions) costs well under 1 PLN.

Safety rails:
- the balance is checked once before the first task;
- an empty balance, a wrong key or a blocked account stops the run, and nothing is ever topped up automatically;
- unsolvable tasks are refunded by 2Captcha and retried up to 3 times;
- when a per-run cap is reached, the run stops with `blocked`;
- the run report and `scrape_runs.diagnostics` record `captcha_solves` and `captcha_cost`.

### Throughput: adaptive concurrency and the session pipeline

CEM answers either fast (~0.2 s) or very slowly (~10 s, waiting for the first byte).
The **request rate** is always capped by `CEM_REQUESTS_PER_SECOND` (default 0.5/s).
What adapts is how many requests may *wait for an answer* at once. By Little's law,
sustaining 0.5 req/s at 10 s latency needs about 5 in flight, while a fixed 2 would
drop to 0.2 req/s. The controller (`throttle.py`) works per window of completed requests:

- it raises the limit by 1 toward `ceil(rate × median latency)`, capped at `CEM_MAX_CONCURRENCY` (4);
- it undoes the increase if the median latency then grows by more than `CEM_LATENCY_TOLERANCE` (1.5×), because the server is queueing because of us;
- it halves the limit once per burst of timeouts, 429s or 5xx responses, then pauses increases;
- it lowers the limit when fewer requests in flight are enough.

Changes are logged and stored in the run's `diagnostics.concurrency_changes`.
Questions are dispatched through a sliding window, so one slow answer doesn't hold
back the others.

**Session pipeline** (`CEM_PIPELINE_DEPTH`, default 2): while one session is being
collected, the next one is already being authorized. In interactive mode you solve its
CAPTCHA while the tool keeps working, and the grant is used at once, so it can't
expire while idle. Authorizations stay strictly one at a time. All sessions share one
rate limiter, one concurrency limit and one circuit breaker, so the server sees the
same load as with one session.

If a session is blocked, no further CAPTCHAs are requested. If the server fails, every
open session stops. In a 10×-compressed simulation (2 sessions × 30 questions, 10 s
latency, 30 s per CAPTCHA), collection took 3.9 min instead of 6.8 min (1.8×) with the
same request-rate cap.

### Discovery and failure classification

Questions are requested from 1 upward through the sliding window. A session
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

`uv sync` creates `.venv/` (not `venv/`). Use `uv run <command>` without activating it, or activate it in fish with `source .venv/bin/activate.fish`. In VS Code, `.vscode/settings.json` selects `.venv` and activates it in every *new* integrated terminal.

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
The defaults are conservative: **0.5 requests/s; 2 requests in flight, adaptive up to 4**,
2 sessions in the pipeline, a 45 s read
timeout (CEM sometimes takes about 10 s), 4 retries with full-jitter exponential
backoff, `Retry-After` honored up to 300 s (anything longer stops the run), and a
circuit breaker after 5 consecutive failed requests. The validator caps concurrency
at 8 and the rate at 5 req/s.

| Setting | Meaning |
|---|---|
| `CEM_ACCESS_PROVIDER` | `interactive_browser` (default), `http_form`, or `fixtures` |
| `CEM_ADAPTIVE_CONCURRENCY` / `CEM_MAX_CONCURRENCY` | Turn the controller on or off (`false` keeps `CEM_CONCURRENCY` fixed) and set its upper bound |
| `CEM_PIPELINE_DEPTH` | Sessions open at once; `1` means strictly sequential |
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

There are 124 tests: parser, models, HTTP client, 2Captcha client, full automation from the EULA page (mocked), traffic control, access management, PostgreSQL, the orchestrator (including the session pipeline), and the browser provider. They need no network access, no CAPTCHA, and no paid services.

- **PostgreSQL tests** start a throwaway embedded PostgreSQL 16 (`pgserver`). Alternatively, set `CEM_TEST_DATABASE_URL` to a **dedicated** database; its `public` schema gets wiped.
- **Browser tests** run headless Chromium against a local stand-in server, not CEM. They're skipped if Playwright isn't installed.
- **Fixtures** in `tests/fixtures/` are **synthetic** (see its README). The two reference questions (Spring 2003 Q1, Autumn 2026 Q1) reproduce the manually verified content, but the surrounding markup is invented.

## Limitations and open questions

1. **The automated mode has only been tested against mocks.** The CEM side of the HTTP flow (cookies across EULA → form → submission, extra form fields, plain non-JS submission) mirrors the documented browser observations but hasn't been exercised live. If CEM rejects it, the run stops with `blocked` after one CAPTCHA (`reportIncorrect` asks for a refund), and the interactive mode still works. Other solvers (CapSolver, Anti-Captcha) use the same `createTask`/`getTaskResult` protocol and can be added in `captcha.py`.
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
CAPTCHA solving through 2Captcha is opt-in, capped, and your decision with respect
to CEM's terms. The tool contains no token replay, fingerprint evasion, or proxy rotation.
