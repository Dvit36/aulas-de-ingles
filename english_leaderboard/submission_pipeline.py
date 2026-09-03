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

from collections.abc import Mapping
from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import text
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
    """O envio não passou nas validações antes de qualquer persistência.

    Quando a recusa vem de um arquivo específico, ``code``, ``message`` e
    ``details`` repetem os do erro de validação original, e ``arquivo`` diz
    qual foi. É o que permite à camada acima montar o ``RuleCheck`` de
    ``valid_file_content`` sem precisar interpretar o texto da mensagem.
    """

    def __init__(
        self,
        mensagem: str,
        *,
        code: str = "invalid_file",
        message: str | None = None,
        details: Mapping[str, object] | None = None,
        arquivo: str | None = None,
    ) -> None:
        super().__init__(mensagem)
        self.code = code
        self.message = mensagem if message is None else message
        self.details = dict(details or {})
        self.arquivo = arquivo


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
    # Um documento idêntico já enviado — no próprio lote ou em qualquer
    # submissão anterior. O pipeline só sinaliza; a rejeição é da camada acima.
    documento_duplicado: bool = False

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
    activity_code: str | None = None,
) -> ResultadoProcessamento:
    """Valida, analisa em memória, envia ao Storage e grava os metadados.

    ``student_id`` vem da sessão autenticada, nunca da interface. A chave do
    objeto é derivada dele, então um identificador forjado não teria como
    apontar para a pasta de outro aluno — e ainda seria barrado pela política
    do bucket e pela constraint do banco.

    **Duas responsabilidades ficam de fora, por desenho, e quem chamar esta
    função direto está pulando as duas:**

    - **Autorização.** O pipeline não confere nada sobre o ator: não exige
      sessão ativa, não exige ``role == STUDENT`` e não confere se a atividade
      existe, está ativa e não foi arquivada. Ele confia no ``student_id`` que
      recebe. Derivar esse UUID da sessão do Supabase Auth é obrigação do
      chamador — hoje ``services.submit_evidence``.
    - **Regras, decisão, pontuação e auditoria.** O pipeline devolve fatos
      sobre os arquivos: bytes registrados, texto de OCR, checksums, pHashes.
      Ele não avalia ``RuleCheck``, não decide status, não transiciona a
      submissão, não credita ponto e não escreve auditoria. Isso é
      ``analyze_submission_rules`` + ``transition_submission`` +
      ``award_approved_submission`` + ``add_audit``, na camada acima.

    Um arquivo inválido rejeita o lote inteiro, com ``EnvioRejeitado``, antes
    de qualquer objeto chegar ao bucket.
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

    # Fase 1: analisar o lote inteiro em memória. Um arquivo inválido rejeita
    # o envio todo, e a recusa acontece antes de qualquer upload — por isso a
    # análise é separada da persistência em vez de intercalada com ela.
    analisados: list[dict[str, object]] = []
    for arquivo in arquivos:
        try:
            analisados.append(
                _analisar(arquivo, politica, settings, ocr_engine, activity_code)
            )
        except (ImageValidationError, DocumentValidationError) as erro:
            # Nada foi ao Storage: não há objeto órfão a compensar.
            raise EnvioRejeitado(
                f"Nenhum arquivo pôde ser aceito. {arquivo.filename}: {erro}",
                code=getattr(erro, "code", "invalid_file"),
                message=getattr(erro, "message", str(erro)),
                details=getattr(erro, "details", None),
                arquivo=arquivo.filename,
            ) from erro

    resultado.documento_duplicado = _documento_ja_enviado(conexao, analisados)

    # Fase 2: só agora os binários vão para o bucket e os metadados para o banco.
    for posicao, (arquivo, analisado) in enumerate(
        zip(arquivos, analisados, strict=True)
    ):
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

    return resultado


def _documento_ja_enviado(
    conexao: Connection, analisados: list[dict[str, object]]
) -> bool:
    """Documento idêntico no próprio lote ou em qualquer submissão anterior.

    Só documento. Imagem é confrontada por pHash, que tolera recorte e
    recompressão; documento é bit a bit, então o checksum basta e é exato.

    A consulta roda na fase de análise, antes de qualquer gravação: depois da
    fase 2 o próprio arquivo já estaria em ``submission_files`` e casaria
    consigo mesmo.
    """

    duplicado = False
    vistos: set[str] = set()
    for analisado in analisados:
        if analisado["categoria"] != CATEGORIA_DOCUMENTO:
            continue
        checksum = str(analisado["sha256"])
        if checksum in vistos or conexao.execute(
            text("select 1 from submission_files where checksum_sha256 = :ck limit 1"),
            {"ck": checksum},
        ).first():
            duplicado = True
        vistos.add(checksum)
    return duplicado


def _analisar(
    arquivo: ArquivoEnviado,
    politica: ImagePolicy,
    settings: Settings,
    ocr_engine,
    activity_code: str | None = None,
) -> dict[str, object]:
    """Roteia entre imagem e documento, sempre sobre os bytes em memória.

    ``activity_code`` existe só para a regra de plataforma: há atividade cuja
    comprovação é um print e nada mais. Sem ele o roteamento é o de sempre,
    por extensão.
    """

    nome = (arquivo.filename or "").lower()
    parece_imagem = nome.endswith((".jpg", ".jpeg", ".png", ".webp"))

    if not parece_imagem and activity_code == "duolingo_beconfident":
        # Antifraude: a prova de lição é o print da tela. Aceitar documento
        # deixaria qualquer PDF pontuar como se fosse a lição concluída.
        raise DocumentValidationError(
            "Duolingo/BeConfident aceita somente imagens",
            code="image_required",
        )

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
