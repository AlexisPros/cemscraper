# CEM PES Psychiatry Scraper

A local tool that collects the publicly published PES (Państwowy Egzamin
Specjalizacyjny) **Psychiatria** questions from CEM
(`https://www.cem.edu.pl/pytcem/`) for Spring 2003 – Autumn 2013 (22 sessions)
plus Autumn 2026. Results go into PostgreSQL (JSONB columns), and you can export them to JSON or JSONL.

> **Access modes.** Each examination session is entered through CEM's official
> form, which requires Google reCAPTCHA v2. There are two live modes:
>
> - **Your own Chrome (`manual_chrome`, recommended):** you pass the terms and the reCAPTCHA
>   in a regular Google Chrome window that nothing controls during the check. The tool then
>   picks up the session cookie and does everything else. One check per session, usually quick.
> - **Interactive (`interactive_browser`):** the same, but in a Playwright-controlled window.
>   reCAPTCHA recognizes it as automated (`navigator.webdriver = true`) and often asks for many
>   rounds of image challenges.
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

### Your own Chrome (`manual_chrome`)

```bash
# .env
CEM_ACCESS_PROVIDER=manual_chrome
CEM_BROWSER_PROFILE_DIR=browser_profile   # separate, persistent Chrome profile (git-ignored)
```

The tool starts Google Chrome as a normal browser, with that profile and a DevTools port that
only accepts local connections (`CEM_CHROME_DEBUG_PORT`, default 9222), and opens
`eula_pes.php` in a new tab. You accept the terms, choose the session, pass the reCAPTCHA and
click «Pokaż pytanie».

While you do this the tool doesn't attach to the page. It only polls the tab list. When the
tab shows a question page, it reads the session cookie at browser level, checks the session
over HTTP and continues the collection. Because the profile persists, Google's trust in it
grows over time. Nothing is hidden or faked: the check simply happens in a browser a person is
using. The Chrome instance the tool started is closed at the end of the run. Set
`CEM_CHROME_PATH` if Chrome isn't in the standard location.

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
cem-scraper captcha-balance    # free check of key and balance
```

```bash
cem-scraper scrape             # unattended from here
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

Safety rails, each pinned by a test in `tests/test_money_safety.py`:
- **The task type matches the page.** The form's reCAPTCHA is identified by the signs in 2Captcha's docs ([v2](https://2captcha.com/api-docs/recaptcha-v2), [v3](https://2captcha.com/api-docs/recaptcha-v3)). The live CEM form is v2 checkbox (`api.js`, `div.g-recaptcha[data-sitekey]`, iframe `size=normal`), solved as `RecaptchaV2TaskProxyless` with only the required fields. Invisible v2, `recaptcha.net` and `data-s` add `isInvisible`, `apiDomain` and `recaptchaDataSValue`. If the site switches to v3 (`api.js?render=…`) or Enterprise (`enterprise.js`), the run stops **before buying anything**. `cem-scraper verify-sessions` shows the detected type.
- **One task is never paid twice.** If `createTask` may have reached 2Captcha (read timeout, dropped connection, HTTP error), it is not resent. Only a request that never left the machine is retried. Polling and balance calls are free and may retry.
- **A bought token is used exactly once.** The form submission carrying it is never retried, because the token is single-use.
- **The token is bought fresh.** Every check that can fail is done first (EULA consent, session offered by the form, sitekey present), and any server `Retry-After` pause is waited out *before* buying, because a token is valid for about 2 minutes. The token's age at submission is logged.
- **No CAPTCHA when a download can't help.** A rerun skips sessions whose only failures are parse errors, since the same HTML fails the same way (use `reparse`), and sessions where an earlier run found no questions (use `--refetch` to try them again).
- **No re-authorization on a guess.** If the check of a known question fails on the network, the affected numbers are retried in a later run; a new CAPTCHA is bought only when the site really shows the entry form again.
- **Two spending caps.** One per run (`CEM_CAPTCHA_MAX_SPEND`, `CEM_CAPTCHA_MAX_SOLVES`) and one lifetime cap over all runs recorded in the database (`CEM_CAPTCHA_TOTAL_BUDGET`, default 5 USD, at most 25 USD ≈ 100 PLN).
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

Measured on one session (120 questions, 2Captcha entry, CEM answering in ~9 s):

| `CEM_REQUESTS_PER_SECOND` / `CEM_CONCURRENCY` / `CEM_MAX_CONCURRENCY` | Total | Median latency |
|---|---|---|
| 0.5 / 2 / 4 (defaults) | 368 s | 8.8 s |
| 1.0 / 6 / 8 | 226 s | 9.8 s |
| 1.5 / 10 / 14 | 166 s | 11.9 s |

Latency grows with load: CEM starts queueing our requests, so each step gains less.
Raise these in steps and stop when the median latency climbs or requests start failing.
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

### Proxy lanes (with the site operator's permission)

CEM's administrators allowed collecting through several IPs, to measure whether
throughput grows with the number of exit IPs. Each proxy in `CEM_PROXY_URLS` becomes
a *lane*: its own CEM session cookie, its own CAPTCHA, its own rate limiter,
concurrency limit and circuit breaker. `CEM_PROXY_INCLUDE_DIRECT=true` (default)
keeps this machine's IP as a lane too. Sessions go to the least busy lane, up to
`CEM_PIPELINE_DEPTH` per lane, and a re-authorization stays on the lane that
obtained the cookie. The rate limits are **per lane**: with 2 lanes at 1.5 rps,
CEM receives up to 3 requests per second in total.

```
CEM_ACCESS_PROVIDER=http_form
# 2Captcha residential proxies (https://2captcha.com/proxy/residential-proxies): take host:port,
# login and password from the proxy generator. Set "IP duration" to 15-30 min (max 120):
# 0 means a new IP for every request, which breaks the CEM cookie. URL-encode special characters.
CEM_PROXY_URLS=http://LOGIN:PASSWORD@HOST:PORT,http://LOGIN2:PASSWORD2@HOST:PORT
```

Only `http(s)://user:pass@host:port` proxies are accepted, and only with
`http_form` (a browser would authorize from your own IP). Lane names in logs show
`host:port`, never credentials. The CAPTCHA is still solved by 2Captcha's own workers
(`RecaptchaV2TaskProxyless`); the token is submitted from the lane's IP. The run caps
count CAPTCHAs being solved in parallel on several lanes, so they cannot be exceeded.
The report prints requests, failures, median latency and approximate traffic per lane.

**When a proxy breaks, the run keeps going.** A broken lane is *retired*: its sessions
continue on the other lanes, and stored questions are not downloaded again.

| What happens on a proxy lane | Detected as | Reaction |
|---|---|---|
| Wrong login, proxy balance used up, proxy blocks us | `ProxyError` 4xx (e.g. `407`) | retired at once, no retry |
| Proxy unreachable (refused, DNS, connect timeout, 5xx from the proxy) | 3 failures in a row | retired; CEM is not paused or slowed down |
| No successful answer for `CEM_STALL_TIMEOUT` (180 s) | stall | retired (on the direct lane: CEM is down, the run stops) |
| `CEM_PROXY_MAX_MB` (150 MB) sent + received in this run | traffic cap | retired before sending more |

Costs of a failure: a proxy that fails at the EULA page costs no CAPTCHA (the CAPTCHA
is bought only after the EULA and form pages loaded through that proxy). A proxy that
fails during collection costs one more CAPTCHA (~0.003) to re-enter the session on
another lane. A proxy that fails exactly while the solved form is being submitted
wastes that one token. With no working lane left, sessions are reported as blocked and
the run stops; rerunning resumes. The traffic count is an estimate (headers + compressed
bodies as received, without TLS overhead), so keep the cap below what you bought.
After a fail-over during `--refetch`, questions stored before this run are kept, not
fetched again.

Traffic: a question page is ~25 KB of HTML, so one session (120 questions, entry pages)
is ~3-3.5 MB through its lane, and all 23 sessions ~80 MB. 2Captcha counts incoming plus
outgoing traffic; CAPTCHA API calls don't go through the proxy.

Measuring linear growth: run the same number of sessions with 1, 2, 3 ... lanes,
in `--memory`, and compare `q/min` and the per-lane median latency. If the total
grows while each lane's latency stays flat, the limit was per IP; if latency rises
on every lane, CEM's own capacity is the limit and more proxies won't help.

### Discovery and failure classification

Questions are requested from 1 upward through the sliding window. A session
ends after `CEM_MISSING_STREAK_TO_STOP` consecutive missing questions past the
highest one found. Gaps are recorded as `missing`, and one failure never stops
collection. If a page reports a total ("… z 120"), that total also bounds the search.

When the tool sees the official form (`http_form`, `interactive_browser`), the form's own
«Nr pytania» list sets the range: on the live PES form it is 1…120. Nothing past it is
requested, so a session ends at its last question and the next one starts right away. The
value is stored in `exam_sessions.expected_question_count`. `manual_chrome` never sees the
form, so there the end is discovered as described above: the first page past the end is the
EULA page, a known question is re-requested to rule out an expired session, and three
missing numbers in a row end the session.

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

Requirements: Python 3.12+ (3.12 recommended: the embedded PostgreSQL used by the
tests has no builds for 3.13+), [uv](https://docs.astral.sh/uv/) (`brew install uv`)
and Docker (for PostgreSQL).

### Recommended: uv (exact versions from `uv.lock`)

```bash
uv sync --extra browser                   # creates/updates .venv to match uv.lock exactly
```

```bash
uv run playwright install chromium        # only for the interactive browser mode
```

Run commands with `uv run <command>` without activating anything, or activate the
environment in fish with `source .venv/bin/activate.fish` (bash/zsh: `source .venv/bin/activate`).
In VS Code, `.vscode/settings.json` selects `.venv` and activates it in every *new*
integrated terminal. `uv sync` also removes packages that are no longer declared, and
`uv sync --check` verifies the environment matches the lock.

### Without uv: `python -m venv` + pip

```bash
python3.12 -m venv --prompt cem-scraper .venv && source .venv/bin/activate.fish
```

```bash
python -m pip install --upgrade pip && pip install -e ".[browser]" --group dev
```

This produces the same kind of `.venv`, but pip ignores `uv.lock` and installs the newest
versions `pyproject.toml` allows, so versions can drift from the tested ones.

### Database and configuration

```bash
cp .env.example .env                      # then edit
```

```bash
docker compose up -d                      # PostgreSQL 16 on localhost:5432
```

```bash
cem-scraper init-db                       # apply migrations (idempotent)
```

Commands below are written for an activated `.venv`; alternatively prefix them with `uv run`.

## Usage

Try everything offline first. This run uses the synthetic fixtures, never touches the network, and stores results in memory:

```bash
cem-scraper scrape --dry-run --session 20031 --session 20262
```

Use the same fixtures but write to PostgreSQL:

```bash
cem-scraper scrape --dry-run --db --session 20031
```

Live collection, semi-automated. A browser window opens for each session: read and accept the terms, solve the reCAPTCHA, and submit. The tool then collects that session over HTTP.

```bash
cem-scraper scrape --session 20031
```

Run all 23 sessions. The run resumes by default, and completed sessions are skipped.

```bash
cem-scraper scrape
```

Other commands:

```bash
cem-scraper scrape --retry-failed               # only reprocess failed questions
cem-scraper scrape --refetch --session 20262    # collect a session again
cem-scraper status                              # per-session progress + recent runs
cem-scraper export --format json                # output/pes_psychiatria.json
cem-scraper export --format jsonl --include-validation
cem-scraper validate                            # re-validate stored records
cem-scraper parse page.html --session 20031 --number 1   # offline parse
cem-scraper verify-sessions --html form.html    # check YYYY1/YYYY2 codes against the form
cem-scraper reparse                                # re-process saved snapshots (CEM_SAVE_HTML_DIR) offline
cem-scraper telegram-chat-id                    # find your chat id for notifications
cem-scraper notify-test                         # send a test Telegram message
```

### Telegram notifications

A message when a run ends: completed, partial, blocked, failed, or stopped with `Ctrl+C`.
It lists the session outcomes, stored questions, duration, CAPTCHA cost, proxy traffic and
retired lanes, and what went wrong. Optionally, a short message after every examination session.

1. In Telegram, open @BotFather, send `/newbot` and copy the token.
2. Put it in `.env`: `CEM_TELEGRAM_BOT_TOKEN=...`
3. Open your new bot and press **Start** (or send it any message).
4. `cem-scraper telegram-chat-id` prints the line to add: `CEM_TELEGRAM_CHAT_ID=...`
5. `cem-scraper notify-test` sends a test message.
6. Optional: `CEM_TELEGRAM_NOTIFY_SESSIONS=true` for a message after each session.

Notifications are best effort: a failed message is logged as a warning and never stops a
run. `--dry-run` sends nothing (it never touches the network). The token is part of the
Bot API URL, so it is never logged: HTTPX request lines stay hidden, error messages carry only
the error type, and every log line and outgoing text is redacted.

Exit codes: `0` completed, `1` error, `2` blocked (access), `3` partial (some
questions failed; rerun to retry them). `Ctrl+C` stops the run cleanly. The run is
marked `interrupted`, and rerunning the same command resumes it.

### Configuration

Every setting is an environment variable with the `CEM_` prefix; see `.env.example`.
The defaults are conservative: **0.5 requests/s; 2 requests in flight, adaptive up to 4**,
2 sessions in the pipeline, a 45 s read
timeout (CEM sometimes takes about 10 s), 4 retries with full-jitter exponential
backoff, `Retry-After` honored up to 300 s (anything longer stops the run), and a
circuit breaker after 5 consecutive failed requests. A timeout, 429 or 5xx pauses every
request on that lane for `CEM_OVERLOAD_PAUSE` (30 s), and a lane with no successful
response for `CEM_STALL_TIMEOUT` (180 s) stops the run instead of retrying into a
server that has stopped answering. Warnings name the lane (`direct`, `proxy1 ...`). The validator caps concurrency
at 16 and the rate at 5 req/s.

| Setting | Meaning |
|---|---|
| `CEM_ACCESS_PROVIDER` | `manual_chrome` (recommended), `interactive_browser` (default), `http_form`, or `fixtures` |
| `CEM_ADAPTIVE_CONCURRENCY` / `CEM_MAX_CONCURRENCY` | Turn the controller on or off (`false` keeps `CEM_CONCURRENCY` fixed) and set its upper bound |
| `CEM_PIPELINE_DEPTH` | Sessions open at once; `1` means strictly sequential |
| `CEM_ACCEPT_EULA` | `false` by default. The tool never accepts the terms on your behalf unless you set this |
| `CEM_BROWSER_CONTEXT_REQUESTS` | `true` sends HTTP requests through the authorized browser context instead of copying its cookies |
| `CEM_SAVE_HTML_DIR` | Saves every fetched page with token-like inputs blanked; useful for building authentic fixtures |
| `CEM_MAX_REAUTHORIZATIONS_PER_SESSION` | How many times an expired session may be re-entered (each costs one reCAPTCHA) |
| `CEM_TELEGRAM_BOT_TOKEN` / `CEM_TELEGRAM_CHAT_ID` | Telegram notifications; both are needed |
| `CEM_TELEGRAM_NOTIFY_SESSIONS` | Also a message after every examination session |

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
pytest
```

There are 124 tests: parser, models, HTTP client, 2Captcha client, full automation from the EULA page (mocked), traffic control, access management, PostgreSQL, the orchestrator (including the session pipeline), and the browser provider. They need no network access, no CAPTCHA, and no paid services.

- **PostgreSQL tests** start a throwaway embedded PostgreSQL 16 (`pgserver`, Python 3.12 only; on 3.13+ they are skipped unless `CEM_TEST_DATABASE_URL` is set). Alternatively, set `CEM_TEST_DATABASE_URL` to a **dedicated** database; its `public` schema gets wiped.
- **Browser tests** run headless Chromium against a local stand-in server, not CEM. They're skipped if Playwright isn't installed.
- **Fixtures** in `tests/fixtures/` are **synthetic** (see its README). The two reference questions (Spring 2003 Q1, Autumn 2026 Q1) reproduce the manually verified content, but the surrounding markup is invented.

## Limitations and open questions

1. **The automated mode has only been tested against mocks.** The CEM side of the HTTP flow (cookies across EULA → form → submission, extra form fields, plain non-JS submission) mirrors the documented browser observations but hasn't been exercised live. If CEM rejects it, the run stops with `blocked` after one CAPTCHA (`reportIncorrect` asks for a refund), and the interactive mode still works. Other solvers (CapSolver, Anti-Captcha) use the same `createTask`/`getTaskResult` protocol and can be added in `captcha.py`.
2. **Verified on the live site** (interactive run, Spring 2003, 2026-10-28):
   - The entry is `eula_pes.php` → `form_pytania_pes.php` → `wyswietl_pytania_pes.php`. Opening the form directly leads to the *general* form (`eula.php` / `form_pytania.php`, a shorter session list), so the tool always starts at `eula_pes.php` and follows whichever question URL the site uses.
   - The PES form offers 38 sessions (wiosna 2003 … jesień 2026). All 23 targets and specialization 725 are confirmed (`verify-sessions`).
   - The header `Pytanie nr N - PES Psychiatria w sesji wiosna 2003` verifies both the question number and the session.
   - Numbers past the end (121+) return the EULA page. The form lists 1…120, so in `http_form` / `interactive_browser` they are never requested. In `manual_chrome` they are recognized as missing by re-checking a known question.
   - Annulled questions show answer `X` and `-` statistics. They are stored as `needs_review` instead of failing.
   - The session cookie works outside the browser. Question pages take a steady ~11 s; the adaptive limit rose to 4, giving about 6 minutes per 120-question session.
   - Saved snapshots can be reprocessed offline after a parser fix with `cem-scraper reparse`.
3. **Throttling.** The roughly 9–11 s responses seen in DevTools are unexplained. The tool doesn't try to get around them. It runs at low concurrency and stops when the server asks it to back off.
4. Each session needs its own CAPTCHA, and re-entering an expired session costs another one. Expect about 23 manual solves for a full run.

## Responsible use

Accept the terms of use yourself (`CEM_ACCEPT_EULA` is off by default). Keep
request rates low, and identify yourself with `CEM_CONTACT_EMAIL`. Content that is publicly
viewable is not the same as permission to collect it in bulk; ask CEM if in doubt.
CAPTCHA solving through 2Captcha is opt-in, capped, and your decision with respect
to CEM's terms. The tool contains no token replay or fingerprint evasion. Proxy lanes exist because
CEM's administrators allowed them; don't use them on a site that didn't.
