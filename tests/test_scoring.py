from __future__ import annotations

from sqlalchemy import func, select

from english_leaderboard.models import (
    Activity,
    LedgerTransaction,
    LessonBatchUnit,
    Role,
    Submission,
    SubmissionStatus,
)
from english_leaderboard.scoring import (
    award_approved_submission,
    batch_policy,
    leaderboard_rows,
    lesson_progress,
    pending_group_progress,
    student_total,
)


def _activity(session, code: str) -> Activity:
    return session.scalar(select(Activity).where(Activity.code == code))


def test_five_unique_lessons_award_exactly_five_once(session, users):
    student = users[Role.STUDENT]
    activity = _activity(session, "duolingo_beconfident")
    awards = []
    submissions = []
    for _ in range(5):
        submission = Submission(
            student_id=student.id,
            activity_id=activity.id,
            status=SubmissionStatus.APPROVED_MANUAL,
            recognized_units=1,
        )
        session.add(submission)
        session.flush()
        awards.append(award_approved_submission(session, submission).points_created)
        submissions.append(submission)
    session.commit()

    assert awards == [0, 0, 0, 0, 5]
    assert student_total(session, student.id) == 5
    assert session.scalar(select(func.count(LedgerTransaction.id))) == 1
    assert session.scalar(select(func.count(LessonBatchUnit.unit_id))) == 5
    assert lesson_progress(session, student.id) == (0, 5)

    assert award_approved_submission(session, submissions[-1]).points_created == 0
    session.commit()
    assert submissions[-1].points_awarded == 5
    assert session.scalar(select(func.count(LedgerTransaction.id))) == 1


def test_ten_units_form_two_non_overlapping_batches(session, users):
    student = users[Role.STUDENT]
    activity = _activity(session, "duolingo_beconfident")
    submission = Submission(
        student_id=student.id,
        activity_id=activity.id,
        status=SubmissionStatus.APPROVED_MANUAL,
        recognized_units=10,
    )
    session.add(submission)
    session.flush()
    result = award_approved_submission(session, submission)
    session.commit()
    assert result.points_created == 10
    assert result.lesson_batches_created == 2
    assert session.scalar(select(func.count(LessonBatchUnit.unit_id))) == 10


def test_sixth_through_ninth_lessons_do_not_create_a_second_award(session, users):
    student = users[Role.STUDENT]
    activity = _activity(session, "duolingo_beconfident")
    awards = []
    for index in range(1, 11):
        submission = Submission(
            student_id=student.id,
            activity_id=activity.id,
            status=SubmissionStatus.APPROVED_MANUAL,
            recognized_units=1,
        )
        session.add(submission)
        session.flush()
        awards.append(award_approved_submission(session, submission).points_created)
        if index == 9:
            assert lesson_progress(session, student.id) == (4, 5)
    session.commit()
    assert awards == [0, 0, 0, 0, 5, 0, 0, 0, 0, 5]
    assert student_total(session, student.id) == 10


def test_lesson_policy_cannot_be_changed_by_mutable_catalog_values(session, users):
    student = users[Role.STUDENT]
    activity = _activity(session, "duolingo_beconfident")
    activity.points = 99
    activity.unit_threshold = 1
    awards = []
    for _ in range(5):
        submission = Submission(
            student_id=student.id,
            activity_id=activity.id,
            status=SubmissionStatus.APPROVED_MANUAL,
            recognized_units=1,
        )
        session.add(submission)
        session.flush()
        awards.append(award_approved_submission(session, submission).points_created)
    assert awards == [0, 0, 0, 0, 5]
    assert student_total(session, student.id) == 5


def test_direct_activity_is_idempotent_and_historical_points_do_not_change(session, users):
    student = users[Role.STUDENT]
    activity = _activity(session, "impact_summary")
    submission = Submission(
        student_id=student.id,
        activity_id=activity.id,
        status=SubmissionStatus.APPROVED_MANUAL,
        recognized_units=1,
    )
    session.add(submission)
    session.flush()
    assert award_approved_submission(session, submission).points_created == 10
    assert award_approved_submission(session, submission).points_created == 0
    activity.points = 99
    session.commit()
    assert student_total(session, student.id) == 10


def test_leaderboard_updates_from_ledger_and_uses_competition_rank(session, users):
    student = users[Role.STUDENT]
    activity = _activity(session, "impact_summary")
    submission = Submission(
        student_id=student.id,
        activity_id=activity.id,
        status=SubmissionStatus.APPROVED_MANUAL,
        recognized_units=1,
    )
    session.add(submission)
    session.flush()
    award_approved_submission(session, submission)
    session.commit()
    board = leaderboard_rows(session)
    assert board[0]["student_id"] == student.id
    assert board[0]["points"] == 10
    assert board[0]["position"] == 1


def _approve(session, student, activity, units: int = 1):
    submission = Submission(
        student_id=student.id,
        activity_id=activity.id,
        status=SubmissionStatus.APPROVED_MANUAL,
        recognized_units=units,
    )
    session.add(submission)
    session.flush()
    return award_approved_submission(session, submission)


def test_activity_threshold_groups_units_outside_duolingo(session, users):
    student = users[Role.STUDENT]
    activity = Activity(
        code="podcast_ingles",
        name="Podcast em inglês",
        points=10,
        unit_threshold=3,
    )
    session.add(activity)
    session.flush()

    assert batch_policy(activity) == (3, 10)
    awarded = [_approve(session, student, activity).points_created for _ in range(7)]
    session.commit()

    # Paga 10 uma vez a cada três aprovações, não 10 por aprovação.
    assert awarded == [0, 0, 10, 0, 0, 10, 0]
    assert student_total(session, student.id) == 20
    assert lesson_progress(
        session, student.id, activity_group="podcast_ingles", threshold=3
    ) == (1, 3)


def test_threshold_one_still_pays_on_every_approval(session, users):
    student = users[Role.STUDENT]
    activity = _activity(session, "english_meeting")

    assert batch_policy(activity) == (1, 30)
    awarded = [_approve(session, student, activity).points_created for _ in range(2)]
    session.commit()

    assert awarded == [30, 30]
    assert student_total(session, student.id) == 60


def test_units_never_migrate_between_grouped_activities(session, users):
    student = users[Role.STUDENT]
    duolingo = _activity(session, "duolingo_beconfident")
    other = Activity(
        code="podcast_ingles",
        name="Podcast em inglês",
        points=10,
        unit_threshold=3,
    )
    session.add(other)
    session.flush()

    for _ in range(4):
        _approve(session, student, duolingo)
    for _ in range(2):
        _approve(session, student, other)
    session.commit()

    # Quatro lições e duas unidades de podcast não fecham grupo nenhum.
    assert student_total(session, student.id) == 0
    progress = {row["code"]: (row["unused"], row["threshold"]) for row in
                pending_group_progress(session, student.id)}
    assert progress["duolingo_beconfident"] == (4, 5)
    assert progress["podcast_ingles"] == (2, 3)


def test_weekly_lesson_count_only_sees_the_current_week(session, users):
    from datetime import timedelta

    from english_leaderboard.models import LessonUnit
    from english_leaderboard.scoring import week_bounds, weekly_lesson_count

    student = users[Role.STUDENT]
    activity = _activity(session, "duolingo_beconfident")
    start, _ = week_bounds()
    submission = Submission(
        student_id=student.id,
        activity_id=activity.id,
        status=SubmissionStatus.APPROVED_MANUAL,
        recognized_units=1,
    )
    session.add(submission)
    session.flush()
    moments = [
        start + timedelta(days=1),  # dentro da semana
        start + timedelta(days=3),  # dentro da semana
        start - timedelta(days=2),  # semana anterior
    ]
    for index, moment in enumerate(moments, start=1):
        session.add(
            LessonUnit(
                submission_id=submission.id,
                student_id=student.id,
                activity_group="duolingo_beconfident",
                unit_index=index,
                approved_at=moment,
            )
        )
    session.commit()

    assert weekly_lesson_count(session, student.id) == 2


def test_weekly_goal_summary_counts_students_that_reached_it(session, users):
    from datetime import timedelta

    from english_leaderboard.models import LessonUnit
    from english_leaderboard.scoring import (
        students_meeting_weekly_goal,
        week_bounds,
    )

    student = users[Role.STUDENT]
    activity = _activity(session, "duolingo_beconfident")
    start, _ = week_bounds()
    submission = Submission(
        student_id=student.id,
        activity_id=activity.id,
        status=SubmissionStatus.APPROVED_MANUAL,
        recognized_units=1,
    )
    session.add(submission)
    session.flush()
    for index in range(1, 4):
        session.add(
            LessonUnit(
                submission_id=submission.id,
                student_id=student.id,
                activity_group="duolingo_beconfident",
                unit_index=index,
                approved_at=start + timedelta(hours=index),
            )
        )
    session.commit()

    assert students_meeting_weekly_goal(session, 3) == (1, 1)
    assert students_meeting_weekly_goal(session, 4) == (0, 1)


def test_next_rival_reports_the_gap_and_ignores_ties(session, users):
    from english_leaderboard.models import User
    from english_leaderboard.scoring import next_rival

    student = users[Role.STUDENT]
    leader = User(username="leader", display_name="Líder", role=Role.STUDENT)
    tied = User(username="tied", display_name="Empatado", role=Role.STUDENT)
    session.add_all([leader, tied])
    session.flush()
    meeting = _activity(session, "english_meeting")
    _approve(session, student, meeting)  # 30
    _approve(session, tied, meeting)  # 30
    for _ in range(2):
        _approve(session, leader, meeting)  # 60
    session.commit()

    rival = next_rival(session, student.id)
    assert rival is not None
    # O empatado não conta como estar à frente; o alvo é quem tem mais pontos.
    assert rival["student"] == "Líder"
    assert rival["gap"] == 30
    assert next_rival(session, leader.id) is None


def test_gap_suggestions_prefer_fewer_submissions_and_discount_pending_units(
    session, users
):
    from english_leaderboard.scoring import activities_closing_gap

    student = users[Role.STUDENT]
    duolingo = _activity(session, "duolingo_beconfident")
    # Quatro lições já acumuladas: falta uma para fechar o grupo de 5 pontos.
    _approve(session, student, duolingo, units=4)
    session.commit()

    todas = activities_closing_gap(session, student.id, gap=5, limit=20)
    by_code = {item["code"]: item for item in todas}

    # Com quatro lições no bolso, falta uma única comprovação para os 5 pontos.
    assert by_code["duolingo_beconfident"]["needed"] == 1
    assert by_code["english_meeting"]["needed"] == 1
    # Cambridge Basic vale 10: um envio já cobre a diferença de 5.
    assert by_code["cambridge_basic"]["needed"] == 1
    # A lista curta prioriza menos envios e, no empate, mais pontos por envio.
    curtas = activities_closing_gap(session, student.id, gap=5)
    assert len(curtas) == 4
    assert curtas[0]["code"] == "english_meeting"
    assert [int(item["needed"]) for item in curtas] == [1, 1, 1, 1]
    assert activities_closing_gap(session, student.id, gap=0) == []


def test_gap_suggestions_scale_with_a_larger_distance(session, users):
    from english_leaderboard.scoring import activities_closing_gap

    student = users[Role.STUDENT]
    todas = activities_closing_gap(session, student.id, gap=45, limit=20)
    by_code = {item["code"]: item for item in todas}

    # 45 pontos: duas reuniões (30 cada) ou nove grupos de 5 lições.
    assert by_code["english_meeting"]["needed"] == 2
    assert by_code["duolingo_beconfident"]["needed"] == 45


def test_weekly_submission_count_uses_server_clock_and_all_states(session, users):
    from datetime import timedelta

    from english_leaderboard.scoring import week_bounds, weekly_submission_count

    student = users[Role.STUDENT]
    activity = _activity(session, "english_meeting")
    start, _ = week_bounds()
    momentos = [
        (start + timedelta(days=1), SubmissionStatus.APPROVED_MANUAL),
        (start + timedelta(days=2), SubmissionStatus.REJECTED),
        (start + timedelta(days=3), SubmissionStatus.NEEDS_REVIEW),
        (start - timedelta(days=1), SubmissionStatus.APPROVED_MANUAL),  # semana passada
    ]
    for received_at, status in momentos:
        session.add(
            Submission(
                student_id=student.id,
                activity_id=activity.id,
                status=status,
                received_at=received_at,
            )
        )
    session.commit()

    # Rejeitadas e pendentes contam: a métrica é de volume recebido.
    assert weekly_submission_count(session) == 3
