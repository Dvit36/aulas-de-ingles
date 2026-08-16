from __future__ import annotations

import pytest
from sqlalchemy import select

from english_leaderboard.authz import (
    AuthorizationError,
    can_view_submission,
    require_admin,
)
from english_leaderboard.models import Activity, Role, Submission, SubmissionStatus, User
from english_leaderboard.states import InvalidStateTransition, transition_submission


def test_state_transitions_accept_review_decisions_and_reject_terminal_changes(session, users):
    student = users[Role.STUDENT]
    activity = session.scalar(select(Activity).where(Activity.code == "impact_summary"))
    submission = Submission(student_id=student.id, activity_id=activity.id)
    session.add(submission)
    session.flush()
    transition_submission(submission, SubmissionStatus.NEEDS_REVIEW)
    transition_submission(
        submission,
        SubmissionStatus.APPROVED_MANUAL,
        decided_by_id=users[Role.ADMIN].id,
        reason="Comprovante revisado",
    )
    with pytest.raises(InvalidStateTransition):
        transition_submission(submission, SubmissionStatus.REJECTED)


def test_student_and_admin_permissions(session, users):
    student = users[Role.STUDENT]
    admin = users[Role.ADMIN]
    activity = session.scalar(select(Activity).where(Activity.code == "impact_summary"))
    submission = Submission(student_id=student.id, activity_id=activity.id)
    assert can_view_submission(student, submission)
    assert can_view_submission(admin, submission)
    other = User(email="other@example.org", display_name="Other", role=Role.STUDENT)
    session.add(other)
    session.flush()
    assert not can_view_submission(other, submission)
    with pytest.raises(AuthorizationError):
        require_admin(student)
    require_admin(admin)

