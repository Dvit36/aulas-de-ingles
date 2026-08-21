"""Independent reminder scheduler process used by Docker or cron."""

from __future__ import annotations

import logging
import time

from .catalog import seed_database
from .config import Settings
from .database import (
    create_database_engine,
    create_session_factory,
    initialize_database,
    session_scope,
)
from .reminders import run_due_reminders


LOGGER = logging.getLogger("english_leaderboard.scheduler")


def run_once(settings: Settings | None = None) -> int:
    settings = settings or Settings.from_env()
    settings.ensure_directories()
    engine = create_database_engine(settings.database_url)
    initialize_database(engine)
    factory = create_session_factory(engine)
    with session_scope(factory) as session:
        seed_database(session, settings)
        attempts = run_due_reminders(session, settings)
        return len(attempts)


def run_forever(settings: Settings | None = None) -> None:
    settings = settings or Settings.from_env()
    while True:
        try:
            count = run_once(settings)
            LOGGER.info("Ciclo de lembretes concluído: %s tentativa(s)", count)
        except Exception:
            LOGGER.exception("Falha no ciclo de lembretes")
        time.sleep(settings.reminder_scheduler_interval_seconds)


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.INFO)
    run_forever()
