"""CAPTCHA provider interface.

CAPTCHA verification is a separate access-management stage. This module only
defines the contract an *authorized* integration must satisfy. No working
third-party solving service is wired in: using one requires express
permission from CEM and compliance with the service's and Google's terms, and
that permission has not been established. Until it is, the only working path
is a human solving the challenge in a real browser
(``InteractiveBrowserAccessProvider``).

Rules for any implementation:
- tokens are used once, immediately, for the form submission they were
  requested for; never cached, replayed, logged or persisted;
- no purchases are made automatically; spending is capped by configuration;
- ``solve`` raises ``CaptchaUnavailable`` instead of attempting a bypass.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from cem_scraper.config import CaptchaProviderName, Settings


class CaptchaUnavailable(Exception):
    """No authorized CAPTCHA mechanism is available."""


class CaptchaProvider(ABC):
    name: str

    @property
    @abstractmethod
    def available(self) -> bool:
        """True only for an integration that is configured *and* authorized."""

    @abstractmethod
    async def solve(self, *, site_key: str, page_url: str) -> str:
        """Return a one-time verification token for ``page_url``."""


class DisabledCaptchaProvider(CaptchaProvider):
    name = "disabled"

    @property
    def available(self) -> bool:
        return False

    async def solve(self, *, site_key: str, page_url: str) -> str:
        raise CaptchaUnavailable(
            "No authorized automated CAPTCHA mechanism is configured. Automated session entry is "
            "blocked until CEM authorizes automated access (or provides an export/API). Use "
            "CEM_ACCESS_PROVIDER=interactive_browser to solve the reCAPTCHA yourself."
        )


def build_captcha_provider(settings: Settings) -> CaptchaProvider:
    if settings.captcha_provider is CaptchaProviderName.DISABLED:
        return DisabledCaptchaProvider()
    raise CaptchaUnavailable(f"unknown captcha provider {settings.captcha_provider!r}")
