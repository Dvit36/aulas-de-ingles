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


def batch_policy(activity: Activity) -> tuple[int, int]:
    """Unidades por grupo e pontos por grupo desta atividade.

    Duolingo/BeConfident tem política fixa de 5 lições por 5 pontos. As demais
    atividades seguem `unit_threshold`: limiar 1 paga a cada aprovação e limiar
    maior acumula unidades até fechar um grupo. O grupo é sempre o código da
    atividade, então unidades nunca migram entre atividades diferentes.
    """

    if activity.code == LESSON_ACTIVITY_CODE:
        return LESSON_BATCH_SIZE, LESSON_BATCH_POINTS
    return max(1, int(activity.unit_threshold or 1)), int(activity.points)


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


def _create_lesson_units(
    session: Session, submission: Submission, activity_group: str
) -> None:
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
                    activity_group=activity_group,
                    unit_index=index,
                    approved_at=submission.decided_at
                    or submission.processed_at
                    or utcnow(),
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

    threshold, batch_points = batch_policy(activity)
    if threshold <= 1:
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

    group = activity.code
    unit_label = "lições" if group == LESSON_ACTIVITY_CODE else "unidades"
    _create_lesson_units(session, submission, group)
    available = _available_lesson_units(session, submission.student_id, group)
    points = 0
    ledger_ids: list[str] = []
    batches = 0
    while len(available) >= threshold:
        group_units = available[:threshold]
        sequence = _next_batch_sequence(session, submission.student_id, group)
        source_key = f"lesson_batch:{submission.student_id}:{group}:{sequence}"
        ledger = LedgerTransaction(
            student_id=submission.student_id,
            points=batch_points,
            kind=LedgerKind.LESSON_BATCH,
            source_type="lesson_batch",
            source_id=None,
            source_key=source_key,
            activity_id=activity.id,
            submission_id=submission.id,
            description=f"Grupo {sequence}: {threshold} {unit_label} validadas",
            occurred_at=submission.decided_at or submission.processed_at or utcnow(),
            created_by_id=actor_id,
        )
        session.add(ledger)
        session.flush()
        batch = LessonBatch(
            student_id=submission.student_id,
            activity_group=group,
            sequence=sequence,
            ledger_transaction_id=ledger.id,
        )
        session.add(batch)
        session.flush()
        for unit in group_units:
            session.add(LessonBatchUnit(batch_id=batch.id, unit_id=unit.id))
        session.flush()
        points += batch_points
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


def pending_group_progress(
    session: Session, student_id: str
) -> list[dict[str, object]]:
    """Unidades ainda não premiadas em cada atividade que agrupa unidades.

    Permite ao aluno ver quanto falta para fechar o próximo grupo em qualquer
    atividade com limiar maior que um, não só em Duolingo/BeConfident.
    """

    rows: list[dict[str, object]] = []
    activities = session.scalars(
        select(Activity)
        .where(Activity.active.is_(True), Activity.archived_at.is_(None))
        .order_by(Activity.name)
    ).all()
    for activity in activities:
        threshold, _ = batch_policy(activity)
        if threshold <= 1:
            continue
        unused, _ = lesson_progress(
            session,
            student_id,
            activity_group=activity.code,
            threshold=threshold,
        )
        rows.append(
            {
                "code": activity.code,
                "activity": activity.name,
                "unused": unused,
                "threshold": threshold,
            }
        )
    return rows


def week_bounds(moment: datetime | None = None) -> tuple[datetime, datetime]:
    """Início (segunda-feira, 00:00 UTC) e fim exclusivo da semana do momento."""

    now = moment or utcnow()
    monday = now.date() - timedelta(days=now.weekday())
    start = datetime.combine(monday, time.min, tzinfo=timezone.utc)
    return start, start + timedelta(days=7)


def weekly_lesson_count(
    session: Session,
    student_id: str,
    *,
    moment: datetime | None = None,
    activity_group: str = LESSON_ACTIVITY_GROUP,
) -> int:
    """Lições aprovadas do aluno na semana corrente.

    Conta unidades, não pontos: a meta semanal fala de lições feitas, e um
    grupo de cinco pode fechar em qualquer dia da semana seguinte.
    """

    start, end = week_bounds(moment)
    return int(
        session.scalar(
            select(func.count(LessonUnit.id)).where(
                LessonUnit.student_id == student_id,
                LessonUnit.activity_group == activity_group,
                LessonUnit.approved_at >= start,
                LessonUnit.approved_at < end,
            )
        )
        or 0
    )


def weekly_submission_count(
    session: Session, *, moment: datetime | None = None
) -> int:
    """Atividades recebidas na semana corrente, em qualquer estado.

    Conta pelo relógio do servidor (`received_at`), não por datas que apareçam
    nas imagens, e inclui rejeitadas e canceladas: a métrica é de volume de
    envios chegando, não de pontuação.
    """

    start, end = week_bounds(moment)
    return int(
        session.scalar(
            select(func.count(Submission.id)).where(
                Submission.received_at >= start,
                Submission.received_at < end,
            )
        )
        or 0
    )


def students_meeting_weekly_goal(
    session: Session, goal: int, *, moment: datetime | None = None
) -> tuple[int, int]:
    """Quantos alunos ativos atingiram a meta da semana, e o total de alunos."""

    students = list(
        session.scalars(
            select(User.id).where(
                User.role == Role.STUDENT,
                User.active.is_(True),
                User.archived_at.is_(None),
            )
        ).all()
    )
    if goal <= 0:
        return 0, len(students)
    reached = sum(
        1
        for student_id in students
        if weekly_lesson_count(session, student_id, moment=moment) >= goal
    )
    return reached, len(students)


def next_rival(
    session: Session,
    student_id: str,
    *,
    start: date | datetime | None = None,
    end: date | datetime | None = None,
) -> dict[str, object] | None:
    """Aluno imediatamente à frente e a diferença de pontos até alcançá-lo.

    Retorna ``None`` para quem já lidera ou não aparece no leaderboard. Empates
    não contam como estar à frente: só quem tem pontuação estritamente maior.
    """

    rows = leaderboard_rows(session, start=start, end=end)
    position = next(
        (index for index, row in enumerate(rows) if row["student_id"] == student_id),
        None,
    )
    if position is None:
        return None
    mine = int(rows[position]["points"])
    for candidate in reversed(rows[:position]):
        if int(candidate["points"]) > mine:
            return {
                "student": candidate["student"],
                "position": int(candidate["position"]),
                "points": int(candidate["points"]),
                "gap": int(candidate["points"]) - mine,
            }
    return None


def activities_closing_gap(
    session: Session, student_id: str, gap: int, *, limit: int = 4
) -> list[dict[str, object]]:
    """Atividades ativas que fecham a diferença, da mais rápida para a mais lenta.

    Para atividades agrupadas o cálculo desconta as unidades já acumuladas, de
    modo que o número mostrado é quantas comprovações ainda faltam de verdade.
    """

    if gap <= 0:
        return []
    activities = session.scalars(
        select(Activity)
        .where(Activity.active.is_(True), Activity.archived_at.is_(None))
        .order_by(Activity.name)
    ).all()
    suggestions: list[dict[str, object]] = []
    for activity in activities:
        threshold, batch_points = batch_policy(activity)
        if batch_points <= 0:
            continue
        groups = -(-gap // batch_points)  # teto da divisão
        if threshold <= 1:
            needed = groups
        else:
            pending, _ = lesson_progress(
                session,
                student_id,
                activity_group=activity.code,
                threshold=threshold,
            )
            needed = max(1, groups * threshold - pending)
        suggestions.append(
            {
                "activity": activity.name,
                "code": activity.code,
                "points": batch_points,
                "threshold": threshold,
                "needed": needed,
            }
        )
    suggestions.sort(key=lambda item: (item["needed"], -int(item["points"])))
    return suggestions[:limit]


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
            User.username,
            func.coalesce(func.sum(LedgerTransaction.points), 0).label("points"),
        )
        .outerjoin(LedgerTransaction, and_(*ledger_join))
        .where(User.role == Role.STUDENT)
        .group_by(User.id, User.display_name, User.username)
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
                "username": row.username,
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
