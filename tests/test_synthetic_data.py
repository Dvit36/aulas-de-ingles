from __future__ import annotations

from dataclasses import replace

from sqlalchemy import func, select

from english_leaderboard.catalog import seed_catalog, seed_database
from english_leaderboard.models import (
    LedgerTransaction,
    Role,
    Submission,
    User,
)
from english_leaderboard.scoring import leaderboard_rows
from english_leaderboard.synthetic_data import (
    SYNTHETIC_STUDENTS,
    seed_fake_students,
)


def test_fake_student_seed_is_complete_and_idempotent(session) -> None:
    seed_catalog(session)

    first = seed_fake_students(session)
    session.commit()
    second = seed_fake_students(session)
    session.commit()

    usernames = [student.username for student in SYNTHETIC_STUDENTS]
    fake_users = list(
        session.scalars(select(User).where(User.username.in_(usernames))).all()
    )
    assert first.users_created == 5
    assert first.submissions_created == 52
    assert first.ledger_transactions_created == 27
    assert second.users_created == 0
    assert second.submissions_created == 0
    assert second.ledger_transactions_created == 0
    assert len(fake_users) == 5
    assert all(user.role == Role.STUDENT for user in fake_users)
    assert all(user.reminders_enabled is False for user in fake_users)
    assert session.scalar(select(func.count(Submission.id))) == 52
    assert session.scalar(select(func.count(LedgerTransaction.id))) == 27

    board = [
        row for row in leaderboard_rows(session) if str(row["student"]).endswith("(Demo)")
    ]
    assert [(row["student"], row["points"]) for row in board] == [
        ("Ana Souza (Demo)", 85),
        ("Bruno Lima (Demo)", 77),
        ("Carla Mendes (Demo)", 70),
        ("Diego Rocha (Demo)", 69),
        ("Elisa Martins (Demo)", 59),
    ]


def test_fake_submissions_are_explicitly_marked(session) -> None:
    seed_catalog(session)
    seed_fake_students(session)

    submissions = list(session.scalars(select(Submission)).all())
    assert submissions
    assert all(item.rule_snapshot_json["synthetic"] is True for item in submissions)
    assert all(item.rule_snapshot_json["seed"] == "fake_students_v1" for item in submissions)


def test_database_seed_can_disable_fake_students(session, settings) -> None:
    seed_database(session, replace(settings, seed_fake_data=False))

    fake_usernames = [student.username for student in SYNTHETIC_STUDENTS]
    assert (
        session.scalar(
            select(func.count(User.id)).where(User.username.in_(fake_usernames))
        )
        == 0
    )
