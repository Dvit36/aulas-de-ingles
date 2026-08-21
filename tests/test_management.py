from __future__ import annotations

from inspect import signature

import pytest
from sqlalchemy import select

from english_leaderboard.models import Activity, Role, Submission, SubmissionStatus
from english_leaderboard.services import (
    archive_or_delete_activity,
    archive_or_delete_user,
    count_activity_references,
    create_activity,
    create_points_adjustment,
    create_user_account,
    reset_user_password,
    review_submission,
    save_activity_changes,
    save_user,
    set_activity_active,
)


def test_administrative_operations_do_not_accept_reason_parameters() -> None:
    for operation in (
        review_submission,
        save_activity_changes,
        save_user,
        reset_user_password,
        archive_or_delete_user,
        archive_or_delete_activity,
        create_activity,
        create_points_adjustment,
        create_user_account,
    ):
        assert "reason" not in signature(operation).parameters


def test_user_create_disable_reactivate_and_delete(session, users) -> None:
    admin = users[Role.ADMIN]
    account, temporary_password = create_user_account(
        session,
        actor=admin,
        username="new.student",
        display_name="New Student",
    )
    session.commit()
    assert account.must_change_password is True
    assert account.password_hash and temporary_password not in account.password_hash

    save_user(
        session,
        actor=admin,
        username=account.username,
        display_name=account.display_name,
        role=Role.STUDENT,
        active=False,
        user_id=account.id,
    )
    assert account.active is False
    save_user(
        session,
        actor=admin,
        username=account.username,
        display_name=account.display_name,
        role=Role.STUDENT,
        active=True,
        user_id=account.id,
    )
    assert account.active is True
    assert (
        archive_or_delete_user(
            session,
            actor=admin,
            user_id=account.id,
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
    set_activity_active(
        session,
        actor=admin,
        activity_id=activity.id,
        active=False,
    )
    assert activity.active is False
    set_activity_active(
        session,
        actor=admin,
        activity_id=activity.id,
        active=True,
    )
    assert activity.active is True
    assert (
        archive_or_delete_activity(
            session,
            actor=admin,
            activity_id=activity.id,
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
        )


def test_core_lesson_activity_cannot_be_deleted(session, users) -> None:
    activity = session.scalar(
        select(Activity).where(Activity.code == "duolingo_beconfident")
    )
    with pytest.raises(ValueError, match="não pode ser excluída"):
        archive_or_delete_activity(
            session,
            actor=users[Role.ADMIN],
            activity_id=activity.id,
        )


def test_activity_reference_count_drives_the_delete_confirmation(
    session, users
) -> None:
    admin = users[Role.ADMIN]
    student = users[Role.STUDENT]
    unused = create_activity(
        session,
        actor=admin,
        code="unused_activity",
        name="Atividade sem uso",
        points=8,
    )
    used = create_activity(
        session,
        actor=admin,
        code="used_activity",
        name="Atividade com histórico",
        points=8,
    )
    session.flush()
    session.add(
        Submission(
            student_id=student.id,
            activity_id=used.id,
            status=SubmissionStatus.REJECTED,
            rule_snapshot_json={"activity_name": used.name, "points": 8},
        )
    )
    session.commit()
    assert count_activity_references(session, unused.id) == 0
    assert count_activity_references(session, used.id) == 1


def test_weekly_goal_is_admin_only_validated_and_audited(session, users) -> None:
    from english_leaderboard.authz import AuthorizationError
    from english_leaderboard.models import AuditLog
    from english_leaderboard.services import (
        get_goal_configuration,
        save_goal_configuration,
    )

    admin = users[Role.ADMIN]
    student = users[Role.STUDENT]

    assert get_goal_configuration(session).weekly_lesson_goal == 5

    with pytest.raises(AuthorizationError):
        save_goal_configuration(session, actor=student, weekly_lesson_goal=10)
    session.rollback()

    for invalid in (0, -3, 201):
        with pytest.raises(ValueError):
            save_goal_configuration(session, actor=admin, weekly_lesson_goal=invalid)
        session.rollback()

    configuration = save_goal_configuration(
        session, actor=admin, weekly_lesson_goal=12
    )
    session.commit()

    assert configuration.weekly_lesson_goal == 12
    assert configuration.updated_by_id == admin.id
    assert get_goal_configuration(session).weekly_lesson_goal == 12
    entry = session.scalar(
        select(AuditLog).where(AuditLog.action == "goal_configuration_updated")
    )
    assert entry is not None
    assert entry.before_json == {"weekly_lesson_goal": 5}
    assert entry.after_json == {"weekly_lesson_goal": 12}


def test_resources_reject_dangerous_links_and_keep_admin_order(session, users) -> None:
    from english_leaderboard.authz import AuthorizationError
    from english_leaderboard.services import (
        list_resources,
        normalize_resource_url,
        replace_resources,
    )

    admin = users[Role.ADMIN]
    student = users[Role.STUDENT]

    # A lista é renderizada como HTML: só http/https podem passar.
    for hostile in ("javascript:alert(1)", "data:text/html,<script>", "  ", "ftp://x"):
        with pytest.raises(ValueError):
            normalize_resource_url(hostile)

    with pytest.raises(AuthorizationError):
        replace_resources(session, actor=student, entries=[])
    session.rollback()

    with pytest.raises(ValueError):
        replace_resources(
            session,
            actor=admin,
            entries=[{"title": "Sem link", "url": ""}],
        )
    session.rollback()

    with pytest.raises(ValueError):
        replace_resources(
            session,
            actor=admin,
            entries=[{"title": "", "url": "https://exemplo.org"}],
        )
    session.rollback()

    replace_resources(
        session,
        actor=admin,
        entries=[
            {"title": "Segundo", "url": "https://b.example", "description": "b"},
            {"title": "Primeiro", "url": "https://a.example", "description": "a"},
            {"title": "Oculto", "url": "https://c.example", "active": False},
        ],
    )
    session.commit()

    # A ordem recebida vira a posição; nada é reordenado por título.
    ativos = list_resources(session)
    assert [item.title for item in ativos] == ["Segundo", "Primeiro"]
    assert [item.position for item in ativos] == [1, 2]
    todos = list_resources(session, include_inactive=True)
    assert [item.title for item in todos] == ["Segundo", "Primeiro", "Oculto"]


def test_replacing_resources_is_a_full_rewrite(session, users) -> None:
    from english_leaderboard.models import AuditLog
    from english_leaderboard.services import list_resources, replace_resources

    admin = users[Role.ADMIN]
    replace_resources(
        session,
        actor=admin,
        entries=[{"title": "Antigo", "url": "https://antigo.example"}],
    )
    session.commit()
    replace_resources(
        session,
        actor=admin,
        entries=[{"title": "Novo", "url": "https://novo.example"}],
    )
    session.commit()

    assert [item.title for item in list_resources(session)] == ["Novo"]
    entry = session.scalar(
        select(AuditLog)
        .where(AuditLog.action == "resources_replaced")
        .order_by(AuditLog.created_at.desc())
    )
    assert entry is not None
    assert entry.after_json["titles"] == ["Novo"]
