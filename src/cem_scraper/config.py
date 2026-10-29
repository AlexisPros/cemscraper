"""Environment-based configuration.

All settings are read from environment variables prefixed with ``CEM_`` (or a
local ``.env`` file). Nothing secret is hardcoded here.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Annotated

import httpx
from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

BASE_URL = "https://www.cem.edu.pl/pytcem/"
EULA_PATH = "eula_pes.php"
FORM_PATH = "form_pytania_pes.php"
QUESTION_PATH = "wyswietl_pytania_pes.php"

EXAM_TYPE = "PES"
SPECIALIZATION_NAME = "Psychiatria"
PSYCHIATRY_SPECIALIZATION_CODE = "725"


def default_target_sessions() -> list[str]:
    """Spring 2003 .. Autumn 2013 (22 sessions) plus Autumn 2026."""
    codes = [f"{year}{half}" for year in range(2003, 2014) for half in (1, 2)]
    codes.append("20262")
    return codes


class AccessProviderName(StrEnum):
    # Human solves the reCAPTCHA in a Playwright-controlled browser window; cookies go to HTTPX.
    INTERACTIVE_BROWSER = "interactive_browser"
    # Human solves it in their own Google Chrome, which nothing controls during the check;
    # the tool only reads the session cookie afterwards.
    MANUAL_CHROME = "manual_chrome"
    # Fully automated form submission; needs an *authorized* CaptchaProvider.
    HTTP_FORM = "http_form"
    # Offline: serves local HTML fixtures, never touches the network.
    FIXTURES = "fixtures"


class CaptchaProviderName(StrEnum):
    DISABLED = "disabled"
    TWOCAPTCHA = "2captcha"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CEM_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        # Validation errors must not print raw values: they may be proxy passwords or API keys.
        hide_input_in_errors=True,
    )

    database_url: SecretStr = SecretStr("postgresql://cem:cem_local_dev@localhost:5432/cem")

    base_url: str = BASE_URL
    user_agent: str = "cem-scraper/0.1 (research; contact: set CEM_CONTACT_EMAIL)"
    contact_email: str | None = None

    # Throughput: deliberately conservative defaults.
    # Initial in-flight limit; with adaptive concurrency it moves within [1, max_concurrency].
    concurrency: int = Field(default=2, ge=1, le=16)
    max_concurrency: int = Field(default=4, ge=1, le=16)
    adaptive_concurrency: bool = True
    latency_tolerance: float = Field(default=1.5, gt=1)
    requests_per_second: float = Field(default=0.5, gt=0, le=5)
    # Extra exit IPs (e.g. 2Captcha residential proxies), comma-separated http://user:pass@host:port.
    # Each proxy is a separate *lane*: its own CEM session, CAPTCHA, rate limit and concurrency.
    # Use only with the site operator's permission; requires CEM_ACCESS_PROVIDER=http_form.
    proxy_urls: Annotated[list[SecretStr], NoDecode] = Field(default_factory=list)
    # Also collect through this machine's own IP, next to the proxies.
    proxy_include_direct: bool = True
    # Paid proxy traffic a proxy lane may use per run (sent + received, approximate); then the lane
    # is retired and its sessions continue on the other lanes. One session is ~3-3.5 MB.
    proxy_max_mb: float = Field(default=150.0, gt=0)
    connect_timeout: float = Field(default=10.0, gt=0)
    read_timeout: float = Field(default=45.0, gt=0)
    max_retries: int = Field(default=4, ge=0, le=10)
    backoff_base: float = Field(default=2.0, gt=0)
    backoff_max: float = Field(default=60.0, gt=0)
    max_retry_after: float = Field(default=300.0, gt=0)
    circuit_breaker_threshold: int = Field(default=5, ge=1)
    # A timeout, 429 or 5xx pauses every request start on that lane (IP) for this long:
    # an overloaded server needs air, not retries 1 s later.
    overload_pause: float = Field(default=30.0, ge=0)
    # Stop the run when a lane has had failures and no successful response for this long.
    stall_timeout: float = Field(default=180.0, gt=0)

    # Discovery
    target_sessions: Annotated[list[str], NoDecode] = Field(default_factory=default_target_sessions)
    specialization_code: str = PSYCHIATRY_SPECIALIZATION_CODE
    max_question_number: int = Field(default=250, ge=1)
    missing_streak_to_stop: int = Field(default=3, ge=1)
    navigation_direction: str = "doprzodu"

    # Access management
    access_provider: AccessProviderName = AccessProviderName.INTERACTIVE_BROWSER
    captcha_provider: CaptchaProviderName = CaptchaProviderName.DISABLED
    twocaptcha_api_key: SecretStr | None = None
    twocaptcha_base_url: str = "https://api.2captcha.com"
    # Per-run caps, in your 2Captcha account currency (USD). One CAPTCHA costs ~0.001-0.003.
    captcha_max_spend: float = Field(default=1.0, gt=0, le=25)
    captcha_max_solves: int = Field(default=40, ge=1)
    # Lifetime cap over all runs recorded in the database (scrape_runs.diagnostics.captcha_cost).
    # 25 USD is roughly the 100 PLN budget; the default stays far below it.
    captcha_total_budget: float = Field(default=5.0, gt=0, le=25)
    captcha_timeout: float = Field(default=180.0, gt=0)
    captcha_poll_interval: float = Field(default=5.0, ge=1)
    accept_eula: bool = False
    browser_wait_timeout: float = Field(default=600.0, gt=0)
    browser_profile_dir: Path | None = None
    # manual_chrome: the Google Chrome binary (auto-detected when empty) and its local DevTools port.
    chrome_path: Path | None = None
    chrome_debug_port: int = Field(default=9222, ge=1024, le=65535)
    chrome_extra_args: Annotated[list[str], NoDecode] = Field(default_factory=list)
    # Headless is only useful for automated tests: a human must see the window to solve the CAPTCHA.
    browser_headless: bool = False
    max_reauthorizations_per_session: int = Field(default=1, ge=0)
    # Exam sessions open at once: the next one is authorized while the current one is collected.
    # Authorizations (CAPTCHAs) still happen one at a time; 1 = strictly sequential.
    pipeline_depth: int = Field(default=2, ge=1, le=4)
    # Send question requests through the authorized browser context instead of copying cookies.
    browser_context_requests: bool = False

    # Output / diagnostics
    output_dir: Path = Path("output")
    fixtures_dir: Path = Path("tests/fixtures/synthetic")
    save_html_dir: Path | None = None
    log_level: str = "INFO"
    log_json: bool = False

    @field_validator("chrome_path", "browser_profile_dir", "save_html_dir", mode="before")
    @classmethod
    def _empty_path_is_unset(cls, value: object) -> object:
        # `CEM_CHROME_PATH=` in .env would otherwise become Path(".")
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("target_sessions", "chrome_extra_args", "proxy_urls", mode="before")
    @classmethod
    def _split_sessions(cls, value: object) -> object:
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value

    @model_validator(mode="after")
    def _concurrency_bounds(self) -> Settings:
        if self.max_concurrency < self.concurrency:
            raise ValueError(f"max_concurrency ({self.max_concurrency}) < concurrency ({self.concurrency})")
        return self

    @model_validator(mode="after")
    def _proxy_lanes(self) -> Settings:
        for index, secret in enumerate(self.proxy_urls, 1):
            try:
                url = httpx.URL(secret.get_secret_value())
            except httpx.InvalidURL:
                url = None
            if url is None or url.scheme not in ("http", "https") or not url.host or not url.port:
                # Never echo the value: it carries the proxy password.
                raise ValueError(f"CEM_PROXY_URLS entry {index} must look like http://user:pass@host:port")
        values = [secret.get_secret_value().strip().rstrip("/") for secret in self.proxy_urls]
        if len(set(values)) != len(values):
            # The same login/session is the same exit IP: a second lane would only double the load.
            raise ValueError("CEM_PROXY_URLS lists the same proxy twice")
        if self.proxy_urls and self.access_provider not in (AccessProviderName.HTTP_FORM, AccessProviderName.FIXTURES):
            # A browser authorizes from this machine's IP; its cookie would then be used from another IP.
            raise ValueError("CEM_PROXY_URLS works only with CEM_ACCESS_PROVIDER=http_form")
        return self

    @field_validator("log_level")
    @classmethod
    def _upper_level(cls, value: str) -> str:
        return value.upper()

    def lane_specs(self) -> list[tuple[str, SecretStr | None]]:
        """``(name, proxy)`` per lane; names show host:port only, never credentials."""
        lanes: list[tuple[str, SecretStr | None]] = []
        if self.proxy_include_direct or not self.proxy_urls:
            lanes.append(("direct", None))
        for index, secret in enumerate(self.proxy_urls, 1):
            url = httpx.URL(secret.get_secret_value())
            lanes.append((f"proxy{index} ({url.host}:{url.port})", secret))
        return lanes

    def url(self, path: str) -> str:
        return self.base_url.rstrip("/") + "/" + path

    def effective_user_agent(self) -> str:
        if self.contact_email:
            return f"cem-scraper/0.1 (research; contact: {self.contact_email})"
        return self.user_agent
