from __future__ import annotations

import pytest
from sqlalchemy import select

from english_leaderboard.models import Activity, Role, Submission, SubmissionStatus
from english_leaderboard.services import (
    archive_or_delete_activity,
    archive_or_delete_user,
    create_activity,
    create_user_account,
    save_activity_changes,
    save_user,
)


def test_user_create_disable_reactivate_and_delete(session, users) -> None:
    admin = users[Role.ADMIN]
    account, temporary_password = create_user_account(
        session,
        actor=admin,
        email="new@example.org",
        display_name="New Student",
        reason="Novo integrante",
    )
    session.commit()
    assert account.must_change_password is True
    assert account.password_hash and temporary_password not in account.password_hash

    save_user(
        session,
        actor=admin,
        email=account.email,
        display_name=account.display_name,
        role=Role.STUDENT,
        active=False,
        user_id=account.id,
        reason="Pausa temporária",
    )
    assert account.active is False
    save_user(
        session,
        actor=admin,
        email=account.email,
        display_name=account.display_name,
        role=Role.STUDENT,
        active=True,
        user_id=account.id,
        reason="Retorno à equipe",
    )
    assert account.active is True
    assert (
        archive_or_delete_user(
            session,
            actor=admin,
            user_id=account.id,
            reason="Cadastro criado para teste",
        )
        == "deleted"
    )


def test_activity_delete_is_logical_when_history_exists(session, users) -> None:
    admin = users[Role.ADMIN]
    student = users[Role.STUDENT]
    activity = create_activity(
        session,
        actor=admin,
        code="custom_activity",
        name="Atividade personalizada",
        points=9,
    )
    session.flush()
    submission = Submission(
        student_id=student.id,
        activity_id=activity.id,
        status=SubmissionStatus.REJECTED,
        rule_snapshot_json={"activity_name": activity.name, "points": 9},
    )
    session.add(submission)
    session.commit()
    result = archive_or_delete_activity(
        session,
        actor=admin,
        activity_id=activity.id,
        reason="Atividade encerrada",
    )
    session.commit()
    assert result == "archived"
    assert activity.active is False
    assert activity.archived_at is not None
    historical = session.get(Submission, submission.id)
    assert historical.activity.name == "Atividade personalizada"


def test_unused_activity_can_be_deleted_and_inactive_can_be_reactivated(
    session, users
) -> None:
    admin = users[Role.ADMIN]
    activity = create_activity(
        session,
        actor=admin,
        code="temporary_activity",
        name="Atividade temporária",
        points=8,
    )
    save_activity_changes(
        session,
        actor=admin,
        activity_id=activity.id,
        name=activity.name,
        points=activity.points,
        active=False,
        reason="Pausa",
    )
    assert activity.active is False
    save_activity_changes(
        session,
        actor=admin,
        activity_id=activity.id,
        name=activity.name,
        points=activity.points,
        active=True,
        reason="Retomada",
    )
    assert activity.active is True
    assert (
        archive_or_delete_activity(
            session,
            actor=admin,
            activity_id=activity.id,
            reason="Não será utilizada",
        )
        == "deleted"
    )
    session.flush()
    assert session.get(Activity, activity.id) is None


def test_core_lesson_points_and_threshold_are_not_editable(session, users) -> None:
    activity = session.scalar(
        select(Activity).where(Activity.code == "duolingo_beconfident")
    )
    with pytest.raises(ValueError, match="política fixa"):
        save_activity_changes(
            session,
            actor=users[Role.ADMIN],
            activity_id=activity.id,
            name=activity.name,
            points=99,
            unit_threshold=1,
            active=True,
            reason="Tentativa inválida",
        )
