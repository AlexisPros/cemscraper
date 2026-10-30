"""Notifications when a run (and optionally each examination session) ends.

Telegram only for now: a bot of your own (from @BotFather) writes to your chat.
Notifications are best effort: a failed message is logged and never stops or
slows down a run. The bot token sits in the request path, so it is never logged;
every outgoing text goes through the same redaction as the logs.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

import httpx
from pydantic import SecretStr

from cem_scraper.config import Settings
from cem_scraper.logging_config import redact
from cem_scraper.models import RunStatus

if TYPE_CHECKING:
    from cem_scraper.orchestrator import SessionReport

log = logging.getLogger(__name__)

TELEGRAM_TEXT_LIMIT = 4096
STATUS_ICON = {
    RunStatus.COMPLETED: "✅",
    RunStatus.PARTIAL: "⚠️",
    RunStatus.BLOCKED: "⛔",
    RunStatus.FAILED: "❌",
    RunStatus.INTERRUPTED: "⏹",
    RunStatus.RUNNING: "▶️",
}


class Notifier:
    """No-op notifier (nothing configured)."""

    enabled = False

    async def send(self, text: str) -> bool:
        return False

    async def close(self) -> None:
        pass


class TelegramNotifier(Notifier):
    """Telegram Bot API ``sendMessage`` (https://core.telegram.org/bots/api#sendmessage)."""

    enabled = True

    def __init__(
        self,
        token: SecretStr,
        chat_id: str,
        *,
        base_url: str = "https://api.telegram.org",
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        attempts: int = 3,
    ) -> None:
        self._token = token
        self.chat_id = chat_id
        self._sleep = sleep
        self.attempts = attempts
        self._http = httpx.AsyncClient(base_url=base_url, transport=transport, timeout=10.0)

    def __repr__(self) -> str:
        return f"TelegramNotifier(chat_id={self.chat_id!r})"

    async def close(self) -> None:
        await self._http.aclose()

    async def _call(self, method: str, payload: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """One Bot API call with a few retries; returns (result, error). Never raises."""
        error = "no attempt"
        for attempt in range(self.attempts):
            try:
                response = await self._http.post(f"/bot{self._token.get_secret_value()}/{method}", json=payload)
            except httpx.HTTPError as exc:  # the message would contain the URL, i.e. the token
                error = type(exc).__name__
            else:
                try:
                    data: dict[str, Any] = response.json()
                except ValueError:
                    data = {}
                if response.status_code == 200 and data.get("ok"):
                    return data, ""
                error = f"HTTP {response.status_code}: {str(data.get('description', ''))[:120]}"
                if response.status_code == 429:  # flood control: Telegram says how long to wait
                    wait = float((data.get("parameters") or {}).get("retry_after", 5))
                    if attempt + 1 < self.attempts:
                        await self._sleep(min(wait, 30.0))
                    continue
                if 400 <= response.status_code < 500:
                    break  # wrong token or chat id: retrying cannot help
            if attempt + 1 < self.attempts:
                await self._sleep(2.0 * (attempt + 1))
        return None, error

    async def send(self, text: str) -> bool:
        text = redact(text)
        if len(text) > TELEGRAM_TEXT_LIMIT:
            text = text[: TELEGRAM_TEXT_LIMIT - 2] + " …"
        payload = {"chat_id": self.chat_id, "text": text, "disable_web_page_preview": True}
        result, error = await self._call("sendMessage", payload)
        if result is None:
            log.warning("Telegram notification not sent: %s", error)
            return False
        return True

    async def chats(self) -> tuple[list[tuple[str, str]], str]:
        """Chats that recently wrote to the bot (``getUpdates``): [(chat_id, name)], error."""
        result, error = await self._call("getUpdates", {"allowed_updates": ["message"]})
        if result is None:
            return [], error
        seen: dict[str, str] = {}
        for update in result.get("result", []):
            chat = (update.get("message") or {}).get("chat") or {}
            if "id" in chat:
                name = chat.get("title") or chat.get("username") or chat.get("first_name") or ""
                seen[str(chat["id"])] = str(name)
        return list(seen.items()), ""


def build_notifier(settings: Settings, *, require_chat: bool = True) -> Notifier:
    token = settings.telegram_bot_token
    if token is None or not token.get_secret_value():
        return Notifier()
    if require_chat and not settings.telegram_chat_id:
        log.warning("CEM_TELEGRAM_BOT_TOKEN is set but CEM_TELEGRAM_CHAT_ID is not; no notifications")
        return Notifier()
    return TelegramNotifier(token, settings.telegram_chat_id or "", base_url=settings.telegram_base_url)


# --------------------------------------------------------------------------- messages


def _duration(seconds: float) -> str:
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m" if hours else f"{minutes}m {secs:02d}s"


def run_message(status: RunStatus, sessions: list[SessionReport], metrics: dict[str, Any], *, note: str = "") -> str:
    """Summary of a finished (or interrupted) run."""
    lines = [f"{STATUS_ICON.get(status, '')} cem-scraper: run {status.value}".strip()]
    if note:
        lines.append(note)
    if sessions:
        counts: dict[str, int] = {}
        for report in sessions:
            counts[report.outcome.value] = counts.get(report.outcome.value, 0) + 1
        lines.append("Sessions: " + ", ".join(f"{n} {outcome}" for outcome, n in counts.items()))
    if metrics:  # empty when the run was refused before it started
        stored = metrics.get("questions_succeeded", 0)
        lines.append(
            f"Stored: {stored} questions in {_duration(float(metrics.get('total_duration_s') or 0))}"
            + (f" ({metrics['questions_per_minute']} q/min)" if metrics.get("questions_per_minute") else "")
        )
    if metrics.get("requests"):
        lines.append(
            f"Requests: {metrics.get('request_successes', 0)} ok, {metrics.get('request_failures', 0)} failed, "
            f"median {metrics.get('duration_median_s')}s"
        )
    if metrics.get("captcha_solves"):
        lines.append(f"CAPTCHA: {metrics['captcha_solves']} solved, cost {float(metrics['captcha_cost']):.4f}")
    for name, lane in (metrics.get("lanes") or {}).items():
        if name != "direct" or lane.get("disabled"):
            line = f"Lane {name}: ~{lane.get('megabytes', 0)} MB"
            if lane.get("disabled"):
                line += f", disabled: {str(lane['disabled'])[:150]}"
            lines.append(line)
    problems = [r for r in sessions if r.outcome.value not in ("completed", "skipped")]
    for report in problems[:10]:
        detail = f" - {str(report.error)[:200]}" if report.error else ""
        failed = f" ({len(report.failed)} failed)" if report.failed else ""
        lines.append(f"• {report.session_code} {report.outcome.value}{failed}{detail}")
    if len(problems) > 10:
        lines.append(f"• … and {len(problems) - 10} more")
    if status in (RunStatus.PARTIAL, RunStatus.BLOCKED, RunStatus.FAILED, RunStatus.INTERRUPTED):
        lines.append("Progress is saved; rerun the same command to resume.")
    return "\n".join(lines)


def session_message(report: SessionReport, label: str, done: int, total: int) -> str:
    """One examination session finished."""
    icon = "✅" if report.outcome.value == "completed" else "⚠️"
    text = f"{icon} {label}: {report.outcome.value}, {report.stored} stored ({done}/{total} sessions)"
    if report.failed:
        text += f", {len(report.failed)} failed"
    if report.error:
        text += f"\n{str(report.error)[:200]}"
    return text
