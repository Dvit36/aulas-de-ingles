from __future__ import annotations

from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

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
    process_document_bytes,
)
from .image_processing import (
    AnalyzedImage,
    ImagePolicy,
    ImageValidationError,
    analyze_image_bytes,
    phash_distance,
    prepare_ocr_variants,
)
from .contas import Contas, exige_conta_no_auth
from .supabase_auth import normalize_username
from .schema import (
    Activity,
    ApprovedEvidence,
    AuditLog,
    CheckOutcome,
    DuplicateKind,
    DuplicateMatch,
    GoalConfiguration,
    LedgerKind,
    LedgerTransaction,
    Resource,
    Role,
    RuleCheck,
    Submission,
    SubmissionFile,
    SubmissionStatus,
    User,
    new_id,
    utcnow,
)
from .ocr import OCRExecutionError, OCRResult, create_ocr_engine, extract_text
from .storage import URL_EXPIRA_SEGUNDOS, StorageGateway
from .storage_service import (
    ArquivoNaoAutorizado,
    ArquivoRegistrado,
    salvar_arquivo,
    url_temporaria,
)
from .submission_pipeline import (
    CATEGORIA_DOCUMENTO,
    CATEGORIA_IMAGEM,
    limites_de,
)
from .rules import AnalysisDecision, RuleResult, analyze_submission_rules
from .scoring import AwardResult, award_approved_submission
from .states import transition_submission


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
    """Reivindica cada checksum antes de pontuar; a PK fecha a corrida.

    Imagem e documento deixaram de ter tabelas separadas: uma única
    reivindicação por checksum cobre os dois, e a chave primária garante que
    duas aprovações simultâneas do mesmo arquivo não paguem duas vezes.
    """

    files = session.scalars(
        select(SubmissionFile).where(SubmissionFile.submission_id == submission.id)
    ).all()
    for stored_file in files:
        existing = session.get(ApprovedEvidence, stored_file.checksum_sha256)
        if existing is not None and existing.submission_id != submission.id:
            raise ValueError("Este arquivo já foi utilizado em uma aprovação")
        if existing is None:
            session.add(
                ApprovedEvidence(
                    checksum_sha256=stored_file.checksum_sha256,
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
) -> list[tuple[SubmissionFile, str]]:
    """Arquivos já enviados que podem colidir com o envio atual.

    Só entram os que têm pHash: a comparação perceptual é de imagem. Documento
    é confrontado por checksum exato, em outro caminho.
    """

    return list(
        session.execute(
            select(SubmissionFile, Submission.student_id)
            .join(Submission, Submission.id == SubmissionFile.submission_id)
            .where(SubmissionFile.phash.is_not(None))
        ).all()
    )


def _record_duplicate_matches(
    session: Session,
    *,
    submission: Submission,
    stored_files: Sequence[SubmissionFile],
    analyzed: Sequence[AnalyzedImage],
    prior_candidates: Sequence[tuple[SubmissionFile, str]],
    max_distance: int,
) -> tuple[list[bool], list[bool]]:
    exact_flags = [False] * len(analyzed)
    similar_flags = [False] * len(analyzed)
    for index, (current_model, current_analysis) in enumerate(
        zip(stored_files, analyzed, strict=True)
    ):
        for candidate, candidate_student_id in prior_candidates:
            if current_analysis.sha256 == candidate.checksum_sha256:
                exact_flags[index] = True
                session.add(
                    DuplicateMatch(
                        submission_id=submission.id,
                        file_id=current_model.id,
                        matched_file_id=candidate.id,
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
                        submission_id=submission.id,
                        file_id=current_model.id,
                        matched_file_id=candidate.id,
                        kind=DuplicateKind.SIMILAR,
                        distance=distance,
                        same_student=candidate_student_id == submission.student_id,
                    )
                )
        for previous_index in range(index):
            previous_model = stored_files[previous_index]
            previous_analysis = analyzed[previous_index]
            if current_analysis.sha256 == previous_analysis.sha256:
                exact_flags[index] = True
                session.add(
                    DuplicateMatch(
                        submission_id=submission.id,
                        file_id=current_model.id,
                        matched_file_id=previous_model.id,
                        kind=DuplicateKind.EXACT,
                        distance=0,
                        same_student=True,
                    )
                )
            else:
                distance = phash_distance(
                    current_analysis.phash, previous_analysis.phash
                )
                if distance <= max_distance:
                    similar_flags[index] = True
                    session.add(
                        DuplicateMatch(
                            submission_id=submission.id,
                            file_id=current_model.id,
                            matched_file_id=previous_model.id,
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
    gateway: StorageGateway,
    ocr_engine: Any | None = None,
    title: str | None = None,
    url: str | None = None,
    summary: str | None = None,
) -> SubmissionResult:
    """Recebe a comprovação: valida, envia ao Storage e grava os metadados.

    ``gateway`` é o bucket privado autenticado com o token do próprio aluno,
    para as políticas do Storage valerem. Ele é injetado em vez de construído
    aqui para os testes exercitarem o fluxo inteiro sem rede.
    """

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
        raise ValueError("O conjunto de arquivos excede o limite total configurado")

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
    # Os binários vão para o bucket privado; o banco fica só com a referência.
    # ``salvar_arquivo`` compensa sozinho: se a gravação dos metadados falhar,
    # o objeto recém-enviado é removido, e se nem isso der certo ele é
    # registrado em ``storage_orphans`` para reconciliação.
    conexao = session.connection()
    limites = limites_de(settings)
    registrados: list[ArquivoRegistrado] = []
    for posicao, (upload, analysis) in enumerate(
        zip(image_uploads, analyses, strict=True)
    ):
        registrados.append(
            salvar_arquivo(
                conexao,
                gateway,
                submission_id=submission.id,
                student_id=actor.id,
                filename=_client_filename(upload.filename),
                dados=analysis.original_bytes,
                content_type=analysis.mime_type,
                categoria=CATEGORIA_IMAGEM,
                extensao=analysis.extension,
                bucket=settings.storage_bucket,
                limites=limites,
                phash=analysis.phash,
                position=posicao,
                width=analysis.width,
                height=analysis.height,
            )
        )

    duplicate_document = False
    document_hashes: set[str] = set()
    for deslocamento, (upload, document) in enumerate(documents):
        duplicate_document = (
            duplicate_document
            or document.sha256 in document_hashes
            or session.scalar(
                select(SubmissionFile.id).where(
                    SubmissionFile.checksum_sha256 == document.sha256
                )
            )
            is not None
        )
        document_hashes.add(document.sha256)
        registrados.append(
            salvar_arquivo(
                conexao,
                gateway,
                submission_id=submission.id,
                student_id=actor.id,
                filename=_client_filename(upload.filename),
                dados=document.original_bytes,
                content_type=document.mime_type,
                categoria=CATEGORIA_DOCUMENTO,
                extensao=document.extension,
                bucket=settings.storage_bucket,
                limites=limites,
                ocr_text=document.extracted_text or None,
                position=len(image_uploads) + deslocamento,
                page_count=document.page_count,
            )
        )

    # As linhas foram inseridas por SQL na mesma transação; recarregar pelo ORM
    # é o que dá aos passos seguintes objetos mapeados, na ordem de envio.
    stored_files = [
        session.get(SubmissionFile, registro.id) for registro in registrados
    ]

    exact_flags, similar_flags = _record_duplicate_matches(
        session,
        submission=submission,
        # ``stored_files`` também contém documentos; a comparação perceptual é
        # posicional contra ``analyses``, então só as imagens entram aqui.
        stored_files=stored_files[: len(analyses)],
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
            selectinload(Submission.files),
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


def get_submission_file_url_for_user(
    session: Session,
    *,
    actor: User,
    file_id: str,
    settings: Settings,
    gateway: StorageGateway,
    expira_em: int = URL_EXPIRA_SEGUNDOS,
) -> tuple[SubmissionFile, str]:
    """URL assinada curta, emitida somente depois de autorizar o registro.

    Entrega o arquivo sem que os bytes passem pelo servidor do Streamlit. A
    ordem de verificação é a mesma do download e não pode ser afrouxada: o
    identificador é resolvido em um registro, a submissão é conferida contra o
    ator, e só então ``url_temporaria`` assina — que por sua vez chama
    ``resolver_arquivo_autorizado`` e, para quem não é administrador,
    ``ensure_owned_key``. São duas barreiras sobre coisas diferentes: esta
    camada confere a submissão, a camada de Storage confere a chave física.

    A URL não é gravada em lugar nenhum: o banco guarda apenas a chave.
    """

    stored_file = session.get(SubmissionFile, file_id)
    if stored_file is None:
        raise LookupError("Arquivo não encontrado")
    submission = session.get(Submission, stored_file.submission_id)
    if submission is None:
        raise LookupError("Submissão não encontrada")
    require_submission_access(actor, submission)
    try:
        url = url_temporaria(
            session.connection(),
            gateway,
            file_id=stored_file.id,
            student_id=actor.id,
            is_admin=actor.role == Role.ADMIN,
            limites=limites_de(settings),
            expira_em=expira_em,
        )
    except ArquivoNaoAutorizado as erro:
        raise AuthorizationError(str(erro)) from erro
    return stored_file, url


# Uma página cobre a temporada inteira de um aluno típico — a planilha
# migrada tem 61 envios para 7 alunos — então o histórico costuma caber sem
# paginar. Para o administrador, é uma tela cheia da fila sem prometer que a
# consulta continue barata quando o acervo dobrar.
PAGINA_PADRAO = 25


def _filtros_de_submissao(
    *,
    actor: User,
    student_id: str | None,
    status: SubmissionStatus | str | None,
    statuses: Collection[SubmissionStatus | str] | None,
    activity_id: str | None,
    start: datetime | None,
    end: datetime | None,
) -> list[Any]:
    """Condições compartilhadas pela listagem e pela contagem.

    As duas precisam enxergar exatamente o mesmo conjunto: um filtro aplicado
    só de um lado faria o total discordar das páginas.
    """

    require_active(actor)
    if actor.role != Role.ADMIN:
        if student_id is not None and student_id != actor.id:
            raise AuthorizationError("Acesso negado")
        student_id = actor.id
    condicoes: list[Any] = []
    if student_id:
        condicoes.append(Submission.student_id == student_id)
    if status:
        condicoes.append(Submission.status == SubmissionStatus(status))
    if statuses is not None:
        # Coleção vazia é um filtro que não seleciona nada, e não a ausência
        # de filtro: `in_(())` é justamente o que expressa isso.
        condicoes.append(
            Submission.status.in_([SubmissionStatus(item) for item in statuses])
        )
    if activity_id:
        condicoes.append(Submission.activity_id == activity_id)
    if start:
        condicoes.append(Submission.received_at >= start)
    if end:
        condicoes.append(Submission.received_at < end)
    return condicoes


def list_submissions(
    session: Session,
    *,
    actor: User,
    student_id: str | None = None,
    status: SubmissionStatus | str | None = None,
    statuses: Collection[SubmissionStatus | str] | None = None,
    activity_id: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int | None = PAGINA_PADRAO,
    offset: int = 0,
) -> list[Submission]:
    """Uma página de submissões, da mais recente para a mais antiga.

    ``limit=None`` devolve tudo e existe para quem realmente precisa do
    conjunto inteiro — exportação, contagem em memória. Não use em tela: cada
    submissão carrega seus arquivos, e a lista cresce com o acervo.
    """

    if limit is not None and limit <= 0:
        raise ValueError("limit deve ser positivo")
    if offset < 0:
        raise ValueError("offset não pode ser negativo")
    condicoes = _filtros_de_submissao(
        actor=actor,
        student_id=student_id,
        status=status,
        statuses=statuses,
        activity_id=activity_id,
        start=start,
        end=end,
    )
    statement = (
        select(Submission)
        .options(
            selectinload(Submission.student),
            selectinload(Submission.activity),
            selectinload(Submission.files),
            selectinload(Submission.checks),
        )
        .where(*condicoes)
        .order_by(Submission.received_at.desc(), Submission.id.desc())
    )
    if limit is not None:
        statement = statement.limit(limit)
    if offset:
        statement = statement.offset(offset)
    return list(session.scalars(statement).all())


def count_submissions(
    session: Session,
    *,
    actor: User,
    student_id: str | None = None,
    status: SubmissionStatus | str | None = None,
    statuses: Collection[SubmissionStatus | str] | None = None,
    activity_id: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
) -> int:
    """Quantas submissões os mesmos filtros alcançam, sem carregar nenhuma."""

    condicoes = _filtros_de_submissao(
        actor=actor,
        student_id=student_id,
        status=status,
        statuses=statuses,
        activity_id=activity_id,
        start=start,
        end=end,
    )
    total = session.scalar(
        select(func.count(Submission.id)).where(*condicoes)
    )
    return int(total or 0)


def review_submission(
    session: Session,
    *,
    actor: User,
    submission_id: str,
    approve: bool,
    recognized_units: int | None = None,
) -> ReviewResult:
    require_admin(actor)
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
            units = (
                submission.recognized_units
                if recognized_units is None
                else recognized_units
            )
            if units < 1:
                raise ValueError("Aprovação de lições exige ao menos uma unidade")
            # Só imagens contam como unidade; documento não é lição.
            image_count = int(
                session.scalar(
                    select(func.count(SubmissionFile.id)).where(
                        SubmissionFile.submission_id == submission.id,
                        SubmissionFile.content_type.startswith("image/"),
                    )
                )
                or 0
            )
            if units > image_count:
                raise ValueError(
                    "Cada imagem única pode representar no máximo uma unidade"
                )
            submission.recognized_units = units
        else:
            submission.recognized_units = 1
        transition_submission(
            submission,
            SubmissionStatus.APPROVED_MANUAL,
            decided_by_id=actor.id,
            reason=None,
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
            reason=None,
        )
        action = "submission_rejected_manual"
    add_audit(
        session,
        actor_id=actor.id,
        action=action,
        entity_type="submission",
        entity_id=submission.id,
        reason=None,
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
    session: Session,
    *,
    actor: User,
    submission_id: str,
    reason: str = "Cancelada pelo aluno",
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
) -> Activity:
    require_admin(actor)
    activity = session.get(Activity, activity_id)
    if activity is None:
        raise LookupError("Atividade não encontrada")
    if points <= 0:
        raise ValueError("Pontos devem ser positivos")
    if activity.code == "duolingo_beconfident" and (
        int(points) != 5 or (unit_threshold is not None and int(unit_threshold) != 5)
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
        reason=None,
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


def list_resources(session: Session, *, include_inactive: bool = False) -> list[Resource]:
    """Recursos na ordem definida pela administração."""

    statement = select(Resource).order_by(Resource.position, Resource.title)
    if not include_inactive:
        statement = statement.where(Resource.active.is_(True))
    return list(session.scalars(statement).all())


def normalize_resource_url(url: str) -> str:
    """Aceita apenas http/https.

    A lista é renderizada como HTML, então um esquema como ``javascript:``
    viraria execução de script na página do aluno.
    """

    candidate = (url or "").strip()
    if not candidate:
        raise ValueError("Informe o link do recurso")
    lowered = candidate.lower()
    if not lowered.startswith(("http://", "https://")):
        raise ValueError(f"Link deve começar com http:// ou https://: {candidate}")
    if len(candidate) > 500:
        raise ValueError("Link excede 500 caracteres")
    return candidate


def replace_resources(
    session: Session,
    *,
    actor: User,
    entries: Sequence[dict[str, Any]],
) -> list[Resource]:
    """Reescreve a lista inteira na ordem recebida.

    Recursos são conteúdo puro — nada em submissões ou ledger aponta para eles —
    então recriar a lista é seguro e mantém a edição em uma única tela.
    """

    require_admin(actor)
    normalized: list[dict[str, Any]] = []
    for entry in entries:
        title = str(entry.get("title") or "").strip()
        if not title:
            raise ValueError("Todo recurso precisa de um título")
        if len(title) > 180:
            raise ValueError(f"Título excede 180 caracteres: {title[:40]}…")
        normalized.append(
            {
                "title": title,
                "url": normalize_resource_url(str(entry.get("url") or "")),
                "description": str(entry.get("description") or "").strip(),
                "active": bool(entry.get("active", True)),
            }
        )
    before = int(session.scalar(select(func.count(Resource.id))) or 0)
    for resource in session.scalars(select(Resource)).all():
        session.delete(resource)
    session.flush()
    created: list[Resource] = []
    for position, entry in enumerate(normalized, start=1):
        resource = Resource(position=position, **entry)
        session.add(resource)
        created.append(resource)
    session.flush()
    add_audit(
        session,
        actor_id=actor.id,
        action="resources_replaced",
        entity_type="resource",
        entity_id="*",
        before={"count": before},
        after={"count": len(created), "titles": [item["title"] for item in normalized]},
    )
    return created


def get_goal_configuration(session: Session) -> GoalConfiguration:
    """Configuração única de metas, criada com o padrão na primeira leitura."""

    configuration = session.get(GoalConfiguration, 1)
    if configuration is None:
        configuration = GoalConfiguration(id=1)
        session.add(configuration)
        session.flush()
    return configuration


def save_goal_configuration(
    session: Session,
    *,
    actor: User,
    weekly_lesson_goal: int,
) -> GoalConfiguration:
    """Define quantas lições por semana a equipe deve cumprir.

    A meta é orientativa: nenhuma pontuação depende dela e o ledger não muda.
    """

    require_admin(actor)
    goal = int(weekly_lesson_goal)
    if goal < 1:
        raise ValueError("A meta semanal deve ser de pelo menos uma lição")
    if goal > 200:
        raise ValueError("A meta semanal não pode passar de 200 lições")
    configuration = get_goal_configuration(session)
    before = {"weekly_lesson_goal": configuration.weekly_lesson_goal}
    configuration.weekly_lesson_goal = goal
    configuration.updated_by_id = actor.id
    add_audit(
        session,
        actor_id=actor.id,
        action="goal_configuration_updated",
        entity_type="goal_configuration",
        entity_id="1",
        before=before,
        after={"weekly_lesson_goal": configuration.weekly_lesson_goal},
    )
    return configuration


def set_activity_active(
    session: Session,
    *,
    actor: User,
    activity_id: str,
    active: bool,
) -> Activity:
    """Toggle an activity from the catalog's single source-of-truth checkbox."""

    require_admin(actor)
    activity = session.get(Activity, activity_id)
    if activity is None:
        raise LookupError("Atividade não encontrada")
    if activity.archived_at is not None:
        raise ValueError("Atividade arquivada não pode ser reativada")
    before = {"active": activity.active}
    activity.active = bool(active)
    add_audit(
        session,
        actor_id=actor.id,
        action="activity_activation_changed",
        entity_type="activity",
        entity_id=activity.id,
        before=before,
        after={"active": activity.active},
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
        reason=None,
        after={"code": activity.code, "name": activity.name, "points": activity.points},
    )
    return activity


def save_user(
    session: Session,
    *,
    actor: User,
    username: str,
    display_name: str,
    role: Role | str = Role.STUDENT,
    active: bool = True,
    user_id: str | None = None,
    auth_user_id: str | None = None,
    reminders_enabled: bool | None = None,
    contas: Contas | None = None,
) -> User:
    """Cria ou atualiza um perfil.

    ``user_id`` seleciona quem editar; ``auth_user_id`` é o identificador da
    conta no Supabase Auth, usado como chave do perfil na criação — os dois são
    coisas distintas e não devem ser confundidos.
    """

    require_admin(actor)
    normalized = normalize_username(username)
    requested_role = Role(role)
    user = (
        session.get(User, user_id)
        if user_id
        else session.scalar(select(User).where(User.username == normalized))
    )
    before = None
    username_mudou = False
    if user is None:
        # O id do perfil é o mesmo da conta no Supabase Auth. Quem já criou a
        # conta passa o identificador; sem ele o perfil nasceria sem login.
        user = User(
            id=str(auth_user_id) if auth_user_id else new_id(),
            username=normalized,
            display_name=display_name.strip(),
            role=requested_role,
            active=bool(active),
        )
        session.add(user)
        action = "user_created"
    else:
        conflict = session.scalar(
            select(User).where(User.username == normalized, User.id != user.id)
        )
        if conflict is not None:
            raise ValueError("Usuário já pertence a outra conta")
        # `user.role` é StrEnum sobre coluna Text: vindo de uma consulta ele é
        # `str` puro e não tem `.value`. Aqui a leitura é anterior a qualquer
        # atribuição — é o valor que estava no banco —, então era exatamente
        # onde o AttributeError caía. `str()` serve aos dois tipos e devolve a
        # string simples que a coluna JSON da auditoria deve guardar.
        before = {
            "username": user.username,
            "display_name": user.display_name,
            "role": str(user.role),
            "active": user.active,
        }
        if (
            user.role == Role.ADMIN
            and user.active
            and (requested_role != Role.ADMIN or not active)
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
                raise ValueError(
                    "Não é permitido desativar o último administrador ativo"
                )
        username_mudou = user.username != normalized
        user.username = normalized
        user.display_name = display_name.strip() or user.display_name
        user.role = requested_role
        user.active = bool(active)
        if reminders_enabled is not None:
            user.reminders_enabled = bool(reminders_enabled)
        action = "user_updated"
        if before["role"] != requested_role.value or before["active"] != bool(active):
            # Mudança de papel ou desativação precisa alcançar a sessão já
            # emitida. Só o Supabase Auth consegue invalidá-la; marcar o perfil
            # deixaria o acesso vivo até o token expirar sozinho.
            if contas is not None:
                if not active:
                    contas.desativar(user.id)
                elif not before["active"]:
                    # O par de `desativar`. Voltar o perfil para `active` não
                    # levanta o ban: sem esta chamada o aluno reativado
                    # aparece ativo na tela e não entra mais, sem sintoma
                    # nenhum até ele tentar.
                    contas.reativar(user.id)
    session.flush()
    if username_mudou:
        if contas is not None:
            # O login traduz o username em endereço a cada tentativa e consulta
            # o Auth; o perfil sozinho não o alcança. Depois do `flush` de
            # propósito: se o banco for recusar o nome, que recuse antes de a
            # conta mudar.
            #
            # A ordem é a mesma da exclusão em `archive_or_delete_user`: o
            # perfil muda dentro da transação, o Auth logo depois, e quem chama
            # confirma no fim. Se o Auth recusar, a exceção sobe e o rollback
            # desfaz o perfil — não há escrita compensatória que possa falhar
            # por sua vez.
            contas.atualizar_username(user.id, normalized)
        elif exige_conta_no_auth(session):
            # Sem a fachada não há como mover a conta, e gravar só o perfil
            # recria em silêncio a divergência que a linha acima conserta.
            # Falhar alto é a única saída honesta: o chamador de hoje sempre
            # passa `contas`, mas um futuro não tem como saber disso.
            #
            # Só no PostgreSQL. No SQLite não existe schema `auth`, renomear um
            # perfil solto é legítimo, e não há nada para manter em passo —
            # exigir a fachada ali quebraria desenvolvimento para proteger um
            # caso que não existe.
            raise ValueError(
                "Renomear exige a fachada de contas: sem ela o login "
                "continuaria aceitando só o nome antigo."
            )
    add_audit(
        session,
        actor_id=actor.id,
        action=action,
        entity_type="user",
        entity_id=user.id,
        reason=None,
        before=before,
        after={
            "display_name": user.display_name,
            "username": user.username,
            # Aqui `user.role` costuma ser o enum recém-atribuído, mas depender
            # disso é depender de o objeto não ter sido recarregado no caminho.
            "role": str(user.role),
            "active": user.active,
        },
    )
    return user


def create_user_account(
    session: Session,
    *,
    actor: User,
    contas: Contas,
    username: str,
    display_name: str,
    role: Role | str = Role.STUDENT,
) -> tuple[User, str]:
    """Cria a conta de acesso e o perfil correspondente.

    A ordem importa: a conta no Supabase Auth vem primeiro porque é ela que
    define o identificador, e o perfil referencia ``auth.users`` por chave
    estrangeira. Fazer o contrário criaria um perfil sem login possível.

    A aplicação não guarda senha. A temporária devolvida aqui é exibida uma
    única vez ao administrador, que a entrega ao aluno.
    """

    require_admin(actor)
    normalized = normalize_username(username)
    if session.scalar(select(User.id).where(func.lower(User.username) == normalized)):
        raise ValueError("Já existe uma conta com esse usuário")
    auth_user_id, temporary_password = contas.criar(
        username=normalized, display_name=display_name
    )
    try:
        user = save_user(
            session,
            actor=actor,
            username=normalized,
            display_name=display_name,
            role=role,
            active=True,
            auth_user_id=auth_user_id,
        )
    except Exception:
        # O perfil não entrou; a conta recém-criada ficaria órfã no Auth,
        # ocupando o nome de usuário sem nada do outro lado.
        contas.remover(auth_user_id)
        raise
    # A senha devolvida acima é temporária e passa pela mão do administrador.
    # A conta nasce devendo a troca; quem libera é `concluir_troca_de_senha`.
    user.must_change_password = True
    return user, temporary_password


def reset_user_password(
    session: Session,
    *,
    actor: User,
    contas: Contas,
    user_id: str,
) -> str:
    """Gera uma senha temporária nova no Supabase Auth.

    Substitui a recuperação por e-mail: como os endereços das contas são
    internos, ninguém recebe link. O administrador gera e entrega.
    """

    require_admin(actor)
    user = session.get(User, user_id)
    if user is None or user.archived_at is not None:
        raise LookupError("Usuário não encontrado")
    temporary_password = contas.redefinir_senha(user.id)
    # Redefinir devolve o aluno à mesma situação da criação: a senha em uso
    # voltou a ser temporária, e a obrigação de trocar volta com ela. É por
    # isso que a marca é "senha temporária" e não "primeiro acesso".
    user.must_change_password = True
    add_audit(
        session,
        actor_id=actor.id,
        action="user_password_reset",
        entity_type="user",
        entity_id=user.id,
        reason=None,
    )
    return temporary_password


def concluir_troca_de_senha(session: Session, *, actor: User) -> None:
    """Libera o ator depois de a senha temporária ter sido substituída.

    Chamada só quando o Supabase Auth confirmou a troca. A senha em si nunca
    passa por aqui: quem a altera é `supabase_auth.trocar_senha`, com o token
    do próprio usuário, e esta função só desfaz a obrigação que a senha
    temporária tinha criado.

    O ator é o dono da própria marca — não há `require_admin`. Limpar a marca
    de outra pessoa não é uma operação que exista.
    """

    actor.must_change_password = False
    add_audit(
        session,
        actor_id=actor.id,
        action="user_password_changed",
        entity_type="user",
        entity_id=actor.id,
        reason=None,
    )


def count_user_references(session: Session, user_id: str) -> int:
    """Conta o histórico que impede a exclusão física de uma conta.

    A interface usa esta contagem para dizer, **antes** da confirmação, se a
    conta será arquivada ou removida de vez, e ``archive_or_delete_user`` usa a
    mesma para decidir. É de propósito que seja a mesma função: se a tela
    contasse por um critério e a decisão por outro, o aviso prometeria uma
    coisa e o banco faria outra — e o erro só apareceria depois de a conta ter
    sumido.

    Espelha ``count_activity_references``, que já resolvia isso no catálogo.
    """

    return sum(
        int(value or 0)
        for value in (
            session.scalar(
                select(func.count(Submission.id)).where(
                    Submission.student_id == user_id
                )
            ),
            session.scalar(
                select(func.count(LedgerTransaction.id)).where(
                    LedgerTransaction.student_id == user_id
                )
            ),
            session.scalar(
                select(func.count(AuditLog.id)).where(AuditLog.actor_id == user_id)
            ),
        )
    )


def archive_or_delete_user(
    session: Session,
    *,
    actor: User,
    user_id: str,
    contas: Contas | None = None,
) -> str:
    require_admin(actor)
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
    references = count_user_references(session, user.id)
    add_audit(
        session,
        actor_id=actor.id,
        action="user_archived" if references else "user_deleted",
        entity_type="user",
        entity_id=user.id,
        reason=None,
        before={"active": user.active, "archived": user.archived_at is not None},
    )
    if references:
        user.active = False
        user.archived_at = utcnow()
        if contas is not None:
            # Banir antes de confirmar é o lado seguro do erro: se o commit
            # falhar, a conta fica sem acesso em vez de ficar acessível.
            contas.desativar(user.id)
        return "archived"
    session.delete(user)
    # A ordem importa: o perfil é apagado primeiro, ainda dentro da transação.
    # Se a remoção no Auth falhar logo abaixo, o rollback devolve o perfil e os
    # dois lados continuam de acordo. Fazer o inverso apagaria o perfil por
    # cascade e deixaria o DELETE do ORM sem linha para remover.
    session.flush()
    if contas is not None:
        contas.remover(user.id)
    return "deleted"


def count_activity_references(session: Session, activity_id: str) -> int:
    """Conta submissões e lançamentos que impedem a exclusão física.

    A interface usa esta contagem para dizer, antes da confirmação, se a
    atividade será arquivada ou removida definitivamente.
    """

    return sum(
        int(value or 0)
        for value in (
            session.scalar(
                select(func.count(Submission.id)).where(
                    Submission.activity_id == activity_id
                )
            ),
            session.scalar(
                select(func.count(LedgerTransaction.id)).where(
                    LedgerTransaction.activity_id == activity_id
                )
            ),
        )
    )


def archive_or_delete_activity(
    session: Session,
    *,
    actor: User,
    activity_id: str,
) -> str:
    require_admin(actor)
    activity = session.get(Activity, activity_id)
    if activity is None:
        raise LookupError("Atividade não encontrada")
    if activity.code == "duolingo_beconfident":
        raise ValueError(
            "A atividade principal Duolingo/BeConfident não pode ser excluída"
        )
    references = count_activity_references(session, activity.id)
    add_audit(
        session,
        actor_id=actor.id,
        action="activity_archived" if references else "activity_deleted",
        entity_type="activity",
        entity_id=activity.id,
        reason=None,
        before={
            "active": activity.active,
            "archived": activity.archived_at is not None,
        },
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
) -> LedgerTransaction:
    require_admin(actor)
    if not points:
        raise ValueError("O ajuste não pode ser zero")
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
        description="Ajuste administrativo de pontos",
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
        reason=None,
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
                selectinload(Submission.files),
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

    return ledger_rows(session, student_id=student_id, start=start, end=end)


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
    "count_activity_references",
    "count_submissions",
    "create_activity",
    "create_points_adjustment",
    "create_user_account",
    "get_goal_configuration",
    "get_submission_file_url_for_user",
    "get_submission_for_user",
    "list_resources",
    "list_review_queue",
    "list_submissions",
    "normalize_resource_url",
    "replace_resources",
    "reset_user_password",
    "review_submission",
    "save_activity_changes",
    "save_goal_configuration",
    "save_user",
    "set_activity_active",
    "submit_evidence",
]
