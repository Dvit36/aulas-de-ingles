from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

from sqlalchemy import func, select

from english_leaderboard.models import EmailAttempt, EmailAttemptStatus, Role, utcnow
from english_leaderboard.reminders import (
    eligible_students,
    get_reminder_configuration,
    run_due_reminders,
    save_reminder_configuration,
)
from english_leaderboard.scheduler import run_once


def test_reminders_are_disabled_by_default_and_dry_run_is_idempotent(
    session, users, settings, monkeypatch
) -> None:
    configuration = get_reminder_configuration(session)
    assert configuration.enabled is False
    student = users[Role.STUDENT]
    student.created_at = utcnow() - timedelta(days=30)
    save_reminder_configuration(
        session,
        actor=users[Role.ADMIN],
        enabled=True,
        frequency="daily",
        weekday=0,
        send_hour=9,
        timezone_name="America/Sao_Paulo",
        inactive_days=7,
        subject_template="Lembrete para {name}",
        body_template="Olá, {name}! Acesse a plataforma.",
    )
    session.commit()
    monkeypatch.setattr(
        "english_leaderboard.reminders._deliver_smtp",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("dry-run não pode chamar SMTP")
        ),
    )
    now = utcnow().replace(minute=0, second=0, microsecond=0)
    first = run_due_reminders(session, settings, now=now, force=True)
    session.commit()
    second = run_due_reminders(session, settings, now=now, force=True)
    session.commit()
    assert len(first) == 1
    assert len(second) == 1
    assert second[0].id == first[0].id
    assert first[0].status == EmailAttemptStatus.SKIPPED
    assert session.scalar(select(func.count(EmailAttempt.id))) == 1


def test_independent_scheduler_can_run_with_reminders_disabled(
    settings, tmp_path
) -> None:
    scheduler_settings = replace(
        settings,
        database_url=f"sqlite:///{tmp_path / 'scheduler.db'}",
        upload_dir=tmp_path / "scheduler-uploads",
        reminder_dry_run=True,
    )
    assert run_once(scheduler_settings) == 0
    assert run_once(scheduler_settings) == 0


def test_reminder_audience_can_target_students_who_never_participated(
    session, users
) -> None:
    student = users[Role.STUDENT]
    student.created_at = utcnow() - timedelta(days=30)
    configuration = save_reminder_configuration(
        session,
        actor=users[Role.ADMIN],
        enabled=True,
        frequency="weekly",
        weekday=0,
        send_hour=9,
        timezone_name="America/Sao_Paulo",
        inactive_days=7,
        subject_template="Olá, {name}",
        body_template="Olá, {name}! Volte para a plataforma.",
        audience="never_approved",
    )
    assert eligible_students(session, configuration, now=utcnow()) == [student]
    configuration.audience = "previously_active"
    assert eligible_students(session, configuration, now=utcnow()) == []
