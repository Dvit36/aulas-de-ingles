"""Fluxo de submissão da arquitetura oficial: memória, Storage, PostgreSQL.

O caminho é: sessão do Supabase Auth identifica o dono; os bytes são validados
e analisados **em memória**; o binário vai para o bucket privado; só metadados
e a ``storage_key`` entram no banco.

O filesystem local nunca guarda nada. O pipeline de análise já opera sobre
bytes — OCR aceita ``bytes``, PDF e DOCX abrem com ``BytesIO`` —, então não há
arquivo intermediário a limpar. Quando alguma biblioteca exigir um caminho,
``tempfiles.arquivo_temporario`` cuida da remoção garantida.

SHA-256 e pHash continuam sendo calculados sobre os bytes originais: são a
base das regras de duplicidade e não podem mudar por causa da troca de
armazenamento.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy.engine import Connection

from .config import Settings
from .document_processing import DocumentValidationError, process_document_bytes
from .image_processing import ImagePolicy, ImageValidationError, analyze_image_bytes
from .storage import StorageGateway
from .storage_budget import LimitesStorage
from .storage_service import ArquivoRegistrado, salvar_arquivo

# Categoria do objeto conforme a natureza do arquivo. Imagens de comprovação
# vão para uploads; PDF, DOCX e TXT para documents.
CATEGORIA_IMAGEM = "uploads"
CATEGORIA_DOCUMENTO = "documents"


class EnvioRejeitado(ValueError):
    """O arquivo não passou nas validações antes de qualquer persistência."""


@dataclass(frozen=True, slots=True)
class ArquivoEnviado:
    filename: str
    dados: bytes
    content_type: str | None = None


@dataclass(slots=True)
class ResultadoProcessamento:
    registrados: list[ArquivoRegistrado] = field(default_factory=list)
    textos_ocr: list[str] = field(default_factory=list)
    checksums: list[str] = field(default_factory=list)
    phashes: list[str] = field(default_factory=list)
    rejeitados: list[tuple[str, str]] = field(default_factory=list)

    @property
    def total_bytes(self) -> int:
        return sum(item.file_size for item in self.registrados)


def limites_de(settings: Settings) -> LimitesStorage:
    return LimitesStorage(
        max_total_bytes=settings.storage_max_total_bytes,
        max_monthly_egress_bytes=settings.storage_max_monthly_egress_bytes,
    )


def _validar_conjunto(arquivos: list[ArquivoEnviado], settings: Settings) -> None:
    """Limites do lote, antes de tocar em qualquer arquivo individualmente."""

    if not arquivos:
        raise EnvioRejeitado("Nenhum arquivo enviado")
    if len(arquivos) > settings.max_upload_files:
        raise EnvioRejeitado(
            f"São aceitos até {settings.max_upload_files} arquivos por envio"
        )
    total = sum(len(item.dados) for item in arquivos)
    if total > settings.max_upload_total_bytes:
        raise EnvioRejeitado("O conjunto de arquivos excede o limite total")
    for item in arquivos:
        if not item.dados:
            raise EnvioRejeitado(f"Arquivo vazio: {item.filename}")
        if len(item.dados) > settings.max_upload_bytes:
            raise EnvioRejeitado(
                f"{item.filename} excede o limite de "
                f"{settings.max_upload_bytes // (1024 * 1024)} MB por arquivo"
            )


def processar_envio(
    conexao: Connection,
    gateway: StorageGateway,
    *,
    submission_id: str | UUID,
    student_id: str | UUID,
    arquivos: list[ArquivoEnviado],
    settings: Settings,
    ocr_engine=None,
) -> ResultadoProcessamento:
    """Valida, analisa em memória, envia ao Storage e grava os metadados.

    ``student_id`` vem da sessão autenticada, nunca da interface. A chave do
    objeto é derivada dele, então um identificador forjado não teria como
    apontar para a pasta de outro aluno — e ainda seria barrado pela política
    do bucket e pela constraint do banco.
    """

    _validar_conjunto(arquivos, settings)
    politica = ImagePolicy(
        max_bytes=settings.max_upload_bytes,
        allowed_formats=frozenset(settings.allowed_image_formats),
        min_width=settings.min_image_width,
        min_height=settings.min_image_height,
        blur_threshold=settings.min_laplacian_variance,
    )
    resultado = ResultadoProcessamento()

    for posicao, arquivo in enumerate(arquivos):
        try:
            analisado = _analisar(arquivo, politica, settings, ocr_engine)
        except (ImageValidationError, DocumentValidationError, EnvioRejeitado) as erro:
            # Arquivo inválido não chega ao Storage: nada a compensar depois.
            resultado.rejeitados.append((arquivo.filename, str(erro)))
            continue

        registrado = salvar_arquivo(
            conexao,
            gateway,
            submission_id=submission_id,
            student_id=student_id,
            filename=arquivo.filename,
            dados=analisado["dados"],
            content_type=analisado["mime"],
            categoria=analisado["categoria"],
            extensao=analisado["extensao"],
            bucket=settings.storage_bucket,
            limites=limites_de(settings),
            ocr_text=analisado["texto"] or None,
            phash=analisado["phash"],
            position=posicao,
        )
        resultado.registrados.append(registrado)
        resultado.checksums.append(registrado.checksum_sha256)
        if analisado["texto"]:
            resultado.textos_ocr.append(analisado["texto"])
        if analisado["phash"]:
            resultado.phashes.append(analisado["phash"])

    if not resultado.registrados:
        motivos = "; ".join(f"{nome}: {erro}" for nome, erro in resultado.rejeitados)
        raise EnvioRejeitado(f"Nenhum arquivo pôde ser aceito. {motivos}")
    return resultado


def _analisar(
    arquivo: ArquivoEnviado,
    politica: ImagePolicy,
    settings: Settings,
    ocr_engine,
) -> dict[str, object]:
    """Roteia entre imagem e documento, sempre sobre os bytes em memória."""

    nome = (arquivo.filename or "").lower()
    parece_imagem = nome.endswith((".jpg", ".jpeg", ".png", ".webp"))

    if parece_imagem:
        analisado = analyze_image_bytes(arquivo.dados, policy=politica)
        texto = ""
        if ocr_engine is not None:
            from .ocr import extract_text

            # O OCR recebe bytes: nenhum arquivo é criado no disco.
            texto = extract_text(analisado.original_bytes, engine=ocr_engine).text
        return {
            "dados": analisado.original_bytes,
            "mime": analisado.mime_type,
            "extensao": analisado.extension.lstrip("."),
            "categoria": CATEGORIA_IMAGEM,
            "texto": texto,
            # pHash e SHA-256 seguem vindo dos bytes originais: a troca de
            # armazenamento não pode alterar as regras antifraude.
            "phash": analisado.phash,
            "sha256": analisado.sha256,
        }

    documento = process_document_bytes(
        arquivo.dados,
        arquivo.filename,
        max_bytes=settings.max_upload_bytes,
        max_pdf_pages=settings.max_pdf_pages,
        max_document_expanded_bytes=settings.max_document_expanded_bytes,
        max_pdf_render_pixels=settings.max_pdf_render_pixels,
        ocr_engine=ocr_engine,
    )
    return {
        "dados": documento.original_bytes,
        "mime": documento.mime_type,
        "extensao": documento.extension.lstrip("."),
        "categoria": CATEGORIA_DOCUMENTO,
        "texto": documento.extracted_text,
        "phash": None,
        "sha256": documento.sha256,
    }


__all__ = [
    "CATEGORIA_DOCUMENTO",
    "CATEGORIA_IMAGEM",
    "ArquivoEnviado",
    "EnvioRejeitado",
    "ResultadoProcessamento",
    "limites_de",
    "processar_envio",
]
