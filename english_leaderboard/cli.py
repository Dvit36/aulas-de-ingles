from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
import sys
from typing import Sequence

from sqlalchemy import text

from .backup import create_backup, verify_backup
from .catalog import seed_database
from .config import Settings
from .database import (
    create_database_engine,
    create_session_factory,
    initialize_database,
    session_scope,
)


def _runtime() -> tuple[Settings, object]:
    settings = Settings.from_env()
    settings.ensure_directories()
    engine = create_database_engine(settings.database_url)
    initialize_database(engine)
    factory = create_session_factory(engine)
    return settings, factory


def _sync_google_sheets(settings: Settings, factory: object):
    from .google_sheets import sync_leaderboard_and_ledger
    from .scoring import leaderboard_rows, ledger_rows

    if not settings.google_sheets_spreadsheet_id:
        raise ValueError("GOOGLE_SHEETS_SPREADSHEET_ID não foi configurado")
    with session_scope(factory) as session:
        board = leaderboard_rows(session)
        ledger = ledger_rows(session)
    return sync_leaderboard_and_ledger(
        settings.google_sheets_spreadsheet_id,
        board,
        ledger,
        leaderboard_tab=settings.google_sheets_leaderboard_tab,
        ledger_tab=settings.google_sheets_ledger_tab,
    )


def command_init(_: argparse.Namespace) -> int:
    settings, factory = _runtime()
    with session_scope(factory) as session:
        seed_database(session, settings)
    print("Banco inicializado e seed aplicado.")
    return 0


def command_import(args: argparse.Namespace) -> int:
    from .importer import import_legacy_workbook

    settings, factory = _runtime()
    source = Path(args.source)
    if not source.is_file():
        raise FileNotFoundError(f"Planilha não encontrada: {source}")
    report_path = Path(args.report) if args.report else Path("import-reports") / (
        f"{source.stem}-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
    )
    with session_scope(factory) as session:
        seed_database(session, settings)
        report = import_legacy_workbook(
            session,
            source,
            namespace=args.namespace,
            sheet_name=args.sheet,
            report_path=report_path,
            commit=False,
        )
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    print(f"Relatório: {report_path.resolve()}")
    if settings.google_sheets_auto_sync:
        try:
            sync_result = _sync_google_sheets(settings, factory)
            print(
                "Google Sheets: "
                + json.dumps(sync_result.to_dict(), ensure_ascii=False)
            )
        except Exception as error:
            print(
                "aviso: a importação foi salva, mas a sincronização com "
                f"Google Sheets falhou: {error}",
                file=sys.stderr,
            )
    return 0 if report.inconsistent == 0 else 2


def command_sync_google_sheets(_: argparse.Namespace) -> int:
    settings, factory = _runtime()
    result = _sync_google_sheets(settings, factory)
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return 0


def command_backup(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    manifest, manifest_path = create_backup(
        settings.database_url, settings.upload_dir, args.destination
    )
    print(json.dumps(asdict(manifest), ensure_ascii=False, indent=2))
    print(f"Manifesto: {manifest_path}")
    return 0


def command_verify_backup(args: argparse.Namespace) -> int:
    valid = verify_backup(args.manifest)
    print("Backup íntegro." if valid else "Backup inválido ou incompleto.")
    return 0 if valid else 1


def command_analyze_image(args: argparse.Namespace) -> int:
    from .image_processing import ImagePolicy, analyze_image_bytes
    from .ocr import create_ocr_engine, extract_text
    from .rules import detect_completion, detect_platform

    settings = Settings.from_env()
    source = Path(args.image)
    analyzed = analyze_image_bytes(
        source.read_bytes(),
        policy=ImagePolicy(
            max_bytes=settings.max_upload_bytes,
            min_width=settings.min_image_width,
            min_height=settings.min_image_height,
            allowed_formats=settings.allowed_image_formats,
            blur_threshold=settings.min_laplacian_variance,
        ),
    )
    ocr = extract_text(analyzed.original_bytes, engine=create_ocr_engine())
    platform, platform_confidence, platform_details = detect_platform(
        ocr.text, color_signals=analyzed.color_signals
    )
    completed, completion_confidence, phrases = detect_completion(ocr.text, platform)
    payload = {
        "file": source.name,
        "format": analyzed.image_format,
        "dimensions": [analyzed.width, analyzed.height],
        "sha256": analyzed.sha256,
        "phash": analyzed.phash,
        "legibility": analyzed.legibility.as_dict(),
        "ocr_text": ocr.text,
        "ocr_confidence": ocr.confidence,
        "platform": platform,
        "platform_confidence": platform_confidence,
        "platform_details": platform_details,
        "completion": completed,
        "completion_confidence": completion_confidence,
        "completion_phrases": phrases,
        "recognized_units": 1 if completed else 0,
        "combo_ignored": True,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if platform in {"duolingo", "beconfident"} and completed else 2


def command_health(_: argparse.Namespace) -> int:
    _, factory = _runtime()
    with session_scope(factory) as session:
        session.execute(text("SELECT 1"))
    print("ok")
    return 0


def command_run_reminders(args: argparse.Namespace) -> int:
    from .reminders import run_due_reminders

    settings, factory = _runtime()
    with session_scope(factory) as session:
        seed_database(session, settings)
        attempts = run_due_reminders(session, settings, force=bool(args.force))
        payload = [
            {
                "id": attempt.id,
                "recipient": attempt.recipient_email,
                "status": attempt.status.value,
                "dry_run": attempt.dry_run,
            }
            for attempt in attempts
        ]
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def command_scheduler(_: argparse.Namespace) -> int:
    from .scheduler import run_forever

    run_forever()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="english-leaderboard",
        description="Operações do MVP de atividades de inglês",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    init_parser = commands.add_parser("init-db", help="Cria tabelas e aplica seed")
    init_parser.set_defaults(func=command_init)

    import_parser = commands.add_parser("import-xlsx", help="Importa planilha legada")
    import_parser.add_argument("source")
    import_parser.add_argument("--namespace", default="legacy_english_scores")
    import_parser.add_argument("--sheet", default="Página1")
    import_parser.add_argument("--report")
    import_parser.set_defaults(func=command_import)

    backup_parser = commands.add_parser("backup", help="Copia SQLite e uploads")
    backup_parser.add_argument("--destination", default="backups")
    backup_parser.set_defaults(func=command_backup)

    verify_parser = commands.add_parser("verify-backup", help="Valida checksums")
    verify_parser.add_argument("manifest")
    verify_parser.set_defaults(func=command_verify_backup)

    image_parser = commands.add_parser("analyze-image", help="Executa OCR/regras em uma imagem")
    image_parser.add_argument("image")
    image_parser.set_defaults(func=command_analyze_image)

    health_parser = commands.add_parser("health", help="Verifica acesso ao banco")
    health_parser.set_defaults(func=command_health)

    sheets_parser = commands.add_parser(
        "sync-google-sheets",
        help="Reconcilia leaderboard e ledger com o Google Sheets",
    )
    sheets_parser.set_defaults(func=command_sync_google_sheets)

    reminder_parser = commands.add_parser(
        "run-reminders", help="Executa um ciclo de lembretes"
    )
    reminder_parser.add_argument(
        "--force", action="store_true", help="Ignora dia/horário configurados"
    )
    reminder_parser.set_defaults(func=command_run_reminders)

    scheduler_parser = commands.add_parser(
        "scheduler", help="Mantém o processo independente de lembretes"
    )
    scheduler_parser.set_defaults(func=command_scheduler)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except Exception as error:
        parser.exit(1, f"erro: {error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
