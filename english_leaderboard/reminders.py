"""Configurable, idempotent SMTP reminders with a safe dry-run mode."""

from __future__ import annotations

from datetime import datetime, timedelta
from email.message import EmailMessage
import smtplib
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .authz import require_admin
from .config import Settings
from .models import (
    EmailAttempt,
    EmailAttemptStatus,
    ReminderConfiguration,
    Role,
    Submission,
    SubmissionStatus,
    User,
    utcnow,
)
from .services import add_audit


APPROVED_STATUSES = (
    SubmissionStatus.APPROVED_AUTO,
    SubmissionStatus.APPROVED_MANUAL,
)


def get_reminder_configuration(session: Session) -> ReminderConfiguration:
    configuration = session.get(ReminderConfiguration, 1)
    if configuration is None:
        configuration = ReminderConfiguration(id=1, enabled=False)
        session.add(configuration)
        session.flush()
    return configuration


def save_reminder_configuration(
    session: Session,
    *,
    actor: User,
    enabled: bool,
    frequency: str,
    weekday: int,
    send_hour: int,
    timezone_name: str,
    inactive_days: int,
    subject_template: str,
    body_template: str,
    audience: str = "inactive_students",
) -> ReminderConfiguration:
    require_admin(actor)
    if frequency not in {"daily", "weekly"}:
        raise ValueError("Frequência deve ser diária ou semanal")
    if not 0 <= weekday <= 6 or not 0 <= send_hour <= 23:
        raise ValueError("Dia da semana ou horário inválido")
    if inactive_days < 1:
        raise ValueError("Dias sem atividade deve ser positivo")
    if audience not in {"inactive_students", "never_approved", "previously_active"}:
        raise ValueError("Público-alvo inválido")
    try:
        ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as error:
        raise ValueError("Fuso horário inválido") from error
    if "{name}" not in body_template:
        raise ValueError("O modelo da mensagem deve conter {name}")
    configuration = get_reminder_configuration(session)
    before = {
        "enabled": configuration.enabled,
        "frequency": configuration.frequency,
        "weekday": configuration.weekday,
        "send_hour": configuration.send_hour,
        "inactive_days": configuration.inactive_days,
    }
    configuration.enabled = bool(enabled)
    configuration.frequency = frequency
    configuration.weekday = int(weekday)
    configuration.send_hour = int(send_hour)
    configuration.timezone_name = timezone_name
    configuration.inactive_days = int(inactive_days)
    configuration.subject_template = subject_template.strip()
    configuration.body_template = body_template.strip()
    configuration.audience = audience
    configuration.updated_by_id = actor.id
    add_audit(
        session,
        actor_id=actor.id,
        action="reminder_configuration_updated",
        entity_type="reminder_configuration",
        entity_id="1",
        before=before,
        after={
            "enabled": configuration.enabled,
            "frequency": configuration.frequency,
            "weekday": configuration.weekday,
            "send_hour": configuration.send_hour,
            "inactive_days": configuration.inactive_days,
        },
    )
    return configuration


def render_reminder(configuration: ReminderConfiguration, user: User) -> tuple[str, str]:
    values = {"name": user.display_name, "email": user.email}
    try:
        return (
            configuration.subject_template.format_map(values),
            configuration.body_template.format_map(values),
        )
    except (KeyError, ValueError) as error:
        raise ValueError("Modelo de lembrete contém variável inválida") from error


def _period_key(configuration: ReminderConfiguration, local_now: datetime) -> str:
    if configuration.frequency == "daily":
        return local_now.strftime("%Y-%m-%d")
    iso_year, iso_week, _ = local_now.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


def reminder_is_due(configuration: ReminderConfiguration, now: datetime) -> bool:
    if not configuration.enabled:
        return False
    zone = ZoneInfo(configuration.timezone_name)
    local_now = now.astimezone(zone)
    if local_now.hour != configuration.send_hour:
        return False
    return configuration.frequency == "daily" or local_now.weekday() == configuration.weekday


def eligible_students(
    session: Session,
    configuration: ReminderConfiguration,
    *,
    now: datetime,
) -> list[User]:
    cutoff = now - timedelta(days=configuration.inactive_days)
    last_approved = (
        select(
            Submission.student_id,
            func.max(Submission.decided_at).label("last_approved_at"),
        )
        .where(Submission.status.in_(APPROVED_STATUSES))
        .group_by(Submission.student_id)
        .subquery()
    )
    statement = (
        select(User)
        .outerjoin(last_approved, last_approved.c.student_id == User.id)
        .where(
            User.role == Role.STUDENT,
            User.active.is_(True),
            User.archived_at.is_(None),
            User.reminders_enabled.is_(True),
            func.coalesce(last_approved.c.last_approved_at, User.created_at) < cutoff,
        )
        .order_by(User.display_name)
    )
    if configuration.audience == "never_approved":
        statement = statement.where(last_approved.c.last_approved_at.is_(None))
    elif configuration.audience == "previously_active":
        statement = statement.where(last_approved.c.last_approved_at.is_not(None))
    return list(session.scalars(statement).all())


def _deliver_smtp(settings: Settings, recipient: str, subject: str, body: str) -> None:
    if not settings.smtp_host or not settings.smtp_from_email:
        raise RuntimeError("SMTP não configurado")
    message = EmailMessage()
    message["From"] = f"{settings.smtp_from_name} <{settings.smtp_from_email}>"
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(body)
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as client:
        if settings.smtp_use_tls:
            client.starttls()
        if settings.smtp_username:
            client.login(settings.smtp_username, settings.smtp_password)
        client.send_message(message)


def create_and_send_attempt(
    session: Session,
    *,
    user: User,
    configuration: ReminderConfiguration,
    settings: Settings,
    scheduled_for: datetime,
    dedupe_key: str,
) -> EmailAttempt:
    existing = session.scalar(
        select(EmailAttempt).where(EmailAttempt.dedupe_key == dedupe_key)
    )
    if existing is not None:
        return existing
    subject, body = render_reminder(configuration, user)
    attempt = EmailAttempt(
        user_id=user.id,
        recipient_email=user.email,
        subject=subject,
        body=body,
        status=EmailAttemptStatus.PENDING,
        dedupe_key=dedupe_key,
        dry_run=settings.reminder_dry_run,
        scheduled_for=scheduled_for,
    )
    session.add(attempt)
    session.flush()
    if settings.reminder_dry_run:
        attempt.status = EmailAttemptStatus.SKIPPED
        attempt.last_error = "dry-run: mensagem não enviada"
        attempt.attempt_count = 0
        return attempt
    for retry in range(1, 4):
        attempt.attempt_count = retry
        try:
            _deliver_smtp(settings, user.email, subject, body)
        except (OSError, smtplib.SMTPException) as error:
            attempt.last_error = str(error)[:1000]
            if retry < 3:
                time.sleep(0.2 * retry)
                continue
            attempt.status = EmailAttemptStatus.FAILED
        else:
            attempt.status = EmailAttemptStatus.SENT
            attempt.sent_at = utcnow()
            attempt.last_error = None
        break
    return attempt


def run_due_reminders(
    session: Session,
    settings: Settings,
    *,
    now: datetime | None = None,
    force: bool = False,
) -> list[EmailAttempt]:
    now = now or utcnow()
    configuration = get_reminder_configuration(session)
    if not force and not reminder_is_due(configuration, now):
        return []
    zone = ZoneInfo(configuration.timezone_name)
    period_key = _period_key(configuration, now.astimezone(zone))
    attempts: list[EmailAttempt] = []
    for user in eligible_students(session, configuration, now=now):
        attempts.append(
            create_and_send_attempt(
                session,
                user=user,
                configuration=configuration,
                settings=settings,
                scheduled_for=now,
                dedupe_key=f"reminder:{user.id}:{period_key}",
            )
        )
    return attempts


def send_test_reminder(
    session: Session,
    *,
    actor: User,
    settings: Settings,
) -> EmailAttempt:
    require_admin(actor)
    configuration = get_reminder_configuration(session)
    now = utcnow()
    return create_and_send_attempt(
        session,
        user=actor,
        configuration=configuration,
        settings=settings,
        scheduled_for=now,
        dedupe_key=f"reminder-test:{actor.id}:{now.isoformat()}",
    )


__all__ = [
    "create_and_send_attempt",
    "eligible_students",
    "get_reminder_configuration",
    "reminder_is_due",
    "render_reminder",
    "run_due_reminders",
    "save_reminder_configuration",
    "send_test_reminder",
]
