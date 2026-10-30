"""`cem-scraper check-new`: find sessions newer than the newest collected one and collect them (no network)."""

from __future__ import annotations

import argparse

import pytest

from cem_scraper import main as cli
from cem_scraper.database import InMemoryRepository
from cem_scraper.models import CollectionStatus, ExamSession
from tests.conftest import EDGE, load

FORM = load(EDGE / "entry_form.html")  # offers 20031, 20032, 20131, 20132, 20262
NEW_OPTIONS = '<option value="20271">Wiosna 2027</option><option value="20272">Jesień 2027</option>'


def form_with(extra: str) -> str:
    return FORM.replace(
        '<option value="20262" selected>Jesień 2026</option>', f'<option value="20262">Jesień 2026</option>{extra}'
    )


def test_only_codes_newer_than_the_newest_collected_count() -> None:
    offered = {"": "-- wybierz --", "20031": "", "20142": "", "20262": "", "20272": "", "20271": "", "x": ""}
    assert cli.newer_session_codes(offered, "20262") == ["20271", "20272"]
    assert cli.newer_session_codes(offered, "20271") == ["20272"]
    assert cli.newer_session_codes(offered, "20272") == []


class Harness:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, form_html: str) -> None:
        self.repo = InMemoryRepository()
        self.fetches = 0
        self.scraped: list[list[str]] = []

        async def postgres(settings: object) -> InMemoryRepository:
            return self.repo

        async def fetch(settings: object) -> str:
            self.fetches += 1
            return form_html

        async def scrape(args: argparse.Namespace) -> int:
            self.scraped.append(args.session)
            return cli.EXIT_OK

        monkeypatch.setattr(cli, "_postgres", postgres)
        monkeypatch.setattr(cli, "_fetch_entry_form", fetch)
        monkeypatch.setattr(cli, "cmd_scrape", scrape)

    async def mark(self, code: str, status: CollectionStatus) -> None:
        state = await self.repo.ensure_session(ExamSession.from_code(code))
        await self.repo.set_session_status(state.id, status)

    async def run(self, *argv: str) -> int:
        return await cli.cmd_check_new(cli.build_parser().parse_args(["check-new", *argv]))


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.chdir(tmp_path)  # never read the developer's real .env
    monkeypatch.setenv("CEM_ACCEPT_EULA", "true")


async def test_completed_new_sessions_are_not_collected_again(env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    h = Harness(monkeypatch, form_with(NEW_OPTIONS))
    await h.mark("20271", CollectionStatus.COMPLETED)
    await h.mark("20272", CollectionStatus.COMPLETED)
    assert await h.run() == cli.EXIT_OK
    assert h.scraped == []


async def test_new_sessions_are_collected_into_the_database(
    env: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    h = Harness(monkeypatch, form_with(NEW_OPTIONS))
    await h.mark("20262", CollectionStatus.COMPLETED)
    assert await h.run() == cli.EXIT_OK
    assert h.scraped == [["20271", "20272"]]
    out = capsys.readouterr().out
    assert "newest collected: 20262; newest in the form: 20272" in out
    assert "new: 20271  Wiosna 2027" in out


async def test_check_only_collects_nothing(env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    h = Harness(monkeypatch, form_with(NEW_OPTIONS))
    assert await h.run("--check-only") == cli.EXIT_OK
    assert h.fetches == 1 and h.scraped == []


async def test_nothing_new_means_nothing_collected(
    env: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    h = Harness(monkeypatch, FORM)
    assert await h.run() == cli.EXIT_OK
    assert h.scraped == [] and "no new sessions" in capsys.readouterr().out


async def test_older_untargeted_sessions_are_never_new(env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """2014-2025 are in the form but were never targeted: the newest *target* (20262) is the baseline."""
    h = Harness(monkeypatch, form_with('<option value="20141">Wiosna 2014</option><option value="20252">x</option>'))
    assert await h.run() == cli.EXIT_OK  # empty database
    assert h.scraped == []


async def test_a_new_session_that_did_not_finish_is_tried_again(env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    h = Harness(monkeypatch, form_with(NEW_OPTIONS))
    await h.mark("20271", CollectionStatus.BLOCKED)  # e.g. questions not published yet last time
    await h.mark("20272", CollectionStatus.COMPLETED)
    assert await h.run() == cli.EXIT_OK
    assert h.scraped == [["20271"]]  # not lost just because a newer session was collected meanwhile
    h2 = Harness(monkeypatch, form_with(NEW_OPTIONS))
    await h2.mark("20271", CollectionStatus.IN_PROGRESS)
    assert await h2.run() == cli.EXIT_OK
    assert h2.scraped == [["20271", "20272"]]  # resumes 20271 (stored questions are kept)


async def test_without_eula_acceptance_nothing_is_requested(
    monkeypatch: pytest.MonkeyPatch, tmp_path, capsys: pytest.CaptureFixture[str]
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.chdir(tmp_path)
    h = Harness(monkeypatch, form_with(NEW_OPTIONS))
    assert await h.run() == cli.EXIT_BLOCKED
    assert h.fetches == 0 and "CEM_ACCEPT_EULA=true" in capsys.readouterr().err


async def test_a_form_without_sessions_is_an_error(env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    h = Harness(monkeypatch, "<html><body><form><select name='sesja'></select></form></body></html>")
    assert await h.run() == cli.EXIT_ERROR
    assert h.scraped == []
