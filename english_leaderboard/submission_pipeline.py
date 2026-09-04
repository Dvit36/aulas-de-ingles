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
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .config import Settings
from .document_processing import DocumentValidationError, process_document_bytes
from .image_processing import (
    ImagePolicy,
    ImageValidationError,
    analyze_image_bytes,
    phash_distance,
    prepare_ocr_variants,
)
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
    # Paralelas às **imagens** do lote, na ordem de envio. Documento não entra:
    # a comparação perceptual é de imagem, e a dele é por checksum exato.
    duplicatas_exatas: list[bool] = field(default_factory=list)
    duplicatas_similares: list[bool] = field(default_factory=list)

    @property
    def total_bytes(self) -> int:
        return sum(item.file_size for item in self.registrados)


# Uma passada de OCR abaixo de qualquer um dos dois não convence: vale gastar
# as duas passadas extras nas variantes tratadas e ficar com a melhor.
OCR_CONFIANCA_MINIMA = 0.60
OCR_TEXTO_MINIMO = 10


class _MotorOCR:
    """Guarda o motor de OCR do envio e o cria só quando alguém precisar.

    Instanciar o RapidOCR custa segundos e memória, e boa parte dos envios não
    precisa dele: PDF com texto extraível, DOCX e TXT nunca chegam a pedir. Um
    envio que precisa, por outro lado, não pode pagar isso uma vez por arquivo
    — daí o motor ficar aqui, e não dentro do laço.
    """

    __slots__ = ("_engine",)

    def __init__(self, engine=None) -> None:
        self._engine = engine

    def obter(self):
        if self._engine is None:
            from .ocr import create_ocr_engine

            self._engine = create_ocr_engine()
        return self._engine


def sanitizar_nome(filename: str) -> str:
    """Reduz o nome vindo do cliente ao que pode virar metadado.

    O nome não entra na chave do Storage — ela é montada a partir de UUIDs —,
    mas é gravado em ``submission_files.filename`` e exibido na tela de
    revisão. Tira o byte nulo, fica só com o último segmento do caminho e
    corta em 255. Nome vazio vira ``upload``.
    """

    limpo = Path((filename or "upload").replace("\x00", "")).name
    return limpo[:255] or "upload"


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
    motor = _MotorOCR(ocr_engine)
    analisados: list[dict[str, object]] = []
    for arquivo in arquivos:
        try:
            analisados.append(
                _analisar(arquivo, politica, settings, motor, activity_code)
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

    _ocr_das_imagens(analisados, motor)
    resultado.documento_duplicado = _documento_ja_enviado(conexao, analisados)
    # O histórico precisa ser lido antes da fase 2: depois dela os arquivos
    # deste envio já estariam em `submission_files` e casariam consigo mesmos.
    candidatos = _candidatos_perceptuais(conexao)

    # Fase 2: só agora os binários vão para o bucket e os metadados para o banco.
    imagens: list[tuple[str, str, str]] = []
    for posicao, (arquivo, analisado) in enumerate(
        zip(arquivos, analisados, strict=True)
    ):
        registrado = salvar_arquivo(
            conexao,
            gateway,
            submission_id=submission_id,
            student_id=student_id,
            filename=sanitizar_nome(arquivo.filename),
            dados=analisado["dados"],
            content_type=analisado["mime"],
            categoria=analisado["categoria"],
            extensao=analisado["extensao"],
            bucket=settings.storage_bucket,
            limites=limites_de(settings),
            ocr_text=analisado["texto"] or None,
            phash=analisado["phash"],
            position=posicao,
            width=analisado["width"],
            height=analisado["height"],
            page_count=analisado["paginas"],
        )
        resultado.registrados.append(registrado)
        resultado.checksums.append(registrado.checksum_sha256)
        if analisado["texto"]:
            resultado.textos_ocr.append(analisado["texto"])
        if analisado["phash"]:
            resultado.phashes.append(analisado["phash"])
        if analisado["categoria"] == CATEGORIA_IMAGEM:
            imagens.append(
                (registrado.id, str(analisado["sha256"]), str(analisado["phash"]))
            )

    resultado.duplicatas_exatas, resultado.duplicatas_similares = _registrar_duplicatas(
        conexao,
        submission_id=submission_id,
        student_id=student_id,
        imagens=imagens,
        candidatos=candidatos,
        distancia_maxima=settings.phash_distance_threshold,
    )
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


def _ocr_com_variantes(dados: bytes, engine) -> str:
    """Lê a imagem uma vez; se o resultado for fraco, tenta as versões tratadas.

    Print escuro, borrado ou de baixo contraste é o caso comum, e é justamente
    onde a passada única falha. ``contrast`` equaliza e ``threshold`` binariza;
    fica a leitura com mais texto, e a confiança desempata.
    """

    from .ocr import OCRExecutionError, extract_text

    try:
        # As variantes são arrays em memória: nada é criado no disco.
        variantes = prepare_ocr_variants(dados)
        primeira = extract_text(variantes["original"], engine=engine)
        candidatas = [primeira]
        if (
            primeira.confidence or 0.0
        ) < OCR_CONFIANCA_MINIMA or len(primeira.text.strip()) < OCR_TEXTO_MINIMO:
            candidatas.extend(
                extract_text(variantes[nome], engine=engine)
                for nome in ("contrast", "threshold")
            )
    except OCRExecutionError:
        # Motor de OCR que quebra é problema do motor, não prova inválida. A
        # imagem segue registrada, sem texto; as regras decidem o que fazer
        # com uma evidência que não rendeu leitura.
        return ""
    melhor = max(
        candidatas,
        key=lambda leitura: (len(leitura.text.strip()), leitura.confidence or 0.0),
    )
    return melhor.text


def _ocr_das_imagens(analisados: list[dict[str, object]], motor: _MotorOCR) -> None:
    """Preenche o texto das imagens do lote, no lugar em que já estava."""

    imagens = [
        analisado
        for analisado in analisados
        if analisado["categoria"] == CATEGORIA_IMAGEM
    ]
    if not imagens:
        return
    engine = motor.obter()
    for analisado in imagens:
        analisado["texto"] = _ocr_com_variantes(analisado["dados"], engine)


def _candidatos_perceptuais(conexao: Connection) -> list[tuple[str, str, str, str]]:
    """Arquivos já enviados que podem colidir com as imagens deste envio.

    Só entram os que têm pHash, ou seja, imagens: documento é confrontado por
    checksum exato em ``_documento_ja_enviado``. O dono sai do próprio
    ``submission_files``, que já o guarda — não é preciso passar por
    ``submissions`` para saber de quem é o arquivo.
    """

    return [
        (str(linha[0]), str(linha[1]), str(linha[2]), str(linha[3]))
        for linha in conexao.execute(
            text(
                "select id, checksum_sha256, phash, student_id"
                " from submission_files where phash is not null"
            )
        ).all()
    ]


def _registrar_duplicatas(
    conexao: Connection,
    *,
    submission_id: str | UUID,
    student_id: str | UUID,
    imagens: list[tuple[str, str, str]],
    candidatos: list[tuple[str, str, str, str]],
    distancia_maxima: int,
) -> tuple[list[bool], list[bool]]:
    """Confronta cada imagem com o histórico e com as anteriores do lote.

    Checksum igual é cópia literal e vale ``exact``; pHash perto é a mesma tela
    recortada, recomprimida ou reenviada em outro formato, e vale ``similar``.
    As linhas em ``duplicate_matches`` são o que o painel de comparação de
    evidências exibe; as duas listas de sinalizadores alimentam as regras.

    O pipeline não decide nada com isso: registra e devolve.
    """

    dono = str(student_id).lower()
    exatas = [False] * len(imagens)
    similares = [False] * len(imagens)

    def registrar(file_id: str, matched_id: str, kind: str, distancia: int, mesmo: bool):
        conexao.execute(
            text(
                "insert into duplicate_matches"
                " (id, submission_id, file_id, matched_file_id, kind, distance,"
                "  same_student)"
                " values (:id, :sub, :file, :matched, :kind, :dist, :mesmo)"
            ),
            {
                "id": str(uuid4()),
                "sub": str(submission_id),
                "file": file_id,
                "matched": matched_id,
                "kind": kind,
                "dist": distancia,
                "mesmo": mesmo,
            },
        )

    for indice, (file_id, sha256, phash) in enumerate(imagens):
        for candidato_id, candidato_sha, candidato_phash, candidato_dono in candidatos:
            if sha256 == candidato_sha:
                exatas[indice] = True
                registrar(file_id, candidato_id, "exact", 0, candidato_dono == dono)
                continue
            distancia = phash_distance(phash, candidato_phash)
            if distancia <= distancia_maxima:
                similares[indice] = True
                registrar(
                    file_id,
                    candidato_id,
                    "similar",
                    distancia,
                    candidato_dono == dono,
                )
        # Contra as imagens anteriores do próprio lote, que ainda não estavam
        # no banco quando `candidatos` foi lido.
        for anterior_id, anterior_sha, anterior_phash in imagens[:indice]:
            if sha256 == anterior_sha:
                exatas[indice] = True
                registrar(file_id, anterior_id, "exact", 0, True)
                continue
            distancia = phash_distance(phash, anterior_phash)
            if distancia <= distancia_maxima:
                similares[indice] = True
                registrar(file_id, anterior_id, "similar", distancia, True)
    return exatas, similares


def _analisar(
    arquivo: ArquivoEnviado,
    politica: ImagePolicy,
    settings: Settings,
    motor: _MotorOCR,
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
        # O OCR da imagem roda depois, quando o lote inteiro já passou pela
        # validação: arquivo ruim no fim da lista não custa OCR nos anteriores.
        texto = ""
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
            "width": analisado.width,
            "height": analisado.height,
            "paginas": None,
        }

    opcoes_documento = {
        "max_bytes": settings.max_upload_bytes,
        "max_pdf_pages": settings.max_pdf_pages,
        "max_document_expanded_bytes": settings.max_document_expanded_bytes,
        "max_pdf_render_pixels": settings.max_pdf_render_pixels,
    }
    # Primeiro sem motor: PDF com texto extraível, DOCX e TXT não precisam de
    # OCR e não podem arrastar o modelo para a memória.
    documento = process_document_bytes(
        arquivo.dados, arquivo.filename, **opcoes_documento
    )
    if documento.file_kind == "pdf" and not documento.extracted_text:
        # PDF digitalizado: as páginas são imagem. Só agora vale o motor.
        documento = process_document_bytes(
            arquivo.dados,
            arquivo.filename,
            **opcoes_documento,
            ocr_engine=motor.obter(),
        )
    return {
        "dados": documento.original_bytes,
        "mime": documento.mime_type,
        "extensao": documento.extension.lstrip("."),
        "categoria": CATEGORIA_DOCUMENTO,
        "texto": documento.extracted_text,
        "phash": None,
        "sha256": documento.sha256,
        "width": None,
        "height": None,
        "paginas": documento.page_count,
    }


__all__ = [
    "CATEGORIA_DOCUMENTO",
    "CATEGORIA_IMAGEM",
    "ArquivoEnviado",
    "EnvioRejeitado",
    "ResultadoProcessamento",
    "limites_de",
    "processar_envio",
    "sanitizar_nome",
]
