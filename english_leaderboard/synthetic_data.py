"""Deterministic synthetic students and activity history for the demo app."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID, uuid5

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import Activity, Role, Submission, SubmissionStatus, User, utcnow
from .scoring import award_approved_submission

_SEED_VERSION = "fake_students_v1"
_SEED_NAMESPACE = UUID("8a393fb8-f845-4a0c-90a8-6ea3cc909661")


@dataclass(frozen=True)
class SyntheticStudent:
    name: str
    username: str
    activity_codes: tuple[str, ...]


@dataclass(frozen=True)
class SyntheticSeedReport:
    users_created: int = 0
    submissions_created: int = 0
    ledger_transactions_created: int = 0


SYNTHETIC_STUDENTS: tuple[SyntheticStudent, ...] = (
    SyntheticStudent(
        "Ana Souza (Demo)",
        "ana.souza.demo",
        (
            "write_improve_business",
            "cambridge_proficient",
            "write_improve_advanced",
            "cambridge_independent",
        ),
    ),
    SyntheticStudent(
        "Bruno Lima (Demo)",
        "bruno.lima.demo",
        (
            "write_improve_business",
            "cambridge_proficient",
            "cambridge_independent",
            "youtube_lesson_notes",
        ),
    ),
    SyntheticStudent(
        "Carla Mendes (Demo)",
        "carla.mendes.demo",
        (
            "write_improve_advanced",
            "cambridge_proficient",
            "cambridge_independent",
            "cambridge_basic",
        ),
    ),
    SyntheticStudent(
        "Diego Rocha (Demo)",
        "diego.rocha.demo",
        (
            "write_improve_advanced",
            "cambridge_independent",
            "youtube_lesson_notes",
            "cambridge_basic",
            "write_improve_fun",
        ),
    ),
    SyntheticStudent(
        "Elisa Martins (Demo)",
        "elisa.martins.demo",
        (
            "cambridge_independent",
            "youtube_lesson_notes",
            "impact_summary",
            "video_fun_summary",
            "write_improve_fun",
        ),
    ),
)


def _seed_id(kind: str, key: str) -> str:
    return str(uuid5(_SEED_NAMESPACE, f"{_SEED_VERSION}:{kind}:{key}"))


def _submission_id(username: str, slot: str) -> str:
    return _seed_id("submission", f"{username}:{slot}")


def _expected_submission_ids() -> tuple[str, ...]:
    identifiers: list[str] = []
    for student in SYNTHETIC_STUDENTS:
        identifiers.extend(
            _submission_id(student.username, f"activity:{code}")
            for code in student.activity_codes
        )
        identifiers.extend(
            _submission_id(student.username, f"lesson:{index}")
            for index in range(1, 6)
        )
        identifiers.append(_submission_id(student.username, "rejected"))
    return tuple(identifiers)


def _synthetic_summary(student_name: str, activity_name: str) -> str:
    return (
        f"Registro sintético de {student_name} para a atividade {activity_name}. "
        "Este texto existe somente para preencher a demonstração com um histórico "
        "realista de participação, revisão e pontuação, sem representar uma entrega "
        "real ou conter dados pessoais de estudantes."
    )


def _create_submission(
    session: Session,
    *,
    identifier: str,
    student: User,
    activity: Activity,
    event_at,
    approved: bool,
    actor_id: str | None,
    detected_platform: str | None = None,
) -> tuple[Submission, bool]:
    existing = session.get(Submission, identifier)
    if existing is not None:
        if existing.student_id != student.id:
            raise ValueError("ID sintético já pertence a outro aluno")
        return existing, False

    status = (
        SubmissionStatus.APPROVED_AUTO
        if detected_platform
        else SubmissionStatus.APPROVED_MANUAL
    )
    if not approved:
        status = SubmissionStatus.REJECTED
    submission = Submission(
        id=identifier,
        student_id=student.id,
        activity_id=activity.id,
        received_at=event_at,
        status=status,
        title=f"[Demo] {activity.name}",
        summary=_synthetic_summary(student.display_name, activity.name),
        ocr_text=(
            "Lição concluída · atividade sintética"
            if detected_platform
            else "Comprovação sintética para demonstração"
        ),
        detected_platform=detected_platform,
        confidence=0.98 if approved else 0.35,
        declared_units=1,
        recognized_units=1 if approved else 0,
        admin_reason=(
            None
            if approved
            else "Dado sintético: rejeição incluída para demonstrar o histórico."
        ),
        processed_at=event_at,
        decided_at=event_at,
        decided_by_id=actor_id,
        rule_snapshot_json={
            "synthetic": True,
            "seed": _SEED_VERSION,
        },
    )
    session.add(submission)
    session.flush()
    return submission, True


def seed_fake_students(session: Session) -> SyntheticSeedReport:
    """Create five marked fake students and varied history exactly once."""

    expected_ids = _expected_submission_ids()
    usernames = tuple(student.username for student in SYNTHETIC_STUDENTS)
    existing_users = {
        user.username: user
        for user in session.scalars(
            select(User).where(User.username.in_(usernames))
        ).all()
    }
    existing_submission_count = int(
        session.scalar(
            select(func.count(Submission.id)).where(Submission.id.in_(expected_ids))
        )
        or 0
    )
    if len(existing_users) == len(usernames) and existing_submission_count == len(
        expected_ids
    ):
        return SyntheticSeedReport()

    users_created = 0
    for definition in SYNTHETIC_STUDENTS:
        user = existing_users.get(definition.username)
        if user is None:
            user = User(
                id=_seed_id("user", definition.username),
                username=definition.username,
                display_name=definition.name,
                role=Role.STUDENT,
                active=True,
                reminders_enabled=False,
            )
            session.add(user)
            existing_users[definition.username] = user
            users_created += 1
        elif user.role != Role.STUDENT:
            raise ValueError(f"Conta sintética inválida: {definition.username}")
    session.flush()

    activity_codes = {
        "duolingo_beconfident",
        "impact_summary",
        *(code for student in SYNTHETIC_STUDENTS for code in student.activity_codes),
    }
    activities = {
        activity.code: activity
        for activity in session.scalars(
            select(Activity).where(Activity.code.in_(activity_codes))
        ).all()
    }
    missing = activity_codes.difference(activities)
    if missing:
        raise ValueError(f"Catálogo incompleto para seed sintético: {sorted(missing)}")

    actor_id = session.scalar(
        select(User.id)
        .where(User.role == Role.ADMIN, User.active.is_(True))
        .order_by(User.created_at, User.id)
    )
    now = utcnow().replace(microsecond=0)
    submissions_created = 0
    ledger_created = 0

    for student_index, definition in enumerate(SYNTHETIC_STUDENTS):
        user = existing_users[definition.username]
        for activity_index, code in enumerate(definition.activity_codes):
            submission, created = _create_submission(
                session,
                identifier=_submission_id(definition.username, f"activity:{code}"),
                student=user,
                activity=activities[code],
                event_at=now
                - timedelta(days=4 + student_index * 2 + activity_index),
                approved=True,
                actor_id=actor_id,
            )
            submissions_created += int(created)
            award = award_approved_submission(session, submission, actor_id=actor_id)
            ledger_created += len(award.ledger_transaction_ids)

        for lesson_index in range(1, 6):
            submission, created = _create_submission(
                session,
                identifier=_submission_id(definition.username, f"lesson:{lesson_index}"),
                student=user,
                activity=activities["duolingo_beconfident"],
                event_at=now
                - timedelta(days=18 + student_index * 2 - lesson_index),
                approved=True,
                actor_id=actor_id,
                detected_platform=(
                    "duolingo" if lesson_index % 2 else "beconfident"
                ),
            )
            submissions_created += int(created)
            award = award_approved_submission(session, submission, actor_id=actor_id)
            ledger_created += len(award.ledger_transaction_ids)

        _, created = _create_submission(
            session,
            identifier=_submission_id(definition.username, "rejected"),
            student=user,
            activity=activities["impact_summary"],
            event_at=now - timedelta(days=2 + student_index),
            approved=False,
            actor_id=actor_id,
        )
        submissions_created += int(created)

    session.flush()
    return SyntheticSeedReport(
        users_created=users_created,
        submissions_created=submissions_created,
        ledger_transactions_created=ledger_created,
    )


__all__ = [
    "SYNTHETIC_STUDENTS",
    "SyntheticSeedReport",
    "SyntheticStudent",
    "seed_fake_students",
]
