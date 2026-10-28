from __future__ import annotations

import os
import tempfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

from cem_scraper.config import AccessProviderName, Settings
from cem_scraper.parser import decode_html

FIXTURES = Path(__file__).parent / "fixtures"
SYNTHETIC = FIXTURES / "synthetic"
EDGE = FIXTURES / "edge"


def load(path: Path) -> str:
    return decode_html(path.read_bytes())


@pytest.fixture
def html_2003() -> str:
    return load(SYNTHETIC / "20031_1.html")


@pytest.fixture
def html_2026() -> str:
    return load(SYNTHETIC / "20262_1.html")


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        access_provider=AccessProviderName.FIXTURES,
        fixtures_dir=SYNTHETIC,
        requests_per_second=5,
        concurrency=2,
        max_retries=2,
        backoff_base=0.01,
        backoff_max=0.02,
        output_dir=tmp_path / "out",
        max_question_number=30,
    )


def question_page(
    *,
    number: int = 1,
    center: str | None = None,
    question: str = "Treść pytania testowego?",
    options: dict[str, str] | None = None,
    answer: str | None = "Prawidłowa odpowiedź to: B",
    stats_rows: list[list[str]] | None = None,
) -> str:
    """Build a SYNTHETIC question page following the documented structure."""
    options = options if options is not None else {k: f"opcja {k.lower()}" for k in "ABCDE"}
    if stats_rows is None:
        stats_rows = [
            ["Procent odp. A", "Procent odp. B", "Procent odp. C", "Procent odp. D", "Procent odp. E", "Wskaźnik trudności"],
            ["10.0", "60.0", "10.0", "10.0", "10.0", "0.600"],
        ]
    center_html = "" if center == "" else f"<center>{center or f'Pytanie nr {number}'}</center>"
    option_html = "<br>".join(f"<p><b>{k}.</b> {v}</p>" for k, v in options.items())
    answer_html = f'<div id="odpowiedz1" style="display: none;"><p>{answer}</p></div>' if answer is not None else ""
    rows = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>" for row in stats_rows)
    stats_html = f'<div id="statystyki1" style="display: none;"><table>{rows}</table></div>' if stats_rows else ""
    return (
        '<html><head><meta charset="utf-8"></head><body><div class="marginesy"><div>'
        f"<p>Nagłówek</p>{center_html}<br><p>{question}</p><br>{option_html}{answer_html}{stats_html}"
        "</div></div></body></html>"
    )


# --------------------------------------------------------------------------- PostgreSQL


@pytest.fixture(scope="session")
def postgres_dsn() -> Iterator[str]:
    """A throwaway PostgreSQL: CEM_TEST_DATABASE_URL (WILL BE WIPED) or an embedded pgserver."""
    dsn = os.environ.get("CEM_TEST_DATABASE_URL")
    if dsn:
        yield dsn
        return
    try:
        import pgserver
    except ImportError:
        pytest.skip("no PostgreSQL available: set CEM_TEST_DATABASE_URL or install pgserver")
    directory = tempfile.mkdtemp(prefix="cem_pg_")
    server = pgserver.get_server(directory, cleanup_mode="delete")
    try:
        yield server.get_uri()
    finally:
        server.cleanup()


@pytest.fixture
async def pg_repo(postgres_dsn: str) -> AsyncIterator[object]:
    import psycopg

    from cem_scraper.database import PostgresRepository

    async with await psycopg.AsyncConnection.connect(postgres_dsn, autocommit=True) as conn:
        await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    repo = await PostgresRepository.connect(postgres_dsn)
    await repo.migrate()
    try:
        yield repo
    finally:
        await repo.close()
