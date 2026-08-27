from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.exc import DatabaseError
from sqlalchemy.orm.exc import StaleDataError

from english_leaderboard.config import Settings
from english_leaderboard.database import (
    create_database_engine,
    create_session_factory,
    initialize_database,
)
from english_leaderboard.schema import (
    Activity,
    LedgerKind,
    LedgerTransaction,
    Role,
    Submission,
    SubmissionStatus,
    User,
    new_id,
)



def test_postgres_schema_is_never_created_from_the_orm(monkeypatch) -> None:
    """No PostgreSQL o schema pertence às migrations, não ao ORM.

    Um ``create_all`` produziria tabelas sem RLS, sem os gatilhos de
    imutabilidade do ledger e sem as restrições declaradas no SQL — parecendo
    certo e deixando os dados desprotegidos.
    """

    import english_leaderboard.database as db
    from english_leaderboard.schema import SchemaBase

    criadas: list[str] = []

    monkeypatch.setattr(
        SchemaBase.metadata,
        "create_all",
        lambda *a, **k: criadas.append("create_all"),
    )

    class EngineFalso:
        dialect = type("D", (), {"name": "postgresql"})()

    class InspetorFalso:
        def __init__(self, tabelas):
            self._tabelas = tabelas

        def get_table_names(self, schema=None):
            return self._tabelas

    # Banco vazio: a aplicação recusa subir e manda aplicar as migrations,
    # em vez de criar as tabelas por conta própria.
    monkeypatch.setattr(db, "inspect", lambda _engine: InspetorFalso([]))
    with pytest.raises(RuntimeError, match="apply_migrations"):
        db.initialize_database(EngineFalso())
    assert criadas == []

    # Banco já migrado: passa sem criar nada.
    monkeypatch.setattr(
        db, "inspect", lambda _engine: InspetorFalso(list(db.TABELAS_ESSENCIAIS))
    )
    db.initialize_database(EngineFalso())
    assert criadas == []


def test_sqlite_persists_after_engine_restart(tmp_path: Path):
    database = tmp_path / "persist.db"
    url = f"sqlite:///{database}"
    first_engine = create_database_engine(url)
    initialize_database(first_engine)
    first_factory = create_session_factory(first_engine)
    with first_factory() as session:
        session.add(
            User(id=new_id(), username="persist", display_name="Persist", role=Role.STUDENT)
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


def test_demo_auth_is_refused_in_production():
    settings = Settings(
        app_env="production",
        demo_auth_enabled=True,
    )
    with pytest.raises(RuntimeError):
        settings.validate()


def test_google_sheets_enabled_requires_spreadsheet_id(monkeypatch):
    monkeypatch.setenv("GOOGLE_SHEETS_AUTO_SYNC", "true")
    monkeypatch.delenv("GOOGLE_SHEETS_SPREADSHEET_ID", raising=False)

    with pytest.raises(ValueError, match="GOOGLE_SHEETS_SPREADSHEET_ID"):
        Settings.from_env(env_file=None)


def test_github_backup_is_refused_by_the_official_architecture():
    settings = Settings(
        github_backup_enabled=True,
        github_backup_repo="equipe/backups",
        github_backup_token="token-legado",
    )

    with pytest.raises(RuntimeError, match="Supabase Storage"):
        settings.validate()


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
        user = User(id=new_id(), username="ledger", display_name="Ledger", role=Role.STUDENT)
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
        user = User(id=new_id(), username="version", display_name="Version", role=Role.STUDENT)
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


def test_legacy_email_variables_still_configure_the_new_username_settings(
    monkeypatch,
) -> None:
    """Ambientes já implantados não podem quebrar por causa do rename."""

    for name in (
        "BOOTSTRAP_ADMIN_NAME",
        "BOOTSTRAP_ADMIN_USERNAME",
        "BOOTSTRAP_ADMIN_EMAIL",
        "BOOTSTRAP_ADMIN_PASSWORD",
        "DEMO_STUDENT_USERNAME",
        "DEMO_STUDENT_EMAIL",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("DEMO_AUTH_ENABLED", "false")
    # Produção exige o Supabase completo; aqui o que está sob teste é só o
    # nome das variáveis antigas continuar valendo.
    monkeypatch.setenv("SUPABASE_URL", "https://ref.supabase.co")
    monkeypatch.setenv("SUPABASE_PUBLISHABLE_KEY", "sb_publishable_x")
    monkeypatch.setenv(
        "SUPABASE_DB_URL",
        "postgresql+psycopg://postgres.ref:senha"
        "@aws-0-sa-east-1.pooler.supabase.com:5432/postgres",
    )
    monkeypatch.setenv("BOOTSTRAP_ADMIN_NAME", "Administrador")
    monkeypatch.setenv("BOOTSTRAP_ADMIN_EMAIL", "admin@equipe.org")
    monkeypatch.setenv("BOOTSTRAP_ADMIN_PASSWORD", "senha-inicial-forte-2026")
    monkeypatch.setenv("DEMO_STUDENT_EMAIL", "aluno.legado")

    settings = Settings.from_env(env_file=None)

    assert settings.bootstrap_admin_username == "admin@equipe.org"
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
