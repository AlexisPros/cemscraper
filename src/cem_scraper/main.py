"""Command-line interface: ``cem-scraper <command>``."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from cem_scraper.captcha import CaptchaUnavailable, build_captcha_provider
from cem_scraper.config import EULA_PATH, FORM_PATH, AccessProviderName, Settings
from cem_scraper.database import DatabaseUnavailable, InMemoryRepository, PostgresRepository, Repository
from cem_scraper.exporter import export_records
from cem_scraper.logging_config import configure_logging
from cem_scraper.metrics import Metrics
from cem_scraper.models import (
    AnswerStatistics,
    CorrectAnswer,
    ExamSession,
    QuestionData,
    RunStatus,
)
from cem_scraper.orchestrator import Orchestrator, RunReport
from cem_scraper.parser import ParseError, decode_html, parse_entry_form, parse_question_page
from cem_scraper.session_manager import (
    AccessBlocked,
    FixtureAccessProvider,
    SessionManager,
    build_access_provider,
)

log = logging.getLogger("cem_scraper")

EXIT_OK, EXIT_ERROR, EXIT_BLOCKED, EXIT_PARTIAL = 0, 1, 2, 3


def _settings(args: argparse.Namespace, **extra: Any) -> Settings:
    overrides: dict[str, Any] = {k: v for k, v in extra.items() if v is not None}
    for name in ("concurrency", "requests_per_second", "log_level", "output_dir"):
        value = getattr(args, name, None)
        if value is not None:
            overrides[name] = value
    return Settings(**overrides)


async def _postgres(settings: Settings) -> PostgresRepository:
    repo = await PostgresRepository.connect(settings.database_url.get_secret_value())
    await repo.migrate()
    return repo


# --------------------------------------------------------------------------- commands


async def cmd_init_db(args: argparse.Namespace) -> int:
    settings = _settings(args)
    repo = await PostgresRepository.connect(settings.database_url.get_secret_value())
    try:
        applied = await repo.migrate()
    finally:
        await repo.close()
    print(f"migrations applied: {applied or 'none (up to date)'}")
    return EXIT_OK


async def cmd_scrape(args: argparse.Namespace) -> int:
    extra: dict[str, Any] = {}
    if args.dry_run:
        extra["access_provider"] = AccessProviderName.FIXTURES
        if args.fixtures:
            extra["fixtures_dir"] = args.fixtures
    elif args.provider:
        extra["access_provider"] = AccessProviderName(args.provider)
    settings = _settings(args, **extra)
    codes = args.session or settings.target_sessions
    for code in codes:
        ExamSession.from_code(code)  # validate early

    use_memory = args.memory or (args.dry_run and not args.db)
    repo: Repository = InMemoryRepository() if use_memory else await _postgres(settings)
    metrics = Metrics()
    try:
        provider = build_access_provider(settings, build_captcha_provider(settings, metrics))
    except AccessBlocked as exc:
        log.error("%s", exc)
        await repo.close()
        return EXIT_BLOCKED
    manager = SessionManager(settings, provider, metrics)
    orchestrator = Orchestrator(settings, repo, manager, metrics)
    log.info(
        "provider=%s sessions=%s concurrency=%d rps=%.2f storage=%s",
        provider.name,
        ",".join(codes),
        settings.concurrency,
        settings.requests_per_second,
        "memory" if use_memory else "postgres",
    )
    try:
        report = await orchestrator.run(codes, refetch=args.refetch, only_failed=args.retry_failed)
        if use_memory:
            out = settings.output_dir / ("dry_run.jsonl" if args.dry_run else "memory_run.jsonl")
            count = export_records(await repo.fetch_records(codes), out, "jsonl", include_validation=True)
            print(f"wrote {count} records to {out}")
    finally:
        await manager.close()
        await repo.close()
    _print_report(report)
    return {
        RunStatus.COMPLETED: EXIT_OK,
        RunStatus.BLOCKED: EXIT_BLOCKED,
    }.get(report.status, EXIT_PARTIAL)


def _print_report(report: RunReport) -> None:
    m = report.metrics
    print(f"\nrun {report.run_id}: {report.status.value}")
    for s in report.sessions:
        line = (
            f"  {s.session_code}: {s.outcome.value:<10} discovered={s.discovered_question_count:<4} stored={s.stored}"
        )
        if s.missing:
            line += f" missing={s.missing}"
        if s.failed:
            line += f" failed={sorted(s.failed)}"
        if s.error:
            line += f"\n      reason: {s.error}"
        print(line)

    def secs(key: str) -> str:
        return "-" if m[key] is None else f"{m[key]}s"

    if m.get("captcha_solves"):
        print(f"  captcha: {m['captcha_solves']} solved, cost {m['captcha_cost']:.4f} (2Captcha account currency)")
    print(
        f"  requests={m['requests']} ok={m['request_successes']} failed={m['request_failures']} "
        f"retries={m['retries']} avg={secs('duration_avg_s')} median={secs('duration_median_s')} "
        f"p95={secs('duration_p95_s')} q/min={m['questions_per_minute']} total={m['total_duration_s']}s"
    )


async def cmd_reparse(args: argparse.Namespace) -> int:
    settings = _settings(args)
    directory = args.dir or settings.save_html_dir
    if directory is None or not directory.is_dir():
        print("error: no snapshot directory (set CEM_SAVE_HTML_DIR or pass --dir)", file=sys.stderr)
        return EXIT_ERROR
    repo = await _postgres(settings)
    metrics = Metrics()
    # Reparsing never contacts CEM: the offline provider only completes the orchestrator's wiring.
    manager = SessionManager(settings, FixtureAccessProvider(directory), metrics)
    try:
        codes = args.session or settings.target_sessions
        report = await Orchestrator(settings, repo, manager, metrics).reparse(codes, directory, everything=args.all)
    finally:
        await repo.close()
    _print_report(report)
    return EXIT_OK if report.status is RunStatus.COMPLETED else EXIT_PARTIAL


async def cmd_status(args: argparse.Namespace) -> int:
    settings = _settings(args)
    repo = await _postgres(settings)
    try:
        summaries = await repo.summaries()
        runs = await repo.last_runs()
    finally:
        await repo.close()
    print(f"{'session':<8} {'status':<12} {'found':>5} {'stored':>6} {'review':>6} {'failed':>6} {'missing':>7}")
    for s in summaries:
        print(
            f"{s.session_code:<8} {s.collection_status:<12} {s.discovered_question_count or 0:>5} "
            f"{s.stored:>6} {s.needs_review:>6} {s.failed:>6} {s.missing:>7}"
        )
        if s.last_error:
            print(f"         └ {s.last_error}")
    known = {s.session_code for s in summaries}
    pending = [c for c in settings.target_sessions if c not in known]
    if pending:
        print(f"not started: {', '.join(pending)}")
    if runs:
        print("\nrecent runs:")
        for r in runs:
            print(
                f"  {r['started_at']:%Y-%m-%d %H:%M} {r['status']:<11} requested={r['total_requested']} "
                f"ok={r['total_succeeded']} failed={r['total_failed']} retries={r['retry_count']}"
            )
    return EXIT_OK


async def cmd_export(args: argparse.Namespace) -> int:
    settings = _settings(args)
    repo = await _postgres(settings)
    try:
        records = await repo.fetch_records(args.session)
    finally:
        await repo.close()
    path = args.output or settings.output_dir / f"pes_psychiatria.{args.format}"
    count = export_records(records, Path(path), args.format, include_validation=args.include_validation)
    print(f"exported {count} questions to {path}")
    return EXIT_OK


def _read_html(path: str) -> str:
    return decode_html(Path(path).read_bytes())


def cmd_parse(args: argparse.Namespace) -> int:
    session = ExamSession.from_code(args.session)
    try:
        parsed = parse_question_page(_read_html(args.file), expected_number=args.number)
        question = parsed.to_scraped(session, args.number)
    except ParseError as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_ERROR
    print(json.dumps(question.to_record(), ensure_ascii=False, indent=2))
    if question.validation_issues:
        print(f"needs review: {question.validation_issues}", file=sys.stderr)
    return EXIT_OK


async def cmd_validate(args: argparse.Namespace) -> int:
    """Re-validate every stored record against the Pydantic models."""
    settings = _settings(args)
    repo = await _postgres(settings)
    try:
        records = await repo.fetch_records(args.session)
    finally:
        await repo.close()
    invalid = review = 0
    for record in records:
        key = f"{record['session_id']}#{record['question_number']}"
        try:
            QuestionData.model_validate(record["question_data"])
            if record["correct_answer"] is not None:
                CorrectAnswer.model_validate(record["correct_answer"])
            if record["answer_statistics"] is not None:
                stats = AnswerStatistics.model_validate(record["answer_statistics"])
                for issue in stats.soft_issues():
                    print(f"review  {key}: {issue}")
        except ValidationError as exc:
            invalid += 1
            print(f"INVALID {key}: {exc.errors()[0]['msg']}")
        if record["validation_status"] != "valid":
            review += 1
            print(f"review  {key}: {'; '.join(record['validation_issues'])}")
    print(f"\n{len(records)} records checked: {invalid} invalid, {review} flagged for review")
    return EXIT_ERROR if invalid else EXIT_OK


async def cmd_verify_sessions(args: argparse.Namespace) -> int:
    """Check session codes / specialization against the official form's <select> options."""
    settings = _settings(args)
    if args.html:
        html = _read_html(args.html)
    else:
        if not settings.accept_eula:
            print(
                f"Fetching the live form requires accepting the terms at {settings.url(EULA_PATH)}.\n"
                "Set CEM_ACCEPT_EULA=true after reading them, or pass --html with a saved form page.",
                file=sys.stderr,
            )
            return EXIT_BLOCKED
        from cem_scraper.client import CemClient

        async with CemClient(settings, Metrics()) as client:
            await client.get(EULA_PATH)
            html = (await client.post(FORM_PATH, {"akceptacja": "tak"})).html
    form = parse_entry_form(html)
    if not form.session_options:
        print("the form has no static <select name=sesja> options (maybe loaded dynamically)")
        return EXIT_ERROR
    ok = True
    for code in settings.target_sessions:
        label = form.session_options.get(code)
        expected = ExamSession.from_code(code).label
        print(f"  {code}: {'OK   ' if label else 'MISSING'} {expected:<24} form label: {label or '-'}")
        ok &= label is not None
    spec = form.specialization_options.get(settings.specialization_code)
    print(f"specialization {settings.specialization_code}: {spec or 'not in static options'}")
    print(f"reCAPTCHA sitekey present: {bool(form.recaptcha_sitekey)}")
    return EXIT_OK if ok else EXIT_ERROR


async def cmd_captcha_balance(args: argparse.Namespace) -> int:
    settings = _settings(args)
    captcha = build_captcha_provider(settings)
    try:
        balance = await captcha.balance()
    except CaptchaUnavailable as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    finally:
        await captcha.close()
    if balance is None:
        print(f"provider '{captcha.name}' has no balance (set CEM_CAPTCHA_PROVIDER=2captcha)")
        return EXIT_ERROR
    estimate = settings.captcha_max_solves
    print(f"{captcha.name} balance: {balance}  (run caps: {settings.captcha_max_spend} / {estimate} solves)")
    return EXIT_OK


# --------------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cem-scraper", description=__doc__)
    parser.add_argument("--log-level", dest="log_level")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="apply database migrations")

    scrape = sub.add_parser("scrape", help="collect questions (resumes by default)")
    scrape.add_argument("--session", action="append", help="session code, e.g. 20031 (repeatable)")
    scrape.add_argument("--resume", action="store_true", help="resume (the default; kept for clarity)")
    scrape.add_argument("--refetch", action="store_true", help="fetch everything again, even stored questions")
    scrape.add_argument("--retry-failed", action="store_true", help="only reprocess failed questions")
    scrape.add_argument("--dry-run", action="store_true", help="offline: use local fixtures, never the network")
    scrape.add_argument("--fixtures", type=Path, help="fixtures directory for --dry-run")
    scrape.add_argument("--db", action="store_true", help="with --dry-run: write to PostgreSQL")
    scrape.add_argument("--memory", action="store_true", help="in-memory storage (results exported to JSONL)")
    scrape.add_argument("--provider", choices=[p.value for p in AccessProviderName])
    scrape.add_argument("--concurrency", type=int)
    scrape.add_argument("--rps", dest="requests_per_second", type=float)
    scrape.add_argument("--output-dir", dest="output_dir", type=Path)

    sub.add_parser("status", help="per-session progress and recent runs")

    reparse = sub.add_parser("reparse", help="re-process saved HTML snapshots offline (no CAPTCHA, no network)")
    reparse.add_argument("--session", action="append")
    reparse.add_argument("--dir", type=Path, help="snapshot directory (default: CEM_SAVE_HTML_DIR)")
    reparse.add_argument("--all", action="store_true", help="reparse every question, not only content failures")

    export = sub.add_parser("export", help="export stored questions")
    export.add_argument("--format", choices=["json", "jsonl"], default="jsonl")
    export.add_argument("--session", action="append")
    export.add_argument("--output", type=Path)
    export.add_argument("--include-validation", action="store_true")
    export.add_argument("--output-dir", dest="output_dir", type=Path)

    validate = sub.add_parser("validate", help="re-validate stored records")
    validate.add_argument("--session", action="append")

    parse = sub.add_parser("parse", help="parse a saved HTML page offline")
    parse.add_argument("file")
    parse.add_argument("--session", required=True)
    parse.add_argument("--number", type=int, required=True)

    sub.add_parser("captcha-balance", help="show the CAPTCHA provider balance (free call)")

    verify = sub.add_parser("verify-sessions", help="check session codes against the official form")
    verify.add_argument("--html", help="saved form_pytania_pes.php page (offline)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = Settings(**({"log_level": args.log_level} if args.log_level else {}))
    configure_logging(settings.log_level, settings.log_json)
    commands = {
        "init-db": cmd_init_db,
        "scrape": cmd_scrape,
        "status": cmd_status,
        "reparse": cmd_reparse,
        "export": cmd_export,
        "validate": cmd_validate,
        "verify-sessions": cmd_verify_sessions,
        "captcha-balance": cmd_captcha_balance,
    }
    try:
        if args.command == "parse":
            return cmd_parse(args)
        return asyncio.run(commands[args.command](args))
    except DatabaseUnavailable as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        print("\ninterrupted; progress saved. Rerun the same command to resume.", file=sys.stderr)
        return 130
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
