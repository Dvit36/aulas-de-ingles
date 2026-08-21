from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from pathlib import Path

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool


class Base(DeclarativeBase):
    pass


def create_database_engine(database_url: str, **kwargs: Any) -> Engine:
    options: dict[str, Any] = {"future": True, "pool_pre_ping": True, **kwargs}
    if database_url.startswith("sqlite"):
        options.setdefault("connect_args", {"check_same_thread": False, "timeout": 30})
        if ":memory:" in database_url:
            options.setdefault("poolclass", StaticPool)
    engine = create_engine(database_url, **options)

    if engine.dialect.name == "sqlite":

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_connection: Any, _: Any) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=30000")
            if ":memory:" not in database_url:
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.close()

    return engine


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


def initialize_database(engine: Engine) -> None:
    from . import models  # noqa: F401 - registers tables on Base.metadata
    from .migrations import apply_migrations

    Base.metadata.create_all(engine)
    apply_migrations(engine)
    if engine.dialect.name == "sqlite":
        # Ledger corrections must be compensating INSERTs, never silent mutation.
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    CREATE TRIGGER IF NOT EXISTS ledger_transactions_no_update
                    BEFORE UPDATE ON ledger_transactions
                    BEGIN
                        SELECT RAISE(ABORT, 'ledger transactions are immutable');
                    END
                    """
                )
            )
            connection.execute(
                text(
                    """
                    CREATE TRIGGER IF NOT EXISTS ledger_transactions_no_delete
                    BEFORE DELETE ON ledger_transactions
                    BEGIN
                        SELECT RAISE(ABORT, 'ledger transactions are immutable');
                    END
                    """
                )
            )


def _cleanup_pending_uploads(session: Session) -> None:
    for path in session.info.pop("created_upload_paths", []):
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            pass


@event.listens_for(Session, "after_rollback")
def _remove_uploads_after_rollback(session: Session) -> None:
    _cleanup_pending_uploads(session)


@event.listens_for(Session, "after_commit")
def _forget_uploads_after_commit(session: Session) -> None:
    session.info.pop("created_upload_paths", None)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
