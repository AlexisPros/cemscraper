"""Telegram notifications against a mocked Bot API (never the real service, never a real token)."""

from __future__ import annotations

import asyncio
import json
import logging

import httpx
import pytest
import respx
from pydantic import SecretStr

from cem_scraper import main as cli
from cem_scraper.config import Settings
from cem_scraper.logging_config import redact
from cem_scraper.metrics import Metrics
from cem_scraper.models import RunStatus
from cem_scraper.notify import TelegramNotifier, build_notifier, run_message, session_message
from cem_scraper.orchestrator import Orchestrator, SessionOutcome, SessionReport
from cem_scraper.session_manager import SessionManager
from tests.conftest import SYNTHETIC
from tests.test_orchestrator import ScriptedProvider, default_responder

TOKEN = "1234567890:AAHsyntheticTokenForTestsOnly_0123456"
API = "https://api.telegram.org"
SEND = f"{API}/bot{TOKEN}/sendMessage"


class FakeSleep:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def notifier(sleep: FakeSleep | None = None) -> TelegramNotifier:
    return TelegramNotifier(SecretStr(TOKEN), "42", sleep=sleep or FakeSleep())


def sent_text(route: respx.Route, index: int = 0) -> str:
    return json.loads(route.calls[index].request.content)["text"]


@respx.mock
async def test_send_posts_to_the_chat() -> None:
    route = respx.post(SEND).mock(return_value=httpx.Response(200, json={"ok": True, "result": {}}))
    n = notifier()
    assert await n.send("hello") is True
    await n.close()
    assert json.loads(route.calls[0].request.content) == {
        "chat_id": "42",
        "text": "hello",
        "disable_web_page_preview": True,
    }


@respx.mock
async def test_outgoing_text_is_redacted_and_fits_telegram() -> None:
    route = respx.post(SEND).mock(return_value=httpx.Response(200, json={"ok": True}))
    n = notifier()
    await n.send("db postgresql://cem:hunter2@localhost/cem down")
    await n.send("x" * 5000)
    assert "hunter2" not in sent_text(route, 0)
    assert len(sent_text(route, 1)) <= 4096


@respx.mock
async def test_flood_control_waits_as_told() -> None:
    respx.post(SEND).mock(
        side_effect=[
            httpx.Response(
                429, json={"ok": False, "description": "Too Many Requests", "parameters": {"retry_after": 7}}
            ),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    sleep = FakeSleep()
    assert await notifier(sleep).send("hi") is True
    assert sleep.calls == [7.0]


@respx.mock
async def test_wrong_token_is_not_retried_and_never_logged(caplog: pytest.LogCaptureFixture) -> None:
    route = respx.post(SEND).mock(
        return_value=httpx.Response(401, json={"ok": False, "error_code": 401, "description": "Unauthorized"})
    )
    with caplog.at_level(logging.DEBUG):
        assert await notifier().send("hi") is False
    assert route.call_count == 1
    ours = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("cem_scraper"))
    assert "HTTP 401: Unauthorized" in ours and TOKEN not in ours


def test_production_logging_never_prints_the_token(capsys: pytest.CaptureFixture[str]) -> None:
    """httpx logs full request URLs (with the bot token) at INFO; the CLI's logging must hide them."""
    from cem_scraper.logging_config import configure_logging

    root, httpx_logger = logging.getLogger(), logging.getLogger("httpx")
    saved = (root.handlers[:], root.level, httpx_logger.level)
    try:
        configure_logging("DEBUG")
        httpx_logger.info('HTTP Request: POST %s "HTTP/1.1 401 Unauthorized"', SEND)  # suppressed by level
        httpx_logger.warning("request to %s failed", SEND)  # shown, but redacted
        logging.getLogger("cem_scraper.notify").warning("token %s", TOKEN)
    finally:
        root.handlers[:] = saved[0]
        root.setLevel(saved[1])
        httpx_logger.setLevel(saved[2])
    err = capsys.readouterr().err
    assert "HTTP Request" not in err
    assert "request to https://api.telegram.org/bot***/sendMessage failed" in err
    assert TOKEN not in err


@respx.mock
async def test_network_errors_are_retried_then_given_up_quietly(caplog: pytest.LogCaptureFixture) -> None:
    route = respx.post(SEND).mock(side_effect=httpx.ConnectError(f"cannot reach {SEND}"))
    with caplog.at_level(logging.DEBUG):
        assert await notifier().send("hi") is False  # never raises
    assert route.call_count == 3
    ours = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("cem_scraper"))
    assert TOKEN not in ours and "ConnectError" in ours


def test_token_is_redacted_alone_and_in_urls() -> None:
    assert TOKEN not in redact(f"GET {SEND} failed")
    assert redact(f"token {TOKEN} leaked") == "token *** leaked"
    assert TOKEN not in repr(notifier())


@respx.mock
async def test_chat_ids_from_recent_messages() -> None:
    respx.post(f"{API}/bot{TOKEN}/getUpdates").mock(
        return_value=httpx.Response(
            200,
            json={
                "ok": True,
                "result": [
                    {"update_id": 1, "message": {"chat": {"id": 555, "first_name": "Aleks", "type": "private"}}},
                    {"update_id": 2, "message": {"chat": {"id": 555, "first_name": "Aleks", "type": "private"}}},
                ],
            },
        )
    )
    chats, error = await notifier().chats()
    assert (chats, error) == ([("555", "Aleks")], "")


def test_notifier_needs_token_and_chat(caplog: pytest.LogCaptureFixture) -> None:
    assert not build_notifier(Settings(_env_file=None)).enabled  # type: ignore[call-arg]
    only_token = Settings(_env_file=None, telegram_bot_token=TOKEN)  # type: ignore[call-arg]
    assert not build_notifier(only_token).enabled
    assert "CEM_TELEGRAM_CHAT_ID is not" in caplog.text
    assert isinstance(build_notifier(only_token, require_chat=False), TelegramNotifier)
    both = Settings(_env_file=None, telegram_bot_token=TOKEN, telegram_chat_id="42")  # type: ignore[call-arg]
    assert build_notifier(both).enabled
    assert TOKEN not in repr(both)


def test_run_message_summarizes_what_matters() -> None:
    sessions = [
        SessionReport("20031", SessionOutcome.COMPLETED, 120, 120),
        SessionReport("20032", SessionOutcome.SKIPPED, 120, 120),
        SessionReport("20041", SessionOutcome.BLOCKED, error="no working lane left: proxy1: 407"),
    ]
    metrics = {
        "questions_succeeded": 120,
        "total_duration_s": 3725.0,
        "questions_per_minute": 24.1,
        "requests": 122,
        "request_successes": 122,
        "request_failures": 0,
        "duration_median_s": 8.9,
        "captcha_solves": 2,
        "captcha_cost": 0.006,
        "lanes": {
            "direct": {"megabytes": 3.1, "disabled": None},
            "proxy1 (eu.proxy.2captcha.com:2334)": {"megabytes": 0.01, "disabled": "proxy refused: 407"},
        },
    }
    text = run_message(RunStatus.BLOCKED, sessions, metrics)
    assert text.startswith("⛔ cem-scraper: run blocked")
    assert "Sessions: 1 completed, 1 skipped, 1 blocked" in text
    assert "Stored: 120 questions in 1h 02m (24.1 q/min)" in text
    assert "CAPTCHA: 2 solved, cost 0.0060" in text
    assert "Lane proxy1 (eu.proxy.2captcha.com:2334): ~0.01 MB, disabled: proxy refused: 407" in text
    assert "Lane direct" not in text  # nothing to say about a healthy direct lane
    assert "• 20041 blocked - no working lane left" in text
    assert "rerun the same command to resume" in text
    assert session_message(sessions[0], "Spring 2003 (20031)", 1, 23) == (
        "✅ Spring 2003 (20031): completed, 120 stored (1/23 sessions)"
    )


async def test_session_callback_only_for_sessions_worked_on(settings: Settings) -> None:
    fast = settings.model_copy(update={"requests_per_second": 10_000, "missing_streak_to_stop": 3})
    seen: list[tuple[str, str, int, int]] = []

    async def done(session, report, finished, total) -> None:  # type: ignore[no-untyped-def]
        seen.append((session.session_code, report.outcome.value, finished, total))
        raise RuntimeError("Telegram is down")  # must not break collection

    metrics = Metrics()
    from cem_scraper.database import InMemoryRepository

    repo = InMemoryRepository()
    provider = ScriptedProvider(default_responder(3))
    first = Orchestrator(fast, repo, SessionManager(fast, provider, metrics), metrics, on_session_done=done)
    report = await first.run(["20031"])
    assert report.status is RunStatus.COMPLETED
    second = Orchestrator(fast, repo, SessionManager(fast, provider, metrics), metrics, on_session_done=done)
    await second.run(["20031", "20262"])
    assert seen == [("20031", "completed", 1, 1), ("20262", "completed", 2, 2)]  # the skipped 20031: no message


# --------------------------------------------------------------------------- the scrape command


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.chdir(tmp_path)  # never read the developer's real .env
    for name, value in {
        "CEM_TELEGRAM_BOT_TOKEN": TOKEN,
        "CEM_TELEGRAM_CHAT_ID": "42",
        "CEM_FIXTURES_DIR": str(SYNTHETIC),
        "CEM_OUTPUT_DIR": str(tmp_path / "out"),
        "CEM_REQUESTS_PER_SECOND": "5",
        "CEM_MAX_QUESTION_NUMBER": "10",
    }.items():
        monkeypatch.setenv(name, value)


async def test_scrape_sends_a_summary_when_the_run_ends(cli_env: None, respx_mock: respx.MockRouter) -> None:
    route = respx_mock.post(SEND).mock(return_value=httpx.Response(200, json={"ok": True}))
    args = cli.build_parser().parse_args(["scrape", "--memory", "--provider", "fixtures", "--session", "20031"])
    assert await cli.cmd_scrape(args) == cli.EXIT_OK
    assert route.call_count == 1
    assert sent_text(route).startswith("✅ cem-scraper: run completed")


@pytest.mark.respx(assert_all_called=False)
async def test_dry_run_sends_nothing(cli_env: None, respx_mock: respx.MockRouter) -> None:
    route = respx_mock.post(SEND).mock(return_value=httpx.Response(200, json={"ok": True}))
    args = cli.build_parser().parse_args(["scrape", "--dry-run", "--session", "20031"])
    await cli.cmd_scrape(args)
    assert route.call_count == 0  # --dry-run promises no network at all


async def test_ctrl_c_still_sends_a_message(
    cli_env: None, monkeypatch: pytest.MonkeyPatch, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.post(SEND).mock(return_value=httpx.Response(200, json={"ok": True}))

    async def interrupted(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise asyncio.CancelledError

    monkeypatch.setattr(Orchestrator, "run", interrupted)
    args = cli.build_parser().parse_args(["scrape", "--memory", "--provider", "fixtures", "--session", "20031"])
    with pytest.raises(asyncio.CancelledError):
        await cli.cmd_scrape(args)
    text = sent_text(route)
    assert text.startswith("⏹ cem-scraper: run interrupted") and "Stopped with Ctrl+C." in text


async def test_notify_test_command(
    cli_env: None, capsys: pytest.CaptureFixture[str], respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.post(SEND).mock(return_value=httpx.Response(200, json={"ok": True}))
    assert await cli.cmd_notify_test(cli.build_parser().parse_args(["notify-test"])) == cli.EXIT_OK
    assert "test notification" in sent_text(route)
    assert TOKEN not in capsys.readouterr().out
