from __future__ import annotations

from dataclasses import replace
from io import BytesIO

import pytest
from docx import Document
from pypdf import PdfWriter
from sqlalchemy import select

from english_leaderboard.authz import AuthorizationError
from english_leaderboard.document_processing import (
    DocumentValidationError,
    process_document_bytes,
)
from english_leaderboard.models import (
    Activity,
    Role,
    Submission,
    SubmissionFile,
    SubmissionStatus,
    User,
)
from english_leaderboard.services import (
    UploadPayload,
    get_submission_file_for_user,
    submit_evidence,
)


def _docx_bytes() -> bytes:
    document = Document()
    document.add_paragraph("Anotações seguras da atividade de inglês.")
    output = BytesIO()
    document.save(output)
    return output.getvalue()


def _pdf_bytes() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


@pytest.mark.parametrize(
    ("filename", "payload", "kind"),
    [
        ("anotacoes.txt", "texto em português".encode(), "txt"),
        ("anotacoes.docx", _docx_bytes(), "docx"),
        ("anotacoes.pdf", _pdf_bytes(), "pdf"),
    ],
)
def test_supported_documents_are_sniffed_and_extracted(
    filename, payload, kind, settings
) -> None:
    processed = process_document_bytes(
        payload,
        filename,
        max_bytes=settings.max_upload_bytes,
        max_pdf_pages=settings.max_pdf_pages,
    )
    assert processed.file_kind == kind
    assert len(processed.sha256) == 64


def test_extension_mismatch_and_legacy_doc_are_rejected(settings) -> None:
    with pytest.raises(DocumentValidationError, match="extensão"):
        process_document_bytes(
            _pdf_bytes(),
            "falso.txt",
            max_bytes=settings.max_upload_bytes,
            max_pdf_pages=settings.max_pdf_pages,
        )
    with pytest.raises(DocumentValidationError, match="converta"):
        process_document_bytes(
            b"documento",
            "antigo.doc",
            max_bytes=settings.max_upload_bytes,
            max_pdf_pages=settings.max_pdf_pages,
        )


def test_docx_expansion_and_pdf_render_budgets_are_enforced(settings) -> None:
    with pytest.raises(DocumentValidationError, match="descompactação"):
        process_document_bytes(
            _docx_bytes(),
            "grande.docx",
            max_bytes=settings.max_upload_bytes,
            max_pdf_pages=settings.max_pdf_pages,
            max_document_expanded_bytes=100,
        )
    with pytest.raises(DocumentValidationError, match="renderização"):
        process_document_bytes(
            _pdf_bytes(),
            "grande.pdf",
            max_bytes=settings.max_upload_bytes,
            max_pdf_pages=settings.max_pdf_pages,
            max_pdf_render_pixels=100,
        )


def test_txt_submission_skips_ocr_and_download_is_owner_protected(
    session, users, settings, monkeypatch
) -> None:
    activity = session.scalar(
        select(Activity).where(Activity.code == "impact_summary")
    )
    activity.requires_images = True
    summary = (
        "Eu aprendi novas palavras e revisei exemplos importantes para a equipe. "
        "Também escrevi anotações em português para praticar o conteúdo depois "
        "e compartilhar o aprendizado com meus colegas durante o treinamento."
    )
    monkeypatch.setattr(
        "english_leaderboard.services.create_ocr_engine",
        lambda: pytest.fail("OCR não deveria ser carregado para TXT"),
    )
    result = submit_evidence(
        session,
        actor=users[Role.STUDENT],
        activity_id=activity.id,
        uploads=[UploadPayload("evidencia.txt", b"conteudo textual seguro")],
        settings=settings,
        title="Atividade Impact",
        summary=summary,
    )
    session.commit()
    assert result.status == SubmissionStatus.NEEDS_REVIEW
    stored_file = session.scalar(
        select(SubmissionFile).where(SubmissionFile.submission_id == result.submission_id)
    )
    assert stored_file.file_kind == "txt"
    other = User(
        email="other-student@example.org",
        display_name="Other",
        role=Role.STUDENT,
    )
    session.add(other)
    session.commit()
    with pytest.raises(AuthorizationError):
        get_submission_file_for_user(
            session,
            actor=other,
            file_id=stored_file.id,
            settings=settings,
        )


def test_submission_enforces_file_count_and_aggregate_budget(
    session, users, settings
) -> None:
    activity = session.scalar(
        select(Activity).where(Activity.code == "impact_summary")
    )
    limited = replace(
        settings,
        max_upload_files=1,
        max_upload_total_bytes=8,
        max_upload_bytes=8,
    )
    with pytest.raises(ValueError, match="no máximo 1"):
        submit_evidence(
            session,
            actor=users[Role.STUDENT],
            activity_id=activity.id,
            uploads=[UploadPayload("a.txt", b"a"), UploadPayload("b.txt", b"b")],
            settings=limited,
        )
    with pytest.raises(ValueError, match="limite total"):
        submit_evidence(
            session,
            actor=users[Role.STUDENT],
            activity_id=activity.id,
            uploads=[UploadPayload("a.txt", b"123456789")],
            settings=limited,
        )
    assert session.scalar(select(Submission.id)) is None
