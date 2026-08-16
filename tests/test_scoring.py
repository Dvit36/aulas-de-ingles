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
    leaderboard_rows,
    lesson_progress,
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
