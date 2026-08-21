"""Small, repeatable schema migrations for the deployed SQLite database.

The project intentionally keeps migrations in-process because it is a single-node
internal deployment.  Every migration is additive and recorded in
``schema_migrations``; existing rows and uploads are never rewritten or deleted.
"""

from __future__ import annotations

from sqlalchemy import Engine, inspect, text


SQLITE_USER_COLUMNS: dict[str, str] = {
    "archived_at": "DATETIME",
    "password_hash": "VARCHAR(500)",
    "must_change_password": "BOOLEAN NOT NULL DEFAULT 0",
    "session_version": "INTEGER NOT NULL DEFAULT 1",
    "last_login_at": "DATETIME",
    "failed_login_attempts": "INTEGER NOT NULL DEFAULT 0",
    "locked_until": "DATETIME",
    "reminders_enabled": "BOOLEAN NOT NULL DEFAULT 1",
}

SQLITE_ACTIVITY_COLUMNS: dict[str, str] = {
    "archived_at": "DATETIME",
}


def _add_missing_columns(
    engine: Engine, table_name: str, definitions: dict[str, str]
) -> None:
    existing = {column["name"] for column in inspect(engine).get_columns(table_name)}
    with engine.begin() as connection:
        for name, definition in definitions.items():
            if name not in existing:
                connection.execute(
                    text(f'ALTER TABLE "{table_name}" ADD COLUMN "{name}" {definition}')
                )


def _rename_email_to_username(engine: Engine) -> None:
    """Renomeia ``users.email`` para ``users.username`` preservando as linhas.

    O login passou a usar nome de usuário. Bancos já implantados guardam os
    identificadores na coluna antiga; o RENAME mantém os valores e as contas
    continuam entrando com o que já usavam. Bancos novos já nascem com
    ``username`` e esta migração não faz nada.
    """

    columns = {column["name"] for column in inspect(engine).get_columns("users")}
    if "username" in columns or "email" not in columns:
        return
    with engine.begin() as connection:
        connection.execute(text('ALTER TABLE "users" RENAME COLUMN "email" TO "username"'))
        if engine.dialect.name == "sqlite":
            connection.execute(text('DROP INDEX IF EXISTS "ix_users_email"'))
            connection.execute(
                text('CREATE UNIQUE INDEX IF NOT EXISTS "ix_users_username" ON "users" ("username")')
            )


def apply_migrations(engine: Engine) -> None:
    """Apply additive migrations after ``metadata.create_all``.

    Fresh databases already contain every column, while deployed SQLite files get
    only the missing columns.  New tables are handled by SQLAlchemy ``create_all``.
    """

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    description VARCHAR(255) NOT NULL,
                    applied_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )

    _rename_email_to_username(engine)

    if engine.dialect.name == "sqlite":
        _add_missing_columns(engine, "users", SQLITE_USER_COLUMNS)
        _add_missing_columns(engine, "activities", SQLITE_ACTIVITY_COLUMNS)
    else:  # pragma: no cover - production target remains SQLite
        with engine.begin() as connection:
            for table_name, definitions in (
                ("users", SQLITE_USER_COLUMNS),
                ("activities", SQLITE_ACTIVITY_COLUMNS),
            ):
                for name, definition in definitions.items():
                    connection.execute(
                        text(
                            f'ALTER TABLE "{table_name}" ADD COLUMN IF NOT EXISTS '
                            f'"{name}" {definition}'
                        )
                    )

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO schema_migrations(version, description)
                SELECT 1, 'local authentication, generic files and reminders'
                WHERE NOT EXISTS (
                    SELECT 1 FROM schema_migrations WHERE version = 1
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO schema_migrations(version, description)
                SELECT 2, 'login identity moved from email to username'
                WHERE NOT EXISTS (
                    SELECT 1 FROM schema_migrations WHERE version = 2
                )
                """
            )
        )


__all__ = ["apply_migrations"]
