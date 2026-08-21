from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from .authz import (
    AuthorizationError,
    require_active,
    require_admin,
    require_submission_access,
)
from .config import Settings
from .document_processing import (
    DocumentValidationError,
    persist_document,
    process_document_bytes,
)
from .image_processing import (
    AnalyzedImage,
    ImagePolicy,
    ImageValidationError,
    analyze_image_bytes,
    persist_image,
    phash_distance,
    prepare_ocr_variants,
)
from .models import (
    Activity,
    ApprovedEvidence,
    ApprovedFileEvidence,
    AuthSession,
    AuditLog,
    CheckOutcome,
    DuplicateKind,
    DuplicateMatch,
    LedgerKind,
    LedgerTransaction,
    Role,
    RuleCheck,
    Submission,
    SubmissionFile,
    SubmissionImage,
    SubmissionStatus,
    User,
    new_id,
    utcnow,
)
from .ocr import OCRExecutionError, OCRResult, create_ocr_engine, extract_text
from .rules import AnalysisDecision, RuleResult, analyze_submission_rules
from .scoring import AwardResult, award_approved_submission
from .states import transition_submission
from .local_auth import (
    generate_temporary_password,
    revoke_all_user_sessions,
    set_temporary_password,
)


@dataclass(frozen=True)
class UploadPayload:
    filename: str
    data: bytes


@dataclass(frozen=True)
class SubmissionResult:
    submission_id: str
    status: SubmissionStatus
    confidence: float
    recognized_units: int
    points_created: int
    reason: str


@dataclass(frozen=True)
class ReviewResult:
    submission_id: str
    status: SubmissionStatus
    recognized_units: int
    points_created: int


def add_audit(
    session: Session,
    *,
    actor_id: str | None,
    action: str,
    entity_type: str,
    entity_id: str,
    reason: str | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
) -> AuditLog:
    log = AuditLog(
        actor_id=actor_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        reason=reason,
        before_json=before,
        after_json=after,
    )
    session.add(log)
    return log


def resolve_oidc_user(
    session: Session,
    *,
    email: str,
    display_name: str | None,
    settings: Settings,
    issuer: str | None = None,
    subject: str | None = None,
    email_verified: bool = True,
) -> User:
    if not email_verified:
        raise AuthorizationError("O provedor OIDC não confirmou o e-mail")
    normalized = email.strip().lower()
    if not normalized or "@" not in normalized:
        raise AuthorizationError("OIDC não forneceu um e-mail válido")
    if normalized not in settings.allowed_emails:
        raise AuthorizationError("E-mail não está na lista autorizada")
    user = None
    if issuer and subject:
        user = session.scalar(
            select(User).where(
                User.oidc_issuer == issuer,
                User.oidc_subject == subject,
            )
        )
    if user is None:
        user = session.scalar(select(User).where(User.email == normalized))
    if user is None:
        user = User(
            email=normalized,
            display_name=(display_name or normalized.split("@", 1)[0]).strip(),
            role=Role.ADMIN if normalized in settings.admin_emails else Role.STUDENT,
            active=True,
            oidc_issuer=issuer,
            oidc_subject=subject,
        )
        session.add(user)
        session.flush()
        add_audit(
            session,
            actor_id=user.id,
            action="user_created_from_oidc",
            entity_type="user",
            entity_id=user.id,
            after={"email": user.email, "role": user.role.value},
        )
    elif not user.active:
        raise AuthorizationError("Usuário está inativo")
    elif issuer and subject:
        if user.oidc_issuer and (
            user.oidc_issuer != issuer or user.oidc_subject != subject
        ):
            raise AuthorizationError("Identidade OIDC não corresponde ao usuário")
        user.oidc_issuer = issuer
        user.oidc_subject = subject
    elif display_name and user.display_name != display_name.strip():
        user.display_name = display_name.strip()
    return user


def _image_policy(settings: Settings) -> ImagePolicy:
    return ImagePolicy(
        max_bytes=settings.max_upload_bytes,
        min_width=settings.min_image_width,
        min_height=settings.min_image_height,
        allowed_formats=settings.allowed_image_formats,
        blur_threshold=settings.min_laplacian_variance,
    )


def _client_filename(filename: str) -> str:
    safe = Path((filename or "upload").replace("\x00", "")).name
    return safe[:255] or "upload"


def _persist_rule_checks(
    session: Session, submission: Submission, checks: Iterable[RuleResult]
) -> None:
    for check in checks:
        session.add(
            RuleCheck(
                submission_id=submission.id,
                rule_name=check.name,
                outcome=check.outcome,
                required=check.required,
                score=check.score,
                message=check.message,
                details_json=check.details,
            )
        )


def _claim_approved_evidence(session: Session, submission: Submission) -> None:
    """Claim every evidence hash before scoring; PKs close concurrent races."""

    files = session.scalars(
        select(SubmissionFile).where(SubmissionFile.submission_id == submission.id)
    ).all()
    for stored_file in files:
        existing = session.get(ApprovedFileEvidence, stored_file.sha256)
        if existing is not None and existing.submission_id != submission.id:
            raise ValueError("Este arquivo já foi utilizado em uma aprovação")
        if existing is None:
            session.add(
                ApprovedFileEvidence(
                    sha256=stored_file.sha256,
                    file_id=stored_file.id,
                    submission_id=submission.id,
                    student_id=submission.student_id,
                )
            )

    images = session.scalars(
        select(SubmissionImage).where(SubmissionImage.submission_id == submission.id)
    ).all()
    for image in images:
        existing = session.get(ApprovedEvidence, image.sha256)
        if existing is not None and existing.submission_id != submission.id:
            raise ValueError("Esta imagem já foi utilizada em uma aprovação")
        if existing is None:
            session.add(
                ApprovedEvidence(
                    sha256=image.sha256,
                    image_id=image.id,
                    submission_id=submission.id,
                    student_id=submission.student_id,
                )
            )
    session.flush()


def _previous_summaries(
    session: Session, *, student_id: str, activity_id: str
) -> list[str]:
    return list(
        session.scalars(
            select(Submission.summary).where(
                Submission.student_id == student_id,
                Submission.activity_id == activity_id,
                Submission.summary.is_not(None),
                Submission.status.in_(
                    [
                        SubmissionStatus.APPROVED_AUTO,
                        SubmissionStatus.APPROVED_MANUAL,
                    ]
                ),
            )
        ).all()
    )


def _duplicate_candidates(
    session: Session,
) -> list[tuple[SubmissionImage, str]]:
    return list(
        session.execute(
            select(SubmissionImage, Submission.student_id).join(
                Submission, Submission.id == SubmissionImage.submission_id
            )
        ).all()
    )


def _record_duplicate_matches(
    session: Session,
    *,
    submission: Submission,
    stored_images: Sequence[SubmissionImage],
    analyzed: Sequence[AnalyzedImage],
    prior_candidates: Sequence[tuple[SubmissionImage, str]],
    max_distance: int,
) -> tuple[list[bool], list[bool]]:
    exact_flags = [False] * len(analyzed)
    similar_flags = [False] * len(analyzed)
    for index, (current_model, current_analysis) in enumerate(
        zip(stored_images, analyzed, strict=True)
    ):
        for candidate, candidate_student_id in prior_candidates:
            if current_analysis.sha256 == candidate.sha256:
                exact_flags[index] = True
                session.add(
                    DuplicateMatch(
                        image_id=current_model.id,
                        matched_image_id=candidate.id,
                        kind=DuplicateKind.EXACT,
                        distance=0,
                        same_student=candidate_student_id == submission.student_id,
                    )
                )
                continue
            distance = phash_distance(current_analysis.phash, candidate.phash)
            if distance <= max_distance:
                similar_flags[index] = True
                session.add(
                    DuplicateMatch(
                        image_id=current_model.id,
                        matched_image_id=candidate.id,
                        kind=DuplicateKind.SIMILAR,
                        distance=distance,
                        same_student=candidate_student_id == submission.student_id,
                    )
                )
        for previous_index in range(index):
            previous_model = stored_images[previous_index]
            previous_analysis = analyzed[previous_index]
            if current_analysis.sha256 == previous_analysis.sha256:
                exact_flags[index] = True
                session.add(
                    DuplicateMatch(
                        image_id=current_model.id,
                        matched_image_id=previous_model.id,
                        kind=DuplicateKind.EXACT,
                        distance=0,
                        same_student=True,
                    )
                )
            else:
                distance = phash_distance(current_analysis.phash, previous_analysis.phash)
                if distance <= max_distance:
                    similar_flags[index] = True
                    session.add(
                        DuplicateMatch(
                            image_id=current_model.id,
                            matched_image_id=previous_model.id,
                            kind=DuplicateKind.SIMILAR,
                            distance=distance,
                            same_student=True,
                        )
                    )
    return exact_flags, similar_flags


def submit_evidence(
    session: Session,
    *,
    actor: User,
    activity_id: str,
    uploads: Sequence[UploadPayload],
    settings: Settings,
    ocr_engine: Any | None = None,
    title: str | None = None,
    url: str | None = None,
    summary: str | None = None,
) -> SubmissionResult:
    require_active(actor)
    if actor.role != Role.STUDENT:
        raise AuthorizationError("Use um usuário aluno para enviar comprovação")
    activity = session.get(Activity, activity_id)
    if activity is None or not activity.active or activity.archived_at is not None:
        raise ValueError("Atividade não existe ou está inativa")
    if len(uploads) > settings.max_upload_files:
        raise ValueError(
            f"Envie no máximo {settings.max_upload_files} arquivos por submissão"
        )
    total_upload_bytes = sum(len(upload.data) for upload in uploads)
    if total_upload_bytes > settings.max_upload_total_bytes:
        raise ValueError(
            "O conjunto de arquivos excede o limite total configurado"
        )

    submission = Submission(
        student_id=actor.id,
        activity_id=activity.id,
        title=(title or "").strip() or None,
        url=(url or "").strip() or None,
        summary=(summary or "").strip() or None,
        declared_units=len(uploads),
        status=SubmissionStatus.PROCESSING,
        rule_snapshot_json={
            "activity_code": activity.code,
            "activity_name": activity.name,
            "points": 5 if activity.code == "duolingo_beconfident" else activity.points,
            "unit_threshold": 5
            if activity.code == "duolingo_beconfident"
            else activity.unit_threshold,
            "config": activity.config_json,
        },
    )
    session.add(submission)
    session.flush()

    analyses: list[AnalyzedImage] = []
    image_uploads: list[UploadPayload] = []
    documents: list[tuple[UploadPayload, Any]] = []
    try:
        for upload in uploads:
            extension = Path(upload.filename.replace("\x00", "")).suffix.casefold()
            if extension in {".png", ".jpg", ".jpeg", ".webp"}:
                image_uploads.append(upload)
                analyses.append(
                    analyze_image_bytes(upload.data, policy=_image_policy(settings))
                )
                continue
            if activity.code == "duolingo_beconfident":
                raise DocumentValidationError(
                    "Duolingo/BeConfident aceita somente imagens",
                    code="image_required",
                )
            document = process_document_bytes(
                upload.data,
                upload.filename,
                max_bytes=settings.max_upload_bytes,
                max_pdf_pages=settings.max_pdf_pages,
                max_document_expanded_bytes=settings.max_document_expanded_bytes,
                max_pdf_render_pixels=settings.max_pdf_render_pixels,
            )
            if document.file_kind == "pdf" and not document.extracted_text:
                if ocr_engine is None:
                    ocr_engine = create_ocr_engine()
                document = process_document_bytes(
                    upload.data,
                    upload.filename,
                    max_bytes=settings.max_upload_bytes,
                    max_pdf_pages=settings.max_pdf_pages,
                    max_document_expanded_bytes=settings.max_document_expanded_bytes,
                    max_pdf_render_pixels=settings.max_pdf_render_pixels,
                    ocr_engine=ocr_engine,
                )
            documents.append((upload, document))
    except (ImageValidationError, DocumentValidationError) as error:
        error_code = getattr(error, "code", "invalid_file")
        error_details = getattr(error, "details", {})
        check = RuleResult(
            name="valid_file_content",
            outcome=CheckOutcome.FAIL,
            required=True,
            score=0.0,
            message=getattr(error, "message", str(error)),
            details={**error_details, "code": error_code, "hard_reject": True},
        )
        _persist_rule_checks(session, submission, [check])
        transition_submission(
            submission,
            SubmissionStatus.REJECTED,
            reason="Arquivo inválido",
        )
        add_audit(
            session,
            actor_id=actor.id,
            action="submission_auto_rejected",
            entity_type="submission",
            entity_id=submission.id,
            reason=error_code,
            after={"status": submission.status.value},
        )
        session.flush()
        return SubmissionResult(
            submission.id,
            submission.status,
            0.0,
            0,
            0,
            "Arquivo inválido",
        )

    prior_candidates = _duplicate_candidates(session)
    stored_images: list[SubmissionImage] = []
    stored_files: list[SubmissionFile] = []
    for upload, analysis in zip(image_uploads, analyses, strict=True):
        path = persist_image(analysis, settings.upload_dir)
        session.info.setdefault("created_upload_paths", []).append(path)
        model = SubmissionImage(
            submission_id=submission.id,
            storage_key=analysis.storage_name,
            client_filename=_client_filename(upload.filename),
            mime_type=analysis.mime_type,
            image_format=analysis.image_format,
            size_bytes=analysis.byte_size,
            width=analysis.width,
            height=analysis.height,
            sha256=analysis.sha256,
            phash=analysis.phash,
            laplacian_variance=analysis.laplacian_variance,
        )
        session.add(model)
        stored_images.append(model)
    session.flush()
    for upload, analysis, image_model in zip(
        image_uploads, analyses, stored_images, strict=True
    ):
        file_model = SubmissionFile(
            submission_id=submission.id,
            image_id=image_model.id,
            storage_key=analysis.storage_name,
            client_filename=_client_filename(upload.filename),
            mime_type=analysis.mime_type,
            file_kind="image",
            size_bytes=analysis.byte_size,
            sha256=analysis.sha256,
            extracted_text="",
        )
        session.add(file_model)
        stored_files.append(file_model)

    duplicate_document = False
    document_hashes: set[str] = set()
    for upload, document in documents:
        duplicate_document = (
            duplicate_document
            or document.sha256 in document_hashes
            or session.scalar(
                select(SubmissionFile.id).where(
                    SubmissionFile.sha256 == document.sha256
                )
            )
            is not None
        )
        document_hashes.add(document.sha256)
        storage_key, path = persist_document(document, settings.upload_dir)
        session.info.setdefault("created_upload_paths", []).append(path)
        file_model = SubmissionFile(
            submission_id=submission.id,
            storage_key=storage_key,
            client_filename=_client_filename(upload.filename),
            mime_type=document.mime_type,
            file_kind=document.file_kind,
            size_bytes=document.byte_size,
            sha256=document.sha256,
            page_count=document.page_count,
            extracted_text=document.extracted_text,
        )
        session.add(file_model)
        stored_files.append(file_model)
    session.flush()

    exact_flags, similar_flags = _record_duplicate_matches(
        session,
        submission=submission,
        stored_images=stored_images,
        analyzed=analyses,
        prior_candidates=prior_candidates,
        max_distance=settings.phash_distance_threshold,
    )

    if ocr_engine is None and analyses:
        ocr_engine = create_ocr_engine()
    ocr_results: list[OCRResult] = []
    for analysis in analyses:
        try:
            variants = prepare_ocr_variants(analysis)
            primary = extract_text(variants["original"], engine=ocr_engine)
            candidates = [primary]
            if (primary.confidence or 0.0) < 0.60 or len(primary.text.strip()) < 10:
                for variant_name in ("contrast", "threshold"):
                    candidates.append(
                        extract_text(variants[variant_name], engine=ocr_engine)
                    )
            ocr_results.append(
                max(
                    candidates,
                    key=lambda result: (
                        len(result.text.strip()),
                        result.confidence or 0.0,
                    ),
                )
            )
        except OCRExecutionError:
            ocr_results.append(OCRResult.empty())

    decision = analyze_submission_rules(
        activity=activity,
        images=analyses,
        ocr_results=ocr_results,
        title=submission.title,
        url=submission.url,
        summary=submission.summary,
        exact_duplicate_flags=exact_flags,
        similar_duplicate_flags=similar_flags,
        previous_summaries=_previous_summaries(
            session, student_id=actor.id, activity_id=activity.id
        ),
        auto_approve_confidence=settings.auto_approve_confidence,
        evidence_count=len(stored_files),
        document_texts=[document.extracted_text for _, document in documents],
    )
    if duplicate_document:
        _persist_rule_checks(
            session,
            submission,
            [
                RuleResult(
                    name="exact_document_duplicate",
                    outcome=CheckOutcome.FAIL,
                    required=True,
                    score=0.0,
                    message="Documento idêntico já foi enviado",
                    details={"hard_reject": True},
                )
            ],
        )
        decision = AnalysisDecision(
            status=SubmissionStatus.REJECTED,
            confidence=0.0,
            recognized_units=0,
            detected_platform=decision.detected_platform,
            ocr_text=decision.ocr_text,
            checks=decision.checks,
            reason="Documento duplicado",
        )
    submission.ocr_text = decision.ocr_text
    submission.detected_platform = decision.detected_platform
    submission.confidence = decision.confidence
    submission.recognized_units = decision.recognized_units
    _persist_rule_checks(session, submission, decision.checks)
    transition_submission(submission, decision.status, reason=decision.reason)
    award = AwardResult()
    if decision.status == SubmissionStatus.APPROVED_AUTO:
        _claim_approved_evidence(session, submission)
        award = award_approved_submission(session, submission)
    add_audit(
        session,
        actor_id=actor.id,
        action="submission_processed",
        entity_type="submission",
        entity_id=submission.id,
        reason=decision.reason,
        after={
            "status": submission.status.value,
            "confidence": submission.confidence,
            "recognized_units": submission.recognized_units,
            "points_created": award.points_created,
        },
    )
    session.flush()
    return SubmissionResult(
        submission.id,
        submission.status,
        submission.confidence,
        submission.recognized_units,
        award.points_created,
        decision.reason,
    )


def get_submission_for_user(
    session: Session, *, actor: User, submission_id: str
) -> Submission:
    submission = session.scalar(
        select(Submission)
        .options(
            selectinload(Submission.images),
            selectinload(Submission.files),
            selectinload(Submission.checks),
            selectinload(Submission.activity),
            selectinload(Submission.student),
        )
        .where(Submission.id == submission_id)
    )
    if submission is None:
        raise LookupError("Submissão não encontrada")
    require_submission_access(actor, submission)
    return submission


def get_submission_file_for_user(
    session: Session,
    *,
    actor: User,
    file_id: str,
    settings: Settings,
) -> tuple[SubmissionFile, Path]:
    stored_file = session.get(SubmissionFile, file_id)
    if stored_file is None:
        raise LookupError("Arquivo não encontrado")
    submission = session.get(Submission, stored_file.submission_id)
    if submission is None:
        raise LookupError("Submissão não encontrada")
    require_submission_access(actor, submission)
    path = settings.upload_dir / stored_file.storage_key
    if not path.is_file() or path.parent.resolve() != settings.upload_dir.resolve():
        raise LookupError("Arquivo armazenado não encontrado")
    return stored_file, path


def list_submissions(
    session: Session,
    *,
    actor: User,
    student_id: str | None = None,
    status: SubmissionStatus | str | None = None,
    activity_id: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
) -> list[Submission]:
    require_active(actor)
    if actor.role != Role.ADMIN:
        if student_id is not None and student_id != actor.id:
            raise AuthorizationError("Acesso negado")
        student_id = actor.id
    statement = (
        select(Submission)
        .options(
            selectinload(Submission.student),
            selectinload(Submission.activity),
            selectinload(Submission.files),
            selectinload(Submission.images),
            selectinload(Submission.checks),
        )
        .order_by(Submission.received_at.desc(), Submission.id.desc())
    )
    if student_id:
        statement = statement.where(Submission.student_id == student_id)
    if status:
        statement = statement.where(Submission.status == SubmissionStatus(status))
    if activity_id:
        statement = statement.where(Submission.activity_id == activity_id)
    if start:
        statement = statement.where(Submission.received_at >= start)
    if end:
        statement = statement.where(Submission.received_at < end)
    return list(session.scalars(statement).all())


def review_submission(
    session: Session,
    *,
    actor: User,
    submission_id: str,
    approve: bool,
    reason: str,
    recognized_units: int | None = None,
) -> ReviewResult:
    require_admin(actor)
    justification = reason.strip()
    if len(justification) < 3:
        raise ValueError("Informe uma justificativa administrativa")
    submission = session.get(Submission, submission_id)
    if submission is None:
        raise LookupError("Submissão não encontrada")
    if submission.status != SubmissionStatus.NEEDS_REVIEW:
        raise ValueError("Somente itens em needs_review podem ser decididos")
    activity = session.get(Activity, submission.activity_id)
    if activity is None:
        raise LookupError("Atividade não encontrada")
    before = {
        "status": submission.status.value,
        "recognized_units": submission.recognized_units,
        "points_awarded": submission.points_awarded,
    }
    award = AwardResult()
    if approve:
        if activity.code == "duolingo_beconfident":
            units = submission.recognized_units if recognized_units is None else recognized_units
            if units < 1:
                raise ValueError("Aprovação de lições exige ao menos uma unidade")
            image_count = int(
                session.scalar(
                    select(func.count(SubmissionImage.id)).where(
                        SubmissionImage.submission_id == submission.id
                    )
                )
                or 0
            )
            if units > image_count:
                raise ValueError("Cada imagem única pode representar no máximo uma unidade")
            submission.recognized_units = units
        else:
            submission.recognized_units = 1
        transition_submission(
            submission,
            SubmissionStatus.APPROVED_MANUAL,
            decided_by_id=actor.id,
            reason=justification,
        )
        _claim_approved_evidence(session, submission)
        award = award_approved_submission(session, submission, actor_id=actor.id)
        action = "submission_approved_manual"
    else:
        submission.recognized_units = 0
        transition_submission(
            submission,
            SubmissionStatus.REJECTED,
            decided_by_id=actor.id,
            reason=justification,
        )
        action = "submission_rejected_manual"
    add_audit(
        session,
        actor_id=actor.id,
        action=action,
        entity_type="submission",
        entity_id=submission.id,
        reason=justification,
        before=before,
        after={
            "status": submission.status.value,
            "recognized_units": submission.recognized_units,
            "points_awarded": submission.points_awarded,
        },
    )
    session.flush()
    return ReviewResult(
        submission.id,
        submission.status,
        submission.recognized_units,
        award.points_created,
    )


def cancel_submission(
    session: Session, *, actor: User, submission_id: str, reason: str = "Cancelada pelo aluno"
) -> Submission:
    submission = session.get(Submission, submission_id)
    if submission is None:
        raise LookupError("Submissão não encontrada")
    require_submission_access(actor, submission)
    transition_submission(
        submission,
        SubmissionStatus.CANCELLED,
        decided_by_id=actor.id,
        reason=reason,
    )
    add_audit(
        session,
        actor_id=actor.id,
        action="submission_cancelled",
        entity_type="submission",
        entity_id=submission.id,
        reason=reason,
    )
    return submission


def save_activity_changes(
    session: Session,
    *,
    actor: User,
    activity_id: str,
    name: str,
    points: int,
    active: bool,
    summary_min_chars: int | None = None,
    unit_threshold: int | None = None,
    requires_images: bool | None = None,
    requires_summary: bool | None = None,
    requires_title_or_url: bool | None = None,
    content_review_required: bool | None = None,
    auto_approvable: bool | None = None,
    reason: str | None = None,
) -> Activity:
    require_admin(actor)
    justification = (reason or "").strip()
    if len(justification) < 3:
        raise ValueError("Informe o motivo da alteração")
    activity = session.get(Activity, activity_id)
    if activity is None:
        raise LookupError("Atividade não encontrada")
    if points <= 0:
        raise ValueError("Pontos devem ser positivos")
    if activity.code == "duolingo_beconfident" and (
        int(points) != 5
        or (unit_threshold is not None and int(unit_threshold) != 5)
    ):
        raise ValueError(
            "Duolingo/BeConfident possui política fixa de 5 pontos a cada 5 lições"
        )
    if activity.archived_at is not None:
        raise ValueError("Atividade arquivada não pode ser editada")
    before = {
        "name": activity.name,
        "points": activity.points,
        "active": activity.active,
        "summary_min_chars": activity.summary_min_chars,
        "unit_threshold": activity.unit_threshold,
        "requires_images": activity.requires_images,
        "requires_summary": activity.requires_summary,
        "requires_title_or_url": activity.requires_title_or_url,
        "content_review_required": activity.content_review_required,
        "auto_approvable": activity.auto_approvable,
    }
    activity.name = name.strip() or activity.name
    activity.points = int(points)
    activity.active = bool(active)
    if summary_min_chars is not None:
        activity.summary_min_chars = max(0, int(summary_min_chars))
    if unit_threshold is not None:
        if int(unit_threshold) < 1:
            raise ValueError("Limiar de unidades deve ser positivo")
        activity.unit_threshold = int(unit_threshold)
    for field_name, value in {
        "requires_images": requires_images,
        "requires_summary": requires_summary,
        "requires_title_or_url": requires_title_or_url,
        "content_review_required": content_review_required,
        "auto_approvable": auto_approvable,
    }.items():
        if value is not None:
            setattr(activity, field_name, bool(value))
    add_audit(
        session,
        actor_id=actor.id,
        action="activity_updated",
        entity_type="activity",
        entity_id=activity.id,
        reason=justification,
        before=before,
        after={
            "name": activity.name,
            "points": activity.points,
            "active": activity.active,
            "summary_min_chars": activity.summary_min_chars,
            "unit_threshold": activity.unit_threshold,
            "requires_images": activity.requires_images,
            "requires_summary": activity.requires_summary,
            "requires_title_or_url": activity.requires_title_or_url,
            "content_review_required": activity.content_review_required,
            "auto_approvable": activity.auto_approvable,
        },
    )
    return activity


def create_activity(
    session: Session,
    *,
    actor: User,
    code: str,
    name: str,
    points: int,
    unit_threshold: int = 1,
    requires_images: bool = True,
    requires_summary: bool = False,
    requires_title_or_url: bool = False,
    summary_min_chars: int = 0,
    content_review_required: bool = True,
    auto_approvable: bool = False,
    reason: str = "Criação administrativa",
) -> Activity:
    require_admin(actor)
    normalized_code = "_".join(code.strip().lower().split())
    if not normalized_code or not normalized_code.replace("_", "").isalnum():
        raise ValueError("Código da atividade inválido")
    if session.scalar(select(Activity.id).where(Activity.code == normalized_code)):
        raise ValueError("Já existe uma atividade com esse código")
    if points <= 0 or unit_threshold <= 0:
        raise ValueError("Pontos e unidades devem ser positivos")
    activity = Activity(
        code=normalized_code,
        name=name.strip(),
        points=int(points),
        unit_threshold=int(unit_threshold),
        requires_images=bool(requires_images),
        requires_summary=bool(requires_summary),
        requires_title_or_url=bool(requires_title_or_url),
        summary_min_chars=max(0, int(summary_min_chars)),
        content_review_required=bool(content_review_required),
        auto_approvable=bool(auto_approvable),
        active=True,
    )
    if not activity.name:
        raise ValueError("Nome da atividade é obrigatório")
    session.add(activity)
    session.flush()
    add_audit(
        session,
        actor_id=actor.id,
        action="activity_created",
        entity_type="activity",
        entity_id=activity.id,
        reason=reason,
        after={"code": activity.code, "name": activity.name, "points": activity.points},
    )
    return activity


def save_user(
    session: Session,
    *,
    actor: User,
    email: str,
    display_name: str,
    role: Role | str = Role.STUDENT,
    active: bool = True,
    user_id: str | None = None,
    reason: str | None = None,
    reminders_enabled: bool | None = None,
) -> User:
    require_admin(actor)
    justification = (reason or "").strip()
    if len(justification) < 3:
        raise ValueError("Informe o motivo da alteração")
    normalized = email.strip().lower()
    if "@" not in normalized:
        raise ValueError("E-mail inválido")
    requested_role = Role(role)
    user = session.get(User, user_id) if user_id else session.scalar(
        select(User).where(User.email == normalized)
    )
    before = None
    if user is None:
        user = User(
            email=normalized,
            display_name=display_name.strip(),
            role=requested_role,
            active=bool(active),
        )
        session.add(user)
        action = "user_created"
    else:
        conflict = session.scalar(
            select(User).where(User.email == normalized, User.id != user.id)
        )
        if conflict is not None:
            raise ValueError("E-mail já pertence a outro usuário")
        before = {
            "email": user.email,
            "display_name": user.display_name,
            "role": user.role.value,
            "active": user.active,
        }
        if user.role == Role.ADMIN and user.active and (
            requested_role != Role.ADMIN or not active
        ):
            other_admins = int(
                session.scalar(
                    select(func.count(User.id)).where(
                        User.role == Role.ADMIN,
                        User.active.is_(True),
                        User.archived_at.is_(None),
                        User.id != user.id,
                    )
                )
                or 0
            )
            if other_admins == 0:
                raise ValueError("Não é permitido desativar o último administrador ativo")
        user.email = normalized
        user.display_name = display_name.strip() or user.display_name
        user.role = requested_role
        user.active = bool(active)
        if reminders_enabled is not None:
            user.reminders_enabled = bool(reminders_enabled)
        action = "user_updated"
        if before["role"] != requested_role.value or before["active"] != bool(active):
            revoke_all_user_sessions(session, user)
    session.flush()
    add_audit(
        session,
        actor_id=actor.id,
        action=action,
        entity_type="user",
        entity_id=user.id,
        reason=justification,
        before=before,
        after={
            "display_name": user.display_name,
            "email": user.email,
            "role": user.role.value,
            "active": user.active,
        },
    )
    return user


def create_user_account(
    session: Session,
    *,
    actor: User,
    email: str,
    display_name: str,
    role: Role | str = Role.STUDENT,
    reason: str = "Cadastro administrativo",
) -> tuple[User, str]:
    require_admin(actor)
    normalized = email.strip().lower()
    if session.scalar(select(User.id).where(func.lower(User.email) == normalized)):
        raise ValueError("Já existe uma conta com esse e-mail")
    temporary_password = generate_temporary_password()
    user = save_user(
        session,
        actor=actor,
        email=normalized,
        display_name=display_name,
        role=role,
        active=True,
        reason=reason,
    )
    set_temporary_password(session, user, temporary_password)
    return user, temporary_password


def reset_user_password(
    session: Session,
    *,
    actor: User,
    user_id: str,
    reason: str,
) -> str:
    require_admin(actor)
    if len(reason.strip()) < 3:
        raise ValueError("Informe o motivo da redefinição")
    user = session.get(User, user_id)
    if user is None or user.archived_at is not None:
        raise LookupError("Usuário não encontrado")
    temporary_password = generate_temporary_password()
    set_temporary_password(session, user, temporary_password)
    add_audit(
        session,
        actor_id=actor.id,
        action="user_password_reset",
        entity_type="user",
        entity_id=user.id,
        reason=reason.strip(),
    )
    return temporary_password


def archive_or_delete_user(
    session: Session,
    *,
    actor: User,
    user_id: str,
    reason: str,
) -> str:
    require_admin(actor)
    justification = reason.strip()
    if len(justification) < 3:
        raise ValueError("Informe o motivo da exclusão ou arquivamento")
    user = session.get(User, user_id)
    if user is None:
        raise LookupError("Usuário não encontrado")
    if user.id == actor.id:
        raise ValueError("Não é possível excluir a própria conta em uso")
    if user.role == Role.ADMIN and user.active and user.archived_at is None:
        other_admins = int(
            session.scalar(
                select(func.count(User.id)).where(
                    User.role == Role.ADMIN,
                    User.active.is_(True),
                    User.archived_at.is_(None),
                    User.id != user.id,
                )
            )
            or 0
        )
        if other_admins == 0:
            raise ValueError("Não é permitido excluir o último administrador ativo")
    references = sum(
        int(value or 0)
        for value in (
            session.scalar(
                select(func.count(Submission.id)).where(Submission.student_id == user.id)
            ),
            session.scalar(
                select(func.count(LedgerTransaction.id)).where(
                    LedgerTransaction.student_id == user.id
                )
            ),
            session.scalar(
                select(func.count(AuthSession.id)).where(AuthSession.user_id == user.id)
            ),
            session.scalar(
                select(func.count(AuditLog.id)).where(AuditLog.actor_id == user.id)
            ),
        )
    )
    add_audit(
        session,
        actor_id=actor.id,
        action="user_archived" if references else "user_deleted",
        entity_type="user",
        entity_id=user.id,
        reason=justification,
        before={"active": user.active, "archived": user.archived_at is not None},
    )
    if references:
        user.active = False
        user.archived_at = utcnow()
        revoke_all_user_sessions(session, user)
        return "archived"
    session.delete(user)
    return "deleted"


def archive_or_delete_activity(
    session: Session,
    *,
    actor: User,
    activity_id: str,
    reason: str,
) -> str:
    require_admin(actor)
    justification = reason.strip()
    if len(justification) < 3:
        raise ValueError("Informe o motivo da exclusão ou arquivamento")
    activity = session.get(Activity, activity_id)
    if activity is None:
        raise LookupError("Atividade não encontrada")
    references = sum(
        int(value or 0)
        for value in (
            session.scalar(
                select(func.count(Submission.id)).where(
                    Submission.activity_id == activity.id
                )
            ),
            session.scalar(
                select(func.count(LedgerTransaction.id)).where(
                    LedgerTransaction.activity_id == activity.id
                )
            ),
        )
    )
    add_audit(
        session,
        actor_id=actor.id,
        action="activity_archived" if references else "activity_deleted",
        entity_type="activity",
        entity_id=activity.id,
        reason=justification,
        before={"active": activity.active, "archived": activity.archived_at is not None},
    )
    if references:
        activity.active = False
        activity.archived_at = utcnow()
        return "archived"
    session.delete(activity)
    return "deleted"


def create_points_adjustment(
    session: Session,
    *,
    actor: User,
    student_id: str,
    points: int,
    reason: str,
) -> LedgerTransaction:
    require_admin(actor)
    justification = reason.strip()
    if not points:
        raise ValueError("O ajuste não pode ser zero")
    if len(justification) < 3:
        raise ValueError("Informe o motivo do ajuste")
    student = session.get(User, student_id)
    if student is None or student.role != Role.STUDENT:
        raise LookupError("Aluno não encontrado")
    adjustment_id = new_id()
    transaction = LedgerTransaction(
        student_id=student.id,
        points=int(points),
        kind=LedgerKind.ADJUSTMENT,
        source_type="adjustment",
        source_id=adjustment_id,
        source_key=f"adjustment:{adjustment_id}",
        description=justification,
        created_by_id=actor.id,
    )
    session.add(transaction)
    session.flush()
    add_audit(
        session,
        actor_id=actor.id,
        action="points_adjusted",
        entity_type="ledger_transaction",
        entity_id=transaction.id,
        reason=justification,
        after={"student_id": student.id, "points": int(points)},
    )
    return transaction


def list_review_queue(session: Session, *, actor: User) -> list[Submission]:
    require_admin(actor)
    return list(
        session.scalars(
            select(Submission)
            .options(
                selectinload(Submission.student),
                selectinload(Submission.activity),
                selectinload(Submission.images),
                selectinload(Submission.files),
                selectinload(Submission.checks),
            )
            .where(Submission.status == SubmissionStatus.NEEDS_REVIEW)
            .order_by(Submission.received_at)
        ).all()
    )


def admin_ledger_rows(
    session: Session,
    *,
    actor: User,
    student_id: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
) -> list[dict[str, object]]:
    require_admin(actor)
    from .scoring import ledger_rows

    return ledger_rows(
        session, student_id=student_id, start=start, end=end
    )


def admin_student_submissions(
    session: Session, *, actor: User, student_id: str
) -> list[Submission]:
    require_admin(actor)
    return list(
        session.scalars(
            select(Submission)
            .options(selectinload(Submission.activity))
            .where(Submission.student_id == student_id)
            .order_by(Submission.received_at.desc())
        ).all()
    )


__all__ = [
    "ReviewResult",
    "SubmissionResult",
    "UploadPayload",
    "add_audit",
    "admin_ledger_rows",
    "admin_student_submissions",
    "archive_or_delete_activity",
    "archive_or_delete_user",
    "cancel_submission",
    "create_activity",
    "create_points_adjustment",
    "create_user_account",
    "get_submission_file_for_user",
    "get_submission_for_user",
    "list_submissions",
    "list_review_queue",
    "resolve_oidc_user",
    "review_submission",
    "reset_user_password",
    "save_activity_changes",
    "save_user",
    "submit_evidence",
]
