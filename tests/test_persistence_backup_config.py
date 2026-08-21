from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.exc import DatabaseError
from sqlalchemy.orm.exc import StaleDataError

from english_leaderboard.catalog import seed_catalog
from english_leaderboard.backup import create_backup, verify_backup
from english_leaderboard.config import Settings
from english_leaderboard.database import (
    create_database_engine,
    create_session_factory,
    initialize_database,
)
from english_leaderboard.models import (
    Activity,
    LedgerKind,
    LedgerTransaction,
    Role,
    Submission,
    SubmissionImage,
    SubmissionStatus,
    User,
)
from english_leaderboard.services import UploadPayload, submit_evidence

from conftest import FakeDuolingoOCR, make_png


def test_sqlite_persists_after_engine_restart(tmp_path: Path):
    database = tmp_path / "persist.db"
    url = f"sqlite:///{database}"
    first_engine = create_database_engine(url)
    initialize_database(first_engine)
    first_factory = create_session_factory(first_engine)
    with first_factory() as session:
        session.add(
            User(username="persist", display_name="Persist", role=Role.STUDENT)
        )
        session.commit()
    first_engine.dispose()

    second_engine = create_database_engine(url)
    second_factory = create_session_factory(second_engine)
    with second_factory() as session:
        assert (
            session.scalar(select(User).where(User.username == "persist")) is not None
        )
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    second_engine.dispose()


def test_backup_snapshot_and_upload_archive_are_verifiable(tmp_path: Path):
    database = tmp_path / "app.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO sample(value) VALUES ('ok')")
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    (uploads / "proof.bin").write_bytes(b"evidence")
    manifest, path = create_backup(f"sqlite:///{database}", uploads, tmp_path / "backup")
    assert manifest.database_sha256
    assert verify_backup(path)


def test_demo_auth_is_refused_in_production():
    settings = Settings(
        app_env="production",
        demo_auth_enabled=True,
        allowed_usernames=frozenset({"admin"}),
        admin_usernames=frozenset({"admin"}),
    )
    with pytest.raises(RuntimeError):
        settings.validate()


def test_google_sheets_enabled_requires_spreadsheet_id(monkeypatch):
    monkeypatch.setenv("GOOGLE_SHEETS_AUTO_SYNC", "true")
    monkeypatch.delenv("GOOGLE_SHEETS_SPREADSHEET_ID", raising=False)

    with pytest.raises(ValueError, match="GOOGLE_SHEETS_SPREADSHEET_ID"):
        Settings.from_env(env_file=None)


def test_google_sheets_settings_are_loaded_and_validated(monkeypatch):
    monkeypatch.setenv("GOOGLE_SHEETS_AUTO_SYNC", "true")
    monkeypatch.setenv("GOOGLE_SHEETS_SPREADSHEET_ID", "sheet-123")
    monkeypatch.setenv("GOOGLE_SHEETS_LEADERBOARD_TAB", "Ranking")
    monkeypatch.setenv("GOOGLE_SHEETS_LEDGER_TAB", "Movimentos")

    settings = Settings.from_env(env_file=None)

    assert settings.google_sheets_auto_sync is True
    assert settings.google_sheets_spreadsheet_id == "sheet-123"
    assert settings.google_sheets_leaderboard_tab == "Ranking"
    assert settings.google_sheets_ledger_tab == "Movimentos"


def test_google_sheets_tabs_must_be_distinct():
    settings = Settings(
        google_sheets_auto_sync=True,
        google_sheets_spreadsheet_id="sheet-123",
        google_sheets_leaderboard_tab="Data",
        google_sheets_ledger_tab="Data",
    )

    with pytest.raises(ValueError, match="nomes diferentes"):
        settings.validate()


def test_sqlite_ledger_rejects_update_and_preserves_value(tmp_path: Path):
    database = tmp_path / "immutable.db"
    engine = create_database_engine(f"sqlite:///{database}")
    initialize_database(engine)
    factory = create_session_factory(engine)
    with factory() as session:
        user = User(username="ledger", display_name="Ledger", role=Role.STUDENT)
        session.add(user)
        session.flush()
        transaction = LedgerTransaction(
            student_id=user.id,
            points=10,
            kind=LedgerKind.ADJUSTMENT,
            source_type="test",
            source_key="test:immutable",
        )
        session.add(transaction)
        session.commit()
        transaction.points = 999
        with pytest.raises(DatabaseError, match="immutable"):
            session.commit()
        session.rollback()
        assert session.scalar(select(LedgerTransaction.points)) == 10
    engine.dispose()


def test_submission_version_prevents_concurrent_terminal_decisions(tmp_path: Path):
    database = tmp_path / "versioned.db"
    engine = create_database_engine(f"sqlite:///{database}")
    initialize_database(engine)
    factory = create_session_factory(engine)
    with factory() as setup:
        user = User(username="version", display_name="Version", role=Role.STUDENT)
        activity = Activity(code="version_test", name="Version test", points=1)
        setup.add_all([user, activity])
        setup.flush()
        submission = Submission(
            student_id=user.id,
            activity_id=activity.id,
            status=SubmissionStatus.NEEDS_REVIEW,
        )
        setup.add(submission)
        setup.commit()
        submission_id = submission.id
    first = factory()
    second = factory()
    try:
        first_submission = first.get(Submission, submission_id)
        second_submission = second.get(Submission, submission_id)
        first_submission.status = SubmissionStatus.APPROVED_MANUAL
        first.commit()
        second_submission.status = SubmissionStatus.REJECTED
        with pytest.raises(StaleDataError):
            second.commit()
        second.rollback()
    finally:
        first.close()
        second.close()
        engine.dispose()


def test_database_and_committed_upload_survive_runtime_restart(tmp_path: Path):
    database = tmp_path / "restart.db"
    upload_dir = tmp_path / "uploads"
    url = f"sqlite:///{database}"
    settings = Settings(
        app_env="test",
        database_url=url,
        upload_dir=upload_dir,
        min_image_width=320,
        min_image_height=320,
        min_laplacian_variance=10,
        auto_approve_confidence=0.85,
    )
    first_engine = create_database_engine(url)
    initialize_database(first_engine)
    first_factory = create_session_factory(first_engine)
    with first_factory() as session:
        seed_catalog(session)
        student = User(
            username="restart",
            display_name="Restart",
            role=Role.STUDENT,
        )
        session.add(student)
        session.flush()
        activity = session.scalar(
            select(Activity).where(Activity.code == "duolingo_beconfident")
        )
        result = submit_evidence(
            session,
            actor=student,
            activity_id=activity.id,
            uploads=[UploadPayload("proof.png", make_png(72))],
            settings=settings,
            ocr_engine=FakeDuolingoOCR(),
        )
        session.commit()
        submission_id = result.submission_id
    first_engine.dispose()

    second_engine = create_database_engine(url)
    second_factory = create_session_factory(second_engine)
    with second_factory() as session:
        submission = session.get(Submission, submission_id)
        image = session.scalar(
            select(SubmissionImage).where(
                SubmissionImage.submission_id == submission_id
            )
        )
        assert submission is not None
        assert image is not None
        assert (upload_dir / image.storage_key).is_file()
    second_engine.dispose()


def test_legacy_email_variables_still_configure_the_new_username_settings(
    monkeypatch,
) -> None:
    """Ambientes já implantados não podem quebrar por causa do rename."""

    for name in (
        "BOOTSTRAP_ADMIN_NAME",
        "BOOTSTRAP_ADMIN_USERNAME",
        "BOOTSTRAP_ADMIN_EMAIL",
        "BOOTSTRAP_ADMIN_PASSWORD",
        "ALLOWED_USERNAMES",
        "ALLOWED_EMAILS",
        "ADMIN_USERNAMES",
        "ADMIN_EMAILS",
        "DEMO_STUDENT_USERNAME",
        "DEMO_STUDENT_EMAIL",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("DEMO_AUTH_ENABLED", "false")
    monkeypatch.setenv("LOCAL_AUTH_ENABLED", "true")
    monkeypatch.setenv("BOOTSTRAP_ADMIN_NAME", "Administrador")
    monkeypatch.setenv("BOOTSTRAP_ADMIN_EMAIL", "admin@equipe.org")
    monkeypatch.setenv("BOOTSTRAP_ADMIN_PASSWORD", "senha-inicial-forte-2026")
    monkeypatch.setenv("ALLOWED_EMAILS", "admin@equipe.org,aluno@equipe.org")
    monkeypatch.setenv("ADMIN_EMAILS", "admin@equipe.org")
    monkeypatch.setenv("DEMO_STUDENT_EMAIL", "aluno.legado")

    settings = Settings.from_env(env_file=None)

    assert settings.bootstrap_admin_username == "admin@equipe.org"
    assert settings.admin_usernames == frozenset({"admin@equipe.org"})
    assert settings.demo_student_username == "aluno.legado"

    # O nome novo tem precedência quando os dois estão definidos.
    monkeypatch.setenv("BOOTSTRAP_ADMIN_USERNAME", "admin")
    assert Settings.from_env(env_file=None).bootstrap_admin_username == "admin"


def test_missing_bootstrap_variable_is_named_in_the_error(monkeypatch) -> None:
    for name in (
        "BOOTSTRAP_ADMIN_USERNAME",
        "BOOTSTRAP_ADMIN_EMAIL",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BOOTSTRAP_ADMIN_NAME", "Administrador")
    monkeypatch.setenv("BOOTSTRAP_ADMIN_PASSWORD", "senha-inicial-forte-2026")

    with pytest.raises(ValueError) as error:
        Settings.from_env(env_file=None)

    message = str(error.value)
    assert "Faltando: BOOTSTRAP_ADMIN_USERNAME" in message
    assert "BOOTSTRAP_ADMIN_EMAIL" in message
