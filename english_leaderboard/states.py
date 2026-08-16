from __future__ import annotations

from datetime import datetime, timezone

from .models import Submission, SubmissionStatus


class InvalidStateTransition(ValueError):
    pass


ALLOWED_TRANSITIONS: dict[SubmissionStatus, frozenset[SubmissionStatus]] = {
    SubmissionStatus.PROCESSING: frozenset(
        {
            SubmissionStatus.APPROVED_AUTO,
            SubmissionStatus.NEEDS_REVIEW,
            SubmissionStatus.REJECTED,
            SubmissionStatus.CANCELLED,
        }
    ),
    SubmissionStatus.NEEDS_REVIEW: frozenset(
        {
            SubmissionStatus.APPROVED_MANUAL,
            SubmissionStatus.REJECTED,
            SubmissionStatus.CANCELLED,
        }
    ),
    SubmissionStatus.APPROVED_AUTO: frozenset(),
    SubmissionStatus.APPROVED_MANUAL: frozenset(),
    SubmissionStatus.REJECTED: frozenset(),
    SubmissionStatus.CANCELLED: frozenset(),
}


def can_transition(
    current: SubmissionStatus | str, target: SubmissionStatus | str
) -> bool:
    source = SubmissionStatus(current)
    destination = SubmissionStatus(target)
    return destination in ALLOWED_TRANSITIONS[source]


def transition_submission(
    submission: Submission,
    target: SubmissionStatus | str,
    *,
    decided_by_id: str | None = None,
    reason: str | None = None,
) -> Submission:
    destination = SubmissionStatus(target)
    if not can_transition(submission.status, destination):
        raise InvalidStateTransition(
            f"Transição inválida: {submission.status.value} -> {destination.value}"
        )
    submission.status = destination
    if destination != SubmissionStatus.PROCESSING:
        submission.processed_at = datetime.now(timezone.utc)
    if destination in {
        SubmissionStatus.APPROVED_MANUAL,
        SubmissionStatus.REJECTED,
        SubmissionStatus.CANCELLED,
    }:
        submission.decided_at = datetime.now(timezone.utc)
        submission.decided_by_id = decided_by_id
        submission.admin_reason = reason
    return submission

