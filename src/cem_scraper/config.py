"""Environment-based configuration.

All settings are read from environment variables prefixed with ``CEM_`` (or a
local ``.env`` file). Nothing secret is hardcoded here.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Annotated

from pydantic import Field, SecretStr, field_validator
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
    # Human solves the reCAPTCHA in a real browser window; cookies are handed to HTTPX.
    INTERACTIVE_BROWSER = "interactive_browser"
    # Fully automated form submission; needs an *authorized* CaptchaProvider.
    HTTP_FORM = "http_form"
    # Offline: serves local HTML fixtures, never touches the network.
    FIXTURES = "fixtures"


class CaptchaProviderName(StrEnum):
    DISABLED = "disabled"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CEM_", env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: SecretStr = SecretStr("postgresql://cem:cem_local_dev@localhost:5432/cem")

    base_url: str = BASE_URL
    user_agent: str = "cem-scraper/0.1 (research; contact: set CEM_CONTACT_EMAIL)"
    contact_email: str | None = None

    # Throughput: deliberately conservative defaults.
    concurrency: int = Field(default=2, ge=1, le=8)
    requests_per_second: float = Field(default=0.5, gt=0, le=5)
    connect_timeout: float = Field(default=10.0, gt=0)
    read_timeout: float = Field(default=45.0, gt=0)
    max_retries: int = Field(default=4, ge=0, le=10)
    backoff_base: float = Field(default=2.0, gt=0)
    backoff_max: float = Field(default=60.0, gt=0)
    max_retry_after: float = Field(default=300.0, gt=0)
    circuit_breaker_threshold: int = Field(default=5, ge=1)

    # Discovery
    target_sessions: Annotated[list[str], NoDecode] = Field(default_factory=default_target_sessions)
    specialization_code: str = PSYCHIATRY_SPECIALIZATION_CODE
    max_question_number: int = Field(default=250, ge=1)
    missing_streak_to_stop: int = Field(default=3, ge=1)
    navigation_direction: str = "doprzodu"

    # Access management
    access_provider: AccessProviderName = AccessProviderName.INTERACTIVE_BROWSER
    captcha_provider: CaptchaProviderName = CaptchaProviderName.DISABLED
    accept_eula: bool = False
    browser_wait_timeout: float = Field(default=600.0, gt=0)
    browser_profile_dir: Path | None = None
    # Headless is only useful for automated tests: a human must see the window to solve the CAPTCHA.
    browser_headless: bool = False
    max_reauthorizations_per_session: int = Field(default=1, ge=0)
    # Send question requests through the authorized browser context instead of copying cookies.
    browser_context_requests: bool = False

    # Output / diagnostics
    output_dir: Path = Path("output")
    fixtures_dir: Path = Path("tests/fixtures/synthetic")
    save_html_dir: Path | None = None
    log_level: str = "INFO"
    log_json: bool = False

    @field_validator("target_sessions", mode="before")
    @classmethod
    def _split_sessions(cls, value: object) -> object:
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value

    @field_validator("log_level")
    @classmethod
    def _upper_level(cls, value: str) -> str:
        return value.upper()

    def url(self, path: str) -> str:
        return self.base_url.rstrip("/") + "/" + path

    def effective_user_agent(self) -> str:
        if self.contact_email:
            return f"cem-scraper/0.1 (research; contact: {self.contact_email})"
        return self.user_agent
