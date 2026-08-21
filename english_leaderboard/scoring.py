from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from .models import (
    Activity,
    LedgerKind,
    LedgerTransaction,
    LessonBatch,
    LessonBatchUnit,
    LessonUnit,
    Role,
    Submission,
    SubmissionStatus,
    User,
    utcnow,
)

LESSON_ACTIVITY_CODE = "duolingo_beconfident"
LESSON_ACTIVITY_GROUP = "duolingo_beconfident"
LESSON_BATCH_POINTS = 5
LESSON_BATCH_SIZE = 5


@dataclass(frozen=True)
class AwardResult:
    points_created: int = 0
    ledger_transaction_ids: tuple[str, ...] = ()
    lesson_batches_created: int = 0


def _approved(status: SubmissionStatus) -> bool:
    return status in {
        SubmissionStatus.APPROVED_AUTO,
        SubmissionStatus.APPROVED_MANUAL,
    }


def _create_lesson_units(session: Session, submission: Submission) -> None:
    existing = set(
        session.scalars(
            select(LessonUnit.unit_index).where(
                LessonUnit.submission_id == submission.id
            )
        ).all()
    )
    for index in range(1, submission.recognized_units + 1):
        if index not in existing:
            session.add(
                LessonUnit(
                    submission_id=submission.id,
                    student_id=submission.student_id,
                    activity_group=LESSON_ACTIVITY_GROUP,
                    unit_index=index,
                    approved_at=submission.decided_at or submission.processed_at or utcnow(),
                )
            )
    session.flush()


def _available_lesson_units(
    session: Session, student_id: str, activity_group: str
) -> list[LessonUnit]:
    return list(
        session.scalars(
            select(LessonUnit)
            .outerjoin(LessonBatchUnit, LessonBatchUnit.unit_id == LessonUnit.id)
            .where(
                LessonUnit.student_id == student_id,
                LessonUnit.activity_group == activity_group,
                LessonBatchUnit.unit_id.is_(None),
            )
            .order_by(LessonUnit.approved_at, LessonUnit.id)
        ).all()
    )


def _next_batch_sequence(session: Session, student_id: str, group: str) -> int:
    current = session.scalar(
        select(func.max(LessonBatch.sequence)).where(
            LessonBatch.student_id == student_id,
            LessonBatch.activity_group == group,
        )
    )
    return int(current or 0) + 1


def award_approved_submission(
    session: Session,
    submission: Submission,
    *,
    actor_id: str | None = None,
) -> AwardResult:
    """Materializa pontos uma única vez para uma submissão aprovada.

    O chamador controla commit/rollback. As restrições únicas são a segunda
    barreira contra repetição acidental da operação.
    """

    if not _approved(submission.status):
        raise ValueError("Somente submissão aprovada pode gerar pontuação")
    activity = session.get(Activity, submission.activity_id)
    if activity is None:
        raise ValueError("Atividade da submissão não encontrada")

    if activity.code != LESSON_ACTIVITY_CODE:
        source_key = f"submission:{submission.id}"
        existing = session.scalar(
            select(LedgerTransaction).where(
                LedgerTransaction.source_key == source_key
            )
        )
        if existing is not None:
            return AwardResult()
        ledger = LedgerTransaction(
            student_id=submission.student_id,
            points=activity.points,
            kind=LedgerKind.DIRECT_ACTIVITY,
            source_type="submission",
            source_id=submission.id,
            source_key=source_key,
            activity_id=activity.id,
            submission_id=submission.id,
            description=activity.name,
            occurred_at=submission.decided_at or submission.processed_at or utcnow(),
            created_by_id=actor_id,
        )
        session.add(ledger)
        session.flush()
        submission.points_awarded = activity.points
        return AwardResult(activity.points, (ledger.id,), 0)

    _create_lesson_units(session, submission)
    threshold = LESSON_BATCH_SIZE
    available = _available_lesson_units(
        session, submission.student_id, LESSON_ACTIVITY_GROUP
    )
    points = 0
    ledger_ids: list[str] = []
    batches = 0
    while len(available) >= threshold:
        group_units = available[:threshold]
        sequence = _next_batch_sequence(
            session, submission.student_id, LESSON_ACTIVITY_GROUP
        )
        source_key = (
            f"lesson_batch:{submission.student_id}:{LESSON_ACTIVITY_GROUP}:{sequence}"
        )
        ledger = LedgerTransaction(
            student_id=submission.student_id,
            points=LESSON_BATCH_POINTS,
            kind=LedgerKind.LESSON_BATCH,
            source_type="lesson_batch",
            source_id=None,
            source_key=source_key,
            activity_id=activity.id,
            submission_id=submission.id,
            description=f"Grupo {sequence}: {threshold} lições validadas",
            occurred_at=submission.decided_at or submission.processed_at or utcnow(),
            created_by_id=actor_id,
        )
        session.add(ledger)
        session.flush()
        batch = LessonBatch(
            student_id=submission.student_id,
            activity_group=LESSON_ACTIVITY_GROUP,
            sequence=sequence,
            ledger_transaction_id=ledger.id,
        )
        session.add(batch)
        session.flush()
        for unit in group_units:
            session.add(LessonBatchUnit(batch_id=batch.id, unit_id=unit.id))
        session.flush()
        points += LESSON_BATCH_POINTS
        ledger_ids.append(ledger.id)
        batches += 1
        available = available[threshold:]

    if points:
        submission.points_awarded += points
    return AwardResult(points, tuple(ledger_ids), batches)


def student_total(
    session: Session,
    student_id: str,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
) -> int:
    conditions = [LedgerTransaction.student_id == student_id]
    if start is not None:
        conditions.append(LedgerTransaction.occurred_at >= start)
    if end is not None:
        conditions.append(LedgerTransaction.occurred_at < end)
    return int(
        session.scalar(
            select(func.coalesce(func.sum(LedgerTransaction.points), 0)).where(
                *conditions
            )
        )
        or 0
    )


def lesson_progress(
    session: Session,
    student_id: str,
    *,
    activity_group: str = LESSON_ACTIVITY_GROUP,
    threshold: int = LESSON_BATCH_SIZE,
) -> tuple[int, int]:
    unused = session.scalar(
        select(func.count(LessonUnit.id))
        .outerjoin(LessonBatchUnit, LessonBatchUnit.unit_id == LessonUnit.id)
        .where(
            LessonUnit.student_id == student_id,
            LessonUnit.activity_group == activity_group,
            LessonBatchUnit.unit_id.is_(None),
        )
    )
    return int(unused or 0) % threshold, threshold


def _period_bounds(
    start: date | datetime | None, end: date | datetime | None
) -> tuple[datetime | None, datetime | None]:
    if isinstance(start, date) and not isinstance(start, datetime):
        start = datetime.combine(start, time.min, tzinfo=timezone.utc)
    if isinstance(end, date) and not isinstance(end, datetime):
        end = datetime.combine(end + timedelta(days=1), time.min, tzinfo=timezone.utc)
    return start, end


def leaderboard_rows(
    session: Session,
    *,
    start: date | datetime | None = None,
    end: date | datetime | None = None,
    include_inactive: bool = False,
) -> list[dict[str, object]]:
    start_dt, end_dt = _period_bounds(start, end)
    ledger_join = [LedgerTransaction.student_id == User.id]
    if start_dt is not None:
        ledger_join.append(LedgerTransaction.occurred_at >= start_dt)
    if end_dt is not None:
        ledger_join.append(LedgerTransaction.occurred_at < end_dt)
    statement = (
        select(
            User.id,
            User.display_name,
            User.email,
            func.coalesce(func.sum(LedgerTransaction.points), 0).label("points"),
        )
        .outerjoin(LedgerTransaction, and_(*ledger_join))
        .where(User.role == Role.STUDENT)
        .group_by(User.id, User.display_name, User.email)
    )
    if not include_inactive:
        statement = statement.where(User.active.is_(True))
    rows = sorted(
        session.execute(statement).all(),
        key=lambda row: (-int(row.points), row.display_name.casefold()),
    )
    output: list[dict[str, object]] = []
    previous_points: int | None = None
    previous_position = 0
    for index, row in enumerate(rows, start=1):
        points = int(row.points)
        position = previous_position if points == previous_points else index
        output.append(
            {
                "position": position,
                "student_id": row.id,
                "student": row.display_name,
                "email": row.email,
                "points": points,
            }
        )
        previous_points = points
        previous_position = position
    return output


def ledger_rows(
    session: Session,
    *,
    student_id: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
) -> list[dict[str, object]]:
    statement = (
        select(LedgerTransaction, User.display_name)
        .join(User, User.id == LedgerTransaction.student_id)
        .order_by(LedgerTransaction.occurred_at.desc(), LedgerTransaction.id.desc())
    )
    if student_id:
        statement = statement.where(LedgerTransaction.student_id == student_id)
    if start:
        statement = statement.where(LedgerTransaction.occurred_at >= start)
    if end:
        statement = statement.where(LedgerTransaction.occurred_at < end)
    return [
        {
            "occurred_at": transaction.occurred_at,
            "student": name,
            "transaction_type": transaction.kind.value,
            "points": transaction.points,
            "source_key": transaction.source_key,
            "description": transaction.description,
        }
        for transaction, name in session.execute(statement).all()
    ]
