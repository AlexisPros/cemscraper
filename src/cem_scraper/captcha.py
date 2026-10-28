"""CAPTCHA providers.

CAPTCHA verification is a separate access-management stage, used only by the
automated ``HttpFormAccessProvider``. Providers:

- ``DisabledCaptchaProvider`` (default): automated entry is off; use the
  interactive browser and solve the reCAPTCHA yourself.
- ``TwoCaptchaProvider``: the paid 2Captcha service (API v2,
  ``RecaptchaV2TaskProxyless``). Opt-in: needs ``CEM_CAPTCHA_PROVIDER=2captcha``
  and your API key. Whether CEM's terms allow it is the operator's decision.

Rules every implementation follows:
- a token is used once, immediately, for the form submission it was requested
  for; never cached, replayed, logged or persisted;
- nothing is bought automatically: an empty balance stops the run, and spending
  per run is capped (``CEM_CAPTCHA_MAX_SPEND``, ``CEM_CAPTCHA_MAX_SOLVES``);
- failures raise ``CaptchaUnavailable``; the session is then reported as blocked.
"""

from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
from pydantic import SecretStr

from cem_scraper.config import CaptchaProviderName, Settings
from cem_scraper.metrics import Metrics

log = logging.getLogger(__name__)


class CaptchaUnavailable(Exception):
    """No CAPTCHA token can be obtained (not configured, budget reached, account or service problem)."""


# Google: a reCAPTCHA response token is valid for two minutes.
TOKEN_LIFETIME_S = 120.0


@dataclass
class CaptchaSolution:
    token: str = field(repr=False)
    task_id: str
    cost: Decimal = Decimal(0)
    solved_at: float = field(default_factory=time.monotonic)

    def age(self) -> float:
        return time.monotonic() - self.solved_at


class CaptchaProvider(ABC):
    name: str

    @property
    @abstractmethod
    def available(self) -> bool:
        """True when the provider is configured and still within its budget."""

    @abstractmethod
    async def solve(self, *, site_key: str, page_url: str) -> CaptchaSolution:
        """Return a one-time verification token for ``page_url``."""

    async def report(self, solution: CaptchaSolution, *, accepted: bool) -> None:  # noqa: B027
        """Tell the provider whether the site accepted the token (optional)."""

    async def balance(self) -> Decimal | None:
        return None

    async def close(self) -> None:  # noqa: B027
        """Release network resources."""


class DisabledCaptchaProvider(CaptchaProvider):
    name = "disabled"

    @property
    def available(self) -> bool:
        return False

    async def solve(self, *, site_key: str, page_url: str) -> CaptchaSolution:
        raise CaptchaUnavailable(
            "no CAPTCHA provider is configured: set CEM_CAPTCHA_PROVIDER=2captcha and CEM_TWOCAPTCHA_API_KEY, "
            "or use CEM_ACCESS_PROVIDER=interactive_browser and solve the reCAPTCHA yourself"
        )


class TwoCaptchaApiError(CaptchaUnavailable):
    def __init__(self, code: str, description: str = "") -> None:
        super().__init__(f"2Captcha: {code}{f' ({description})' if description else ''}")
        self.code = code


class TwoCaptchaProvider(CaptchaProvider):
    """2Captcha API v2 (https://2captcha.com/api-docs/recaptcha-v2).

    createTask(RecaptchaV2TaskProxyless) -> poll getTaskResult every few seconds ->
    ``solution.gRecaptchaResponse``. Unsolvable tasks are refunded by 2Captcha and
    retried with a new task; "no slot" is retried after 5 s; account problems
    (wrong key, zero balance, blocked IP) stop immediately.
    """

    name = "2captcha"
    # Errors that a new task can fix; everything else needs the operator.
    RETRY_NEW_TASK = frozenset({"ERROR_CAPTCHA_UNSOLVABLE", "ERROR_NO_SLOT_AVAILABLE"})
    # Charged when 2Captcha does not report a price: keeps the budget conservative.
    FALLBACK_COST = Decimal("0.003")

    def __init__(
        self,
        api_key: SecretStr | None,
        *,
        base_url: str = "https://api.2captcha.com",
        max_spend: Decimal = Decimal("1.0"),
        max_solves: int = 40,
        timeout: float = 180.0,
        poll_interval: float = 5.0,
        max_tasks_per_solve: int = 3,
        metrics: Metrics | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._key = api_key
        self.max_spend = max_spend
        self.max_solves = max_solves
        self.timeout = timeout
        self.poll_interval = poll_interval
        self.max_tasks_per_solve = max_tasks_per_solve
        self.metrics = metrics
        self._sleep = sleep
        self._clock = clock
        self._http = httpx.AsyncClient(base_url=base_url, transport=transport, timeout=30.0)
        self._balance_checked = False
        self.spent = Decimal(0)
        self.solves = 0

    @property
    def available(self) -> bool:
        return bool(self._key and self._key.get_secret_value()) and self._within_budget()

    def _within_budget(self) -> bool:
        return self.solves < self.max_solves and self.spent < self.max_spend

    async def close(self) -> None:
        await self._http.aclose()

    async def _call(self, method: str, *, paid: bool = False, **payload: Any) -> dict[str, Any]:
        """POST one API method.

        Free methods are retried on any transport/HTTP error. A ``paid`` call
        (createTask) is retried only when the request provably never left this
        machine (connection not established): if it may have reached 2Captcha, the
        task may exist and resending it would pay for a second CAPTCHA.
        """
        if not self._key:
            raise CaptchaUnavailable("CEM_TWOCAPTCHA_API_KEY is not set")
        body = {"clientKey": self._key.get_secret_value(), **payload}
        for attempt in range(3):
            try:
                response = await self._http.post(f"/{method}", json=body)
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
                error = f"{type(exc).__name__} (request not sent)"
            except httpx.HTTPError as exc:
                if paid:
                    raise CaptchaUnavailable(
                        f"2Captcha {method}: {type(exc).__name__}; the task may have been created, "
                        "so it is not resent (that could charge twice)"
                    ) from exc
                error = f"{type(exc).__name__}"
            else:
                if response.status_code == 200:
                    data: dict[str, Any] = response.json()
                    if data.get("errorId", 0) != 0:
                        raise TwoCaptchaApiError(str(data.get("errorCode")), str(data.get("errorDescription", "")))
                    return data
                if paid:
                    raise CaptchaUnavailable(
                        f"2Captcha {method}: HTTP {response.status_code}; not resent to avoid paying twice"
                    )
                error = f"HTTP {response.status_code}"
            log.warning("2Captcha %s failed (%s), attempt %d", method, error, attempt + 1)
            await self._sleep(5.0)
        raise CaptchaUnavailable(f"2Captcha {method} unreachable: {error}")

    async def balance(self) -> Decimal:
        data = await self._call("getBalance")
        return Decimal(str(data.get("balance", 0)))

    async def _check_balance_once(self) -> None:
        if self._balance_checked:
            return
        balance = await self.balance()
        self._balance_checked = True
        log.info("2Captcha balance: %s (run budget: %s, max %d solves)", balance, self.max_spend, self.max_solves)
        if balance <= 0:
            raise CaptchaUnavailable("2Captcha balance is empty; top it up yourself at https://2captcha.com")

    async def solve(self, *, site_key: str, page_url: str) -> CaptchaSolution:
        if not self._within_budget():
            raise CaptchaUnavailable(
                f"CAPTCHA budget for this run reached ({self.solves} solves, {self.spent} spent; "
                f"limits {self.max_solves} / {self.max_spend})"
            )
        await self._check_balance_once()
        task = {"type": "RecaptchaV2TaskProxyless", "websiteURL": page_url, "websiteKey": site_key}
        last_error = "no attempt"
        for attempt in range(self.max_tasks_per_solve):
            try:
                created = await self._call("createTask", paid=True, task=task)
                task_id = created["taskId"]
                solution = await self._wait_for(task_id)
            except TwoCaptchaApiError as exc:
                if exc.code not in self.RETRY_NEW_TASK:
                    raise
                last_error = exc.code
                log.warning("2Captcha %s; new task in 5 s (attempt %d)", exc.code, attempt + 1)
                await self._sleep(5.0)
                continue
            self.solves += 1
            self.spent += solution.cost
            if self.metrics:
                self.metrics.record_captcha(solution.cost)
            log.info("CAPTCHA solved by 2Captcha (task %s, cost %s, run total %s)", task_id, solution.cost, self.spent)
            return solution
        raise CaptchaUnavailable(
            f"2Captcha could not solve the CAPTCHA after {self.max_tasks_per_solve} tasks: {last_error}"
        )

    async def _wait_for(self, task_id: int | str) -> CaptchaSolution:
        deadline = self._clock() + self.timeout
        await self._sleep(self.poll_interval)  # docs: never poll immediately after createTask
        while True:
            data = await self._call("getTaskResult", taskId=task_id)
            if data.get("status") == "ready":
                token = (data.get("solution") or {}).get("gRecaptchaResponse")
                if not token:
                    raise TwoCaptchaApiError("ERROR_EMPTY_SOLUTION", "ready without gRecaptchaResponse")
                try:
                    cost = Decimal(str(data["cost"]))
                except (KeyError, InvalidOperation):
                    cost = self.FALLBACK_COST
                return CaptchaSolution(token=token, task_id=str(task_id), cost=cost)
            if self._clock() >= deadline:
                raise CaptchaUnavailable(f"2Captcha task {task_id} not solved within {self.timeout:.0f}s")
            await self._sleep(self.poll_interval)

    async def report(self, solution: CaptchaSolution, *, accepted: bool) -> None:
        method = "reportCorrect" if accepted else "reportIncorrect"
        try:
            await self._call(method, taskId=int(solution.task_id))
        except CaptchaUnavailable as exc:  # feedback is best effort
            log.warning("2Captcha %s for task %s failed: %s", method, solution.task_id, exc)


def build_captcha_provider(settings: Settings, metrics: Metrics | None = None) -> CaptchaProvider:
    match settings.captcha_provider:
        case CaptchaProviderName.DISABLED:
            return DisabledCaptchaProvider()
        case CaptchaProviderName.TWOCAPTCHA:
            return TwoCaptchaProvider(
                settings.twocaptcha_api_key,
                base_url=settings.twocaptcha_base_url,
                max_spend=Decimal(str(settings.captcha_max_spend)),
                max_solves=settings.captcha_max_solves,
                timeout=settings.captcha_timeout,
                poll_interval=settings.captcha_poll_interval,
                metrics=metrics,
            )
    raise CaptchaUnavailable(f"unknown captcha provider {settings.captcha_provider!r}")
