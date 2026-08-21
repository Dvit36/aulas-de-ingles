from __future__ import annotations

from sqlalchemy import inspect, text

from english_leaderboard.database import create_database_engine, initialize_database


def test_migrations_are_repeatable_and_recorded(tmp_path) -> None:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'migration.db'}")
    initialize_database(engine)
    initialize_database(engine)
    inspector = inspect(engine)
    assert "schema_migrations" in inspector.get_table_names()
    user_columns = {column["name"] for column in inspector.get_columns("users")}
    assert {"password_hash", "session_version", "archived_at"} <= user_columns
    with engine.connect() as connection:
        versions = connection.execute(
            text("SELECT version FROM schema_migrations")
        ).scalars().all()
    assert versions == [1]
    engine.dispose()


def test_migration_preserves_rows_from_existing_schema(tmp_path) -> None:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE users (
                    id VARCHAR(36) PRIMARY KEY,
                    email VARCHAR(320) NOT NULL UNIQUE,
                    display_name VARCHAR(160) NOT NULL,
                    role VARCHAR(16) NOT NULL,
                    active BOOLEAN NOT NULL,
                    oidc_issuer VARCHAR(500),
                    oidc_subject VARCHAR(500),
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE activities (
                    id VARCHAR(36) PRIMARY KEY,
                    code VARCHAR(80) NOT NULL UNIQUE,
                    name VARCHAR(180) NOT NULL,
                    points INTEGER NOT NULL,
                    unit_threshold INTEGER NOT NULL,
                    requires_images BOOLEAN NOT NULL,
                    requires_summary BOOLEAN NOT NULL,
                    requires_title_or_url BOOLEAN NOT NULL,
                    summary_min_chars INTEGER NOT NULL,
                    content_review_required BOOLEAN NOT NULL,
                    auto_approvable BOOLEAN NOT NULL,
                    active BOOLEAN NOT NULL,
                    config_json JSON NOT NULL,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO users(
                    id, email, display_name, role, active, created_at, updated_at
                ) VALUES (
                    'legacy-user', 'legacy@example.org', 'Usuário legado',
                    'STUDENT', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO activities(
                    id, code, name, points, unit_threshold, requires_images,
                    requires_summary, requires_title_or_url, summary_min_chars,
                    content_review_required, auto_approvable, active,
                    config_json, created_at, updated_at
                ) VALUES (
                    'legacy-activity', 'legacy', 'Atividade legada', 10, 1,
                    1, 0, 0, 0, 0, 0, 1, '{}', CURRENT_TIMESTAMP,
                    CURRENT_TIMESTAMP
                )
                """
            )
        )

    initialize_database(engine)
    initialize_database(engine)

    with engine.connect() as connection:
        user = connection.execute(
            text(
                "SELECT display_name, active, password_hash, session_version "
                "FROM users WHERE id = 'legacy-user'"
            )
        ).one()
        activity = connection.execute(
            text(
                "SELECT name, points, archived_at FROM activities "
                "WHERE id = 'legacy-activity'"
            )
        ).one()
    assert tuple(user) == ("Usuário legado", 1, None, 1)
    assert tuple(activity) == ("Atividade legada", 10, None)
    engine.dispose()
