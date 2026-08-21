from __future__ import annotations

import pytest
from sqlalchemy import func, select

from english_leaderboard.authz import AuthorizationError
from english_leaderboard.models import (
    Activity,
    DuplicateKind,
    DuplicateMatch,
    Role,
    SubmissionStatus,
    User,
)
from english_leaderboard.scoring import leaderboard_rows, student_total
from english_leaderboard.services import (
    UploadPayload,
    admin_ledger_rows,
    list_review_queue,
    review_submission,
    submit_evidence,
)

from conftest import FakeDuolingoOCR, make_png


def _activity(session, code):
    return session.scalar(select(Activity).where(Activity.code == code))


def _portuguese_summary(seed: str) -> str:
    return (
        f"Eu assisti ao conteúdo {seed} e aprendi sobre comunicação, trabalho em equipe e novas palavras. "
        "O vídeo apresentou exemplos importantes para usar o inglês durante reuniões e atividades da equipe. "
        "Também anotei as ideias principais para praticar depois com meus colegas."
    )


def test_exact_duplicate_is_auto_rejected_and_never_scores(session, users, settings):
    student = users[Role.STUDENT]
    activity = _activity(session, "duolingo_beconfident")
    payload = make_png(10)
    first = submit_evidence(
        session,
        actor=student,
        activity_id=activity.id,
        uploads=[UploadPayload("first.png", payload)],
        settings=settings,
        ocr_engine=FakeDuolingoOCR(),
    )
    session.commit()
    second = submit_evidence(
        session,
        actor=student,
        activity_id=activity.id,
        uploads=[UploadPayload("again.png", payload)],
        settings=settings,
        ocr_engine=FakeDuolingoOCR(),
    )
    session.commit()
    assert first.status == SubmissionStatus.APPROVED_AUTO
    assert second.status == SubmissionStatus.REJECTED
    assert student_total(session, student.id) == 0


def test_phash_only_match_goes_to_review(session, users, settings):
    student = users[Role.STUDENT]
    activity = _activity(session, "duolingo_beconfident")
    first_bytes = make_png(11, metadata="one")
    second_bytes = make_png(11, metadata="two")
    submit_evidence(
        session,
        actor=student,
        activity_id=activity.id,
        uploads=[UploadPayload("one.png", first_bytes)],
        settings=settings,
        ocr_engine=FakeDuolingoOCR(),
    )
    session.commit()
    result = submit_evidence(
        session,
        actor=student,
        activity_id=activity.id,
        uploads=[UploadPayload("two.png", second_bytes)],
        settings=settings,
        ocr_engine=FakeDuolingoOCR(),
    )
    session.commit()
    assert result.status == SubmissionStatus.NEEDS_REVIEW
    assert session.scalar(
        select(func.count(DuplicateMatch.id)).where(DuplicateMatch.kind == DuplicateKind.SIMILAR)
    ) >= 1
    with pytest.raises(ValueError, match="no máximo uma unidade"):
        review_submission(
            session,
            actor=users[Role.ADMIN],
            submission_id=result.submission_id,
            approve=True,
            reason="Tentativa de correção excessiva",
            recognized_units=2,
        )


def test_exact_duplicate_is_compared_across_students(session, users, settings):
    first_student = users[Role.STUDENT]
    other = User(email="other@example.org", display_name="Other", role=Role.STUDENT)
    session.add(other)
    session.commit()
    activity = _activity(session, "duolingo_beconfident")
    payload = make_png(15)
    submit_evidence(
        session,
        actor=first_student,
        activity_id=activity.id,
        uploads=[UploadPayload("one.png", payload)],
        settings=settings,
        ocr_engine=FakeDuolingoOCR(),
    )
    session.commit()
    result = submit_evidence(
        session,
        actor=other,
        activity_id=activity.id,
        uploads=[UploadPayload("copy.png", payload)],
        settings=settings,
        ocr_engine=FakeDuolingoOCR(),
    )
    session.commit()
    assert result.status == SubmissionStatus.REJECTED
    match = session.scalar(
        select(DuplicateMatch).where(DuplicateMatch.kind == DuplicateKind.EXACT).order_by(DuplicateMatch.created_at.desc())
    )
    assert match.same_student is False


def test_manual_approval_updates_leaderboard_and_rejection_does_not(session, users, settings):
    student = users[Role.STUDENT]
    admin = users[Role.ADMIN]
    activity = _activity(session, "impact_summary")
    pending = submit_evidence(
        session,
        actor=student,
        activity_id=activity.id,
        uploads=[UploadPayload("proof.png", make_png(21))],
        settings=settings,
        ocr_engine=FakeDuolingoOCR(),
        title="Impact lesson",
        summary=_portuguese_summary("A"),
    )
    assert pending.status == SubmissionStatus.NEEDS_REVIEW
    approved = review_submission(
        session,
        actor=admin,
        submission_id=pending.submission_id,
        approve=True,
        reason="Resumo e comprovante conferidos",
    )
    session.commit()
    assert approved.points_created == 10
    assert leaderboard_rows(session)[0]["points"] == 10

    second = submit_evidence(
        session,
        actor=student,
        activity_id=activity.id,
        uploads=[UploadPayload("proof-2.png", make_png(44))],
        settings=settings,
        ocr_engine=FakeDuolingoOCR(),
        title="Outro Impact",
        summary=_portuguese_summary("B diferente"),
    )
    review_submission(
        session,
        actor=admin,
        submission_id=second.submission_id,
        approve=False,
        reason="Comprovante não corresponde à atividade",
    )
    session.commit()
    assert student_total(session, student.id) == 10


def test_admin_queries_are_guarded_in_service_layer(session, users):
    with pytest.raises(AuthorizationError):
        list_review_queue(session, actor=users[Role.STUDENT])
    with pytest.raises(AuthorizationError):
        admin_ledger_rows(session, actor=users[Role.STUDENT])
    assert list_review_queue(session, actor=users[Role.ADMIN]) == []
