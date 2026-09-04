"""Fluxo de submissão sem filesystem persistente.

Cobre o que o disco efêmero do Streamlit exige: processamento em memória,
limpeza garantida de qualquer temporário e nenhum resíduo local depois do
envio.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from uuid import uuid4

import numpy as np
import pytest
from pypdf import PdfWriter
from sqlalchemy import create_engine, text

from english_leaderboard.schema import SchemaBase
from english_leaderboard.config import Settings
from english_leaderboard.storage import StorageError
from english_leaderboard.storage_budget import StorageBudgetExceeded
from english_leaderboard.submission_pipeline import (
    CATEGORIA_DOCUMENTO,
    CATEGORIA_IMAGEM,
    ArquivoEnviado,
    EnvioRejeitado,
    processar_envio,
)
from english_leaderboard.tempfiles import arquivo_temporario, diretorio_temporario
from tests.conftest import make_png

ALUNO = "11111111-1111-1111-1111-111111111111"
OUTRO_ALUNO = "22222222-2222-2222-2222-222222222222"
TEXTO_FALSO = "Licao concluida hoje"


def _pdf_em_branco() -> bytes:
    """PDF sem texto extraível: obriga o caminho de OCR de página digitalizada."""

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def _nome_da_variante(source) -> str:
    """Descobre qual variante de `prepare_ocr_variants` chegou ao motor.

    `original` é BGR de três canais; `contrast` e `threshold` são cinza, e só
    o `threshold` é binário.
    """

    if getattr(source, "ndim", 0) == 3:
        return "original"
    return "threshold" if set(np.unique(source).tolist()) <= {0, 255} else "contrast"


class OCRFalso:
    """Motor barato: o suficiente para o pipeline achar texto e seguir."""

    def __init__(self) -> None:
        self.chamadas: list[str] = []

    def __call__(self, source):
        self.chamadas.append(_nome_da_variante(source))
        return ([[None, TEXTO_FALSO, 0.99]], [0.01])


class OCRRuimNaOriginal:
    """Original sai ilegível; a variante de contraste é que rende texto.

    É o caso comum de print escuro ou borrado — o motivo de as variantes
    existirem.
    """

    def __init__(self) -> None:
        self.chamadas: list[str] = []

    def __call__(self, source):
        variante = _nome_da_variante(source)
        self.chamadas.append(variante)
        if variante == "original":
            return ([[None, "Lic", 0.20]], [0.01])
        if variante == "contrast":
            return ([[None, TEXTO_FALSO, 0.95]], [0.01])
        return ([[None, "L1c40", 0.30]], [0.01])


@pytest.fixture(autouse=True)
def motor_ocr_barato(monkeypatch):
    """Nenhum teste deste arquivo carrega o RapidOCR de verdade.

    Depois da criação preguiçosa do motor, um envio de imagem sem
    `ocr_engine` passa a fazer OCR — o que é o comportamento certo, e custaria
    quase três segundos por chamada só para instanciar o modelo.
    """

    monkeypatch.setattr(
        "english_leaderboard.ocr.create_ocr_engine", lambda *a, **k: OCRFalso()
    )


class GatewayFalso:
    def __init__(self) -> None:
        self.objetos: dict[str, bytes] = {}
        self.falhar_em: set[str] = set()

    def upload(self, key: str, data: bytes, content_type: str) -> None:
        if "upload" in self.falhar_em:
            raise StorageError("falha simulada")
        self.objetos[key] = data

    def download(self, key: str) -> bytes:
        return self.objetos[key]

    def remove(self, key: str) -> None:
        if "remove" in self.falhar_em:
            raise StorageError("falha simulada")
        self.objetos.pop(key, None)

    def exists(self, key: str) -> bool:
        return key in self.objetos

    def signed_url(self, key: str, expires_in: int) -> str:
        return f"https://exemplo.invalid/{key}"


@pytest.fixture
def conexao():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    SchemaBase.metadata.create_all(engine)
    with engine.connect() as c:
        yield c
    engine.dispose()


@pytest.fixture
def opcoes(settings: Settings) -> Settings:
    return replace(settings, storage_bucket="student-files")


# ------------------------------------------------------- temporários

def test_temporary_file_is_removed_on_success() -> None:
    with arquivo_temporario(b"conteudo", sufixo=".bin") as caminho:
        assert caminho.is_file()
        assert caminho.read_bytes() == b"conteudo"
        # Só o dono lê e escreve.
        assert oct(caminho.stat().st_mode)[-3:] == "600"
        guardado = caminho

    assert not guardado.exists()
    assert not guardado.parent.exists()


def test_temporary_file_is_removed_when_the_block_raises() -> None:
    """O caso que deixa lixo se a limpeza depender do caminho feliz."""

    guardado: Path | None = None
    with (
        pytest.raises(RuntimeError, match="falha no meio"),
        arquivo_temporario(b"conteudo") as caminho,
    ):
        guardado = caminho
        assert caminho.is_file()
        raise RuntimeError("falha no meio do processamento")

    assert guardado is not None
    assert not guardado.exists()
    assert not guardado.parent.exists()


def test_temporary_directory_is_removed_even_with_content() -> None:
    guardado: Path | None = None
    with pytest.raises(ValueError), diretorio_temporario() as diretorio:
        guardado = diretorio
        (diretorio / "a.txt").write_text("x")
        (diretorio / "sub").mkdir()
        (diretorio / "sub" / "b.txt").write_text("y")
        raise ValueError("interrompido")

    assert guardado is not None and not guardado.exists()


def test_empty_temporary_content_is_refused() -> None:
    with pytest.raises(ValueError, match="conteúdo"), arquivo_temporario(b""):
        pass


# ------------------------------------------------------------ pipeline

def test_image_upload_keeps_checksum_and_phash(conexao, opcoes) -> None:
    """As regras antifraude dependem desses dois: a troca de armazenamento
    não pode alterá-los."""

    gateway = GatewayFalso()
    dados = make_png(seed=3)

    resultado = processar_envio(
        conexao,
        gateway,
        submission_id=uuid4(),
        student_id=ALUNO,
        arquivos=[ArquivoEnviado("print.png", dados)],
        settings=opcoes,
    )

    assert len(resultado.registrados) == 1
    registro = resultado.registrados[0]
    assert registro.checksum_sha256 == hashlib.sha256(dados).hexdigest()
    assert resultado.phashes and len(resultado.phashes[0]) > 0
    # O binário foi para o bucket, não para o disco.
    assert gateway.objetos[registro.storage_key] == dados
    assert f"/{CATEGORIA_IMAGEM}/" in registro.storage_key
    assert registro.storage_key.startswith(f"students/{ALUNO}/")


def test_text_document_goes_to_the_documents_category(conexao, opcoes) -> None:
    gateway = GatewayFalso()

    resultado = processar_envio(
        conexao,
        gateway,
        submission_id=uuid4(),
        student_id=ALUNO,
        arquivos=[ArquivoEnviado("anotacoes.txt", "resumo em português".encode())],
        settings=opcoes,
    )

    chave = resultado.registrados[0].storage_key
    assert f"/{CATEGORIA_DOCUMENTO}/" in chave
    assert chave.endswith(".txt")
    assert resultado.textos_ocr == ["resumo em português"]


def test_nothing_is_written_to_the_local_filesystem(conexao, opcoes, tmp_path) -> None:
    """O disco do Streamlit é efêmero: o envio não pode depender dele.

    O teste apontava `upload_dir` para `tmp_path` e conferia que nada aparecia
    lá. O campo deixou de existir — não havia mais para onde escrever —, então
    o que resta é a garantia direta: processar um envio não cria arquivo local
    nenhum, e `Settings` não oferece diretório de upload a quem tentar.
    """

    gateway = GatewayFalso()
    antes = set(tmp_path.rglob("*"))

    processar_envio(
        conexao,
        gateway,
        submission_id=uuid4(),
        student_id=ALUNO,
        arquivos=[ArquivoEnviado("print.png", make_png(seed=5))],
        settings=opcoes,
    )

    assert set(tmp_path.rglob("*")) == antes
    assert not hasattr(opcoes, "upload_dir")


def test_invalid_file_is_rejected_without_touching_the_bucket(
    conexao, opcoes
) -> None:
    gateway = GatewayFalso()

    with pytest.raises(EnvioRejeitado, match="Nenhum arquivo"):
        processar_envio(
            conexao,
            gateway,
            submission_id=uuid4(),
            student_id=ALUNO,
            arquivos=[ArquivoEnviado("falso.png", b"isto-nao-e-uma-imagem")],
            settings=opcoes,
        )

    assert gateway.objetos == {}
    assert conexao.execute(text("select count(*) from submission_files")).scalar() == 0


def test_a_rejected_file_takes_the_whole_batch_down(conexao, opcoes) -> None:
    """Um arquivo ruim no lote rejeita o envio inteiro.

    Este teste travava o contrário: os arquivos bons eram registrados e o ruim
    virava uma linha em ``rejeitados``. A aceitação parcial foi descartada
    porque creditava o aluno pelos arquivos bons e **descartava o ruim em
    silêncio** — sem ``RuleCheck``, sem auditoria, sem nada na tela de revisão.
    O revisor nunca ficava sabendo que um arquivo tinha sido recusado.

    Tudo-ou-nada é o que ``submit_evidence`` já fazia, e é o que passa a valer
    aqui. O arquivo bom vem primeiro de propósito: prova que a análise do lote
    inteiro acontece antes de qualquer upload, e não que deu sorte na ordem.
    """

    gateway = GatewayFalso()

    with pytest.raises(EnvioRejeitado, match="ruim.png") as excinfo:
        processar_envio(
            conexao,
            gateway,
            submission_id=uuid4(),
            student_id=ALUNO,
            arquivos=[
                ArquivoEnviado("bom.png", make_png(seed=7)),
                ArquivoEnviado("ruim.png", b"nao-e-imagem"),
            ],
            settings=opcoes,
        )

    assert gateway.objetos == {}
    assert conexao.execute(text("select count(*) from submission_files")).scalar() == 0
    # O código do erro original sobrevive: é com ele que a camada acima monta o
    # `RuleCheck` de `valid_file_content` sem ler o texto da mensagem.
    assert excinfo.value.code == "invalid_image"
    assert excinfo.value.arquivo == "ruim.png"


def test_duolingo_activity_refuses_a_document(conexao, opcoes) -> None:
    """Regra antifraude: prova de lição do Duolingo/BeConfident é print, não PDF.

    Sem ela o aluno anexa um documento qualquer e a atividade pontua. O
    pipeline não conhecia a atividade e por isso aceitava; `submit_evidence`
    já recusava com `code="image_required"`.
    """

    gateway = GatewayFalso()

    with pytest.raises(EnvioRejeitado, match="somente imagens") as excinfo:
        processar_envio(
            conexao,
            gateway,
            submission_id=uuid4(),
            student_id=ALUNO,
            arquivos=[ArquivoEnviado("licao.txt", "terminei a lição".encode())],
            settings=opcoes,
            activity_code="duolingo_beconfident",
        )

    assert excinfo.value.code == "image_required"
    assert gateway.objetos == {}


def test_duolingo_activity_still_accepts_an_image(conexao, opcoes) -> None:
    """A regra recusa não-imagem, não aperta o que a atividade de fato aceita."""

    gateway = GatewayFalso()

    resultado = processar_envio(
        conexao,
        gateway,
        submission_id=uuid4(),
        student_id=ALUNO,
        arquivos=[ArquivoEnviado("print.png", make_png(seed=19))],
        settings=opcoes,
        activity_code="duolingo_beconfident",
    )

    assert len(resultado.registrados) == 1
    assert f"/{CATEGORIA_IMAGEM}/" in resultado.registrados[0].storage_key


def test_batch_limits_are_enforced_before_any_upload(conexao, opcoes) -> None:
    gateway = GatewayFalso()
    pequeno = replace(opcoes, max_upload_files=2)

    with pytest.raises(EnvioRejeitado, match="até 2 arquivos"):
        processar_envio(
            conexao,
            gateway,
            submission_id=uuid4(),
            student_id=ALUNO,
            arquivos=[ArquivoEnviado(f"a{i}.png", make_png(seed=i)) for i in range(3)],
            settings=pequeno,
        )

    assert gateway.objetos == {}


def test_oversized_file_is_refused(conexao, opcoes) -> None:
    gateway = GatewayFalso()
    apertado = replace(opcoes, max_upload_bytes=100, max_upload_total_bytes=1000)

    with pytest.raises(EnvioRejeitado, match="excede o limite"):
        processar_envio(
            conexao,
            gateway,
            submission_id=uuid4(),
            student_id=ALUNO,
            arquivos=[ArquivoEnviado("grande.png", make_png(seed=9))],
            settings=apertado,
        )

    assert gateway.objetos == {}


def test_empty_upload_list_is_refused(conexao, opcoes) -> None:
    with pytest.raises(EnvioRejeitado, match="Nenhum arquivo enviado"):
        processar_envio(
            conexao,
            GatewayFalso(),
            submission_id=uuid4(),
            student_id=ALUNO,
            arquivos=[],
            settings=opcoes,
        )


def test_storage_budget_stops_the_upload_before_the_gateway(conexao, opcoes) -> None:
    gateway = GatewayFalso()
    sem_espaco = replace(opcoes, storage_max_total_bytes=10)

    with pytest.raises(StorageBudgetExceeded):
        processar_envio(
            conexao,
            gateway,
            submission_id=uuid4(),
            student_id=ALUNO,
            arquivos=[ArquivoEnviado("print.png", make_png(seed=11))],
            settings=sem_espaco,
        )

    assert gateway.objetos == {}


def test_resending_the_same_file_does_not_duplicate(conexao, opcoes) -> None:
    gateway = GatewayFalso()
    submissao = uuid4()
    dados = make_png(seed=13)
    arquivo = [ArquivoEnviado("print.png", dados)]

    primeiro = processar_envio(
        conexao,
        gateway,
        submission_id=submissao,
        student_id=ALUNO,
        arquivos=arquivo,
        settings=opcoes,
    )
    segundo = processar_envio(
        conexao,
        gateway,
        submission_id=submissao,
        student_id=ALUNO,
        arquivos=arquivo,
        settings=opcoes,
    )

    assert segundo.registrados[0].reaproveitado is True
    assert segundo.registrados[0].id == primeiro.registrados[0].id
    assert len(gateway.objetos) == 1
    assert conexao.execute(text("select count(*) from submission_files")).scalar() == 1


def test_an_image_is_ocred_even_without_an_engine_argument(conexao, opcoes) -> None:
    """Sem a criação preguiçosa, `ocr_engine=None` devolvia a imagem sem texto
    nenhum, em silêncio. Em produção não aparecia porque o chamador sempre
    passava o motor; aparecia em qualquer outro chamador."""

    gateway = GatewayFalso()

    resultado = processar_envio(
        conexao,
        gateway,
        submission_id=uuid4(),
        student_id=ALUNO,
        arquivos=[ArquivoEnviado("print.png", make_png(seed=51))],
        settings=opcoes,
    )

    assert resultado.textos_ocr == [TEXTO_FALSO]


def test_a_scanned_pdf_creates_the_engine_on_demand(conexao, opcoes) -> None:
    """O fallback de PDF digitalizado já existia em `process_document_bytes`,
    mas o pipeline só o alcançava se alguém tivesse passado um motor."""

    gateway = GatewayFalso()

    resultado = processar_envio(
        conexao,
        gateway,
        submission_id=uuid4(),
        student_id=ALUNO,
        arquivos=[ArquivoEnviado("digitalizado.pdf", _pdf_em_branco())],
        settings=opcoes,
    )

    assert resultado.textos_ocr == [TEXTO_FALSO]


def test_no_engine_is_created_when_there_is_nothing_to_ocr(
    conexao, opcoes, monkeypatch
) -> None:
    """Criação preguiçosa é preguiçosa mesmo: um TXT tem texto próprio e não
    pode arrastar o modelo de OCR para a memória."""

    monkeypatch.setattr(
        "english_leaderboard.ocr.create_ocr_engine",
        lambda *a, **k: pytest.fail("OCR não deveria ser carregado para TXT"),
    )

    resultado = processar_envio(
        conexao,
        GatewayFalso(),
        submission_id=uuid4(),
        student_id=ALUNO,
        arquivos=[ArquivoEnviado("resumo.txt", "texto já extraível".encode())],
        settings=opcoes,
    )

    assert resultado.textos_ocr == ["texto já extraível"]


def test_a_poor_first_pass_falls_back_to_the_other_variants(conexao, opcoes) -> None:
    """Print escuro ou borrado é o caso comum, e é onde a passada única falha.

    Confiança baixa ou texto curto na original mandam tentar `contrast` e
    `threshold`; fica o melhor dos três.
    """

    gateway = GatewayFalso()
    motor = OCRRuimNaOriginal()

    resultado = processar_envio(
        conexao,
        gateway,
        submission_id=uuid4(),
        student_id=ALUNO,
        arquivos=[ArquivoEnviado("print.png", make_png(seed=53))],
        settings=opcoes,
        ocr_engine=motor,
    )

    assert motor.chamadas == ["original", "contrast", "threshold"]
    assert resultado.textos_ocr == [TEXTO_FALSO]


def test_a_good_first_pass_skips_the_variants(conexao, opcoes) -> None:
    """As variantes custam duas passadas a mais. Só valem quando a primeira
    não serviu."""

    gateway = GatewayFalso()
    motor = OCRFalso()

    processar_envio(
        conexao,
        gateway,
        submission_id=uuid4(),
        student_id=ALUNO,
        arquivos=[ArquivoEnviado("print.png", make_png(seed=59))],
        settings=opcoes,
        ocr_engine=motor,
    )

    assert motor.chamadas == ["original"]


def test_an_ocr_failure_does_not_invalidate_the_evidence(conexao, opcoes) -> None:
    """Motor de OCR que quebra é problema do motor, não prova inválida.

    O pipeline deixava a `OCRExecutionError` subir e derrubar o envio inteiro
    — inclusive os arquivos que o motor tinha lido sem problema.
    """

    class OCRQueQuebraNaSegunda:
        def __init__(self) -> None:
            self.imagens = 0

        def __call__(self, source):
            if _nome_da_variante(source) == "original":
                self.imagens += 1
            if self.imagens == 2:
                raise RuntimeError("motor caiu no meio")
            return ([[None, TEXTO_FALSO, 0.99]], [0.01])

    gateway = GatewayFalso()

    resultado = processar_envio(
        conexao,
        gateway,
        submission_id=uuid4(),
        student_id=ALUNO,
        arquivos=[
            ArquivoEnviado("primeira.png", make_png(seed=61)),
            ArquivoEnviado("segunda.png", make_png(seed=67)),
        ],
        settings=opcoes,
        ocr_engine=OCRQueQuebraNaSegunda(),
    )

    # As duas imagens foram registradas; só o texto da segunda ficou vazio.
    assert len(resultado.registrados) == 2
    assert resultado.textos_ocr == [TEXTO_FALSO]
    assert len(gateway.objetos) == 2


def _matches(conexao) -> list[tuple]:
    return list(
        conexao.execute(
            text(
                "select kind, distance, same_student from duplicate_matches"
                " order by kind"
            )
        ).all()
    )


def test_an_image_resent_by_another_student_is_an_exact_duplicate(
    conexao, opcoes
) -> None:
    """O caso que o antifraude existe para pegar: o print de um aluno reenviado
    por outro. Sem isso o pipeline pontuava os dois."""

    gateway = GatewayFalso()
    dados = make_png(seed=31)

    processar_envio(
        conexao,
        gateway,
        submission_id=uuid4(),
        student_id=ALUNO,
        arquivos=[ArquivoEnviado("print.png", dados)],
        settings=opcoes,
    )
    segundo = processar_envio(
        conexao,
        gateway,
        submission_id=uuid4(),
        student_id=OUTRO_ALUNO,
        arquivos=[ArquivoEnviado("print.png", dados)],
        settings=opcoes,
    )

    assert segundo.duplicatas_exatas == [True]
    assert segundo.duplicatas_similares == [False]
    assert _matches(conexao) == [("exact", 0, False)]


def test_a_recompressed_image_is_a_similar_duplicate(conexao, opcoes) -> None:
    """Mesmos pixels, bytes diferentes: o checksum não pega, o pHash pega.

    É por isso que imagem não pode depender só de checksum como documento faz.
    """

    gateway = GatewayFalso()

    processar_envio(
        conexao,
        gateway,
        submission_id=uuid4(),
        student_id=ALUNO,
        arquivos=[ArquivoEnviado("print.png", make_png(seed=37))],
        settings=opcoes,
    )
    segundo = processar_envio(
        conexao,
        gateway,
        submission_id=uuid4(),
        student_id=ALUNO,
        arquivos=[
            ArquivoEnviado("print.png", make_png(seed=37, metadata="reenviado"))
        ],
        settings=opcoes,
    )

    assert segundo.duplicatas_exatas == [False]
    assert segundo.duplicatas_similares == [True]
    assert _matches(conexao) == [("similar", 0, True)]


def test_two_similar_images_in_one_batch_flag_the_second(conexao, opcoes) -> None:
    """A comparação também é contra o próprio lote, não só contra o histórico."""

    gateway = GatewayFalso()

    resultado = processar_envio(
        conexao,
        gateway,
        submission_id=uuid4(),
        student_id=ALUNO,
        arquivos=[
            ArquivoEnviado("a.png", make_png(seed=41)),
            ArquivoEnviado("b.png", make_png(seed=41, metadata="copia")),
        ],
        settings=opcoes,
    )

    # O primeiro não tem contra o que casar; o segundo casa com ele.
    assert resultado.duplicatas_similares == [False, True]
    assert _matches(conexao) == [("similar", 0, True)]


def test_a_first_image_has_nothing_to_match(conexao, opcoes) -> None:
    gateway = GatewayFalso()

    resultado = processar_envio(
        conexao,
        gateway,
        submission_id=uuid4(),
        student_id=ALUNO,
        arquivos=[ArquivoEnviado("print.png", make_png(seed=43))],
        settings=opcoes,
    )

    assert resultado.duplicatas_exatas == [False]
    assert resultado.duplicatas_similares == [False]
    assert _matches(conexao) == []


def test_document_gets_no_perceptual_flags(conexao, opcoes) -> None:
    """As listas são paralelas às imagens do lote. Documento não entra nelas."""

    gateway = GatewayFalso()

    resultado = processar_envio(
        conexao,
        gateway,
        submission_id=uuid4(),
        student_id=ALUNO,
        arquivos=[
            ArquivoEnviado("resumo.txt", b"texto qualquer"),
            ArquivoEnviado("print.png", make_png(seed=47)),
        ],
        settings=opcoes,
    )

    assert resultado.duplicatas_exatas == [False]
    assert resultado.duplicatas_similares == [False]


def test_resending_the_same_document_is_flagged_as_duplicate(conexao, opcoes) -> None:
    """Sem esta marca, reenviar o mesmo PDF pontua de novo.

    O pipeline só sinaliza. Transformar a marca em `RuleCheck` e em rejeição é
    da camada acima, junto com o resto da decisão.
    """

    gateway = GatewayFalso()
    dados = b"resumo da unidade"

    primeiro = processar_envio(
        conexao,
        gateway,
        submission_id=uuid4(),
        student_id=ALUNO,
        arquivos=[ArquivoEnviado("resumo.txt", dados)],
        settings=opcoes,
    )
    assert primeiro.documento_duplicado is False

    # Outra submissão: a idempotência de `salvar_arquivo` não entra no caminho.
    segundo = processar_envio(
        conexao,
        gateway,
        submission_id=uuid4(),
        student_id=ALUNO,
        arquivos=[ArquivoEnviado("outro-nome.txt", dados)],
        settings=opcoes,
    )
    assert segundo.documento_duplicado is True


def test_the_same_document_twice_in_one_batch_is_flagged(conexao, opcoes) -> None:
    """A colisão dentro do próprio lote não passa pelo banco: nada foi gravado
    ainda quando o segundo arquivo é analisado."""

    gateway = GatewayFalso()
    dados = b"mesma coisa duas vezes"

    resultado = processar_envio(
        conexao,
        gateway,
        submission_id=uuid4(),
        student_id=ALUNO,
        arquivos=[
            ArquivoEnviado("a.txt", dados),
            ArquivoEnviado("copia.txt", dados),
        ],
        settings=opcoes,
    )

    assert resultado.documento_duplicado is True


def test_an_image_does_not_trip_the_document_duplicate_flag(conexao, opcoes) -> None:
    """Imagem é confrontada por pHash, em outro caminho. Reenviá-la não pode
    acender a marca que existe para documento."""

    gateway = GatewayFalso()
    dados = make_png(seed=23)

    processar_envio(
        conexao,
        gateway,
        submission_id=uuid4(),
        student_id=ALUNO,
        arquivos=[ArquivoEnviado("print.png", dados)],
        settings=opcoes,
    )
    segundo = processar_envio(
        conexao,
        gateway,
        submission_id=uuid4(),
        student_id=ALUNO,
        arquivos=[ArquivoEnviado("print.png", dados)],
        settings=opcoes,
    )

    assert segundo.documento_duplicado is False


def test_image_ocr_text_does_not_land_on_the_file_row(conexao, opcoes) -> None:
    """O texto lido de uma imagem pertence a `submission.ocr_text`.

    O pipeline gravava os dois — imagem e documento — em
    `submission_files.ocr_text`. Só o do documento é lido de lá; o da imagem
    chega às telas e às regras pela decisão, que a camada acima escreve na
    submissão. Gravar nos dois lugares deixava a coluna com um valor que
    ninguém consulta e que diverge do que a submissão diz.

    O pipeline continua devolvendo os dois textos a quem chamou: quem grava
    onde é decisão de quem está por cima.
    """

    gateway = GatewayFalso()

    resultado = processar_envio(
        conexao,
        gateway,
        submission_id=uuid4(),
        student_id=ALUNO,
        arquivos=[
            ArquivoEnviado("print.png", make_png(seed=83)),
            ArquivoEnviado("resumo.txt", "resumo em português".encode()),
        ],
        settings=opcoes,
    )

    linhas = conexao.execute(
        text("select ocr_text from submission_files order by position")
    ).all()

    assert linhas[0] == (None,)
    assert linhas[1] == ("resumo em português",)
    assert resultado.textos_ocr == [TEXTO_FALSO, "resumo em português"]


def test_dimensions_and_page_count_reach_the_metadata(conexao, opcoes) -> None:
    """`width` e `height` alimentam a comparação antifraude de evidências, e
    `page_count` aparece na tela de revisão. O pipeline não gravava nenhum
    dos três."""

    gateway = GatewayFalso()

    processar_envio(
        conexao,
        gateway,
        submission_id=uuid4(),
        student_id=ALUNO,
        arquivos=[
            ArquivoEnviado("print.png", make_png(seed=79)),
            ArquivoEnviado("anexo.pdf", _pdf_em_branco()),
        ],
        settings=opcoes,
    )

    linhas = conexao.execute(
        text(
            "select width, height, page_count from submission_files"
            " order by position"
        )
    ).all()

    # `make_png` gera 480x800; imagem não tem página, PDF não tem dimensão.
    assert linhas[0] == (480, 800, None)
    assert linhas[1] == (None, None, 1)


def test_the_client_filename_is_sanitized_before_becoming_metadata(
    conexao, opcoes
) -> None:
    """O nome cru do cliente ia para `submission_files.filename` sem tratamento.

    Ele não entra na chave do Storage — isso a construção da chave já garante
    —, mas é gravado e exibido na tela de revisão. Diretório, byte nulo e nome
    quilométrico não têm por que chegar até lá.
    """

    gateway = GatewayFalso()

    processar_envio(
        conexao,
        gateway,
        submission_id=uuid4(),
        student_id=ALUNO,
        arquivos=[
            ArquivoEnviado("../../etc/pas\x00swd.png", make_png(seed=71)),
            ArquivoEnviado("a" * 300 + ".png", make_png(seed=73)),
        ],
        settings=opcoes,
    )

    nomes = [
        linha[0]
        for linha in conexao.execute(
            text("select filename from submission_files order by position")
        ).all()
    ]
    assert nomes[0] == "passwd.png"
    assert len(nomes[1]) == 255


def test_key_is_always_derived_from_the_session_owner(conexao, opcoes) -> None:
    """Nem o nome do arquivo nem qualquer campo da interface entra na chave."""

    gateway = GatewayFalso()

    resultado = processar_envio(
        conexao,
        gateway,
        submission_id=uuid4(),
        student_id=ALUNO,
        arquivos=[ArquivoEnviado("../../etc/passwd.png", make_png(seed=17))],
        settings=opcoes,
    )

    chave = resultado.registrados[0].storage_key
    assert chave.startswith(f"students/{ALUNO}/{CATEGORIA_IMAGEM}/")
    assert ".." not in chave and "passwd" not in chave
    # O nome sobrevive apenas nos metadados, e sanitizado: o teste travava aqui
    # o caminho cru `../../etc/passwd.png`, de quando o pipeline gravava o que
    # o cliente mandasse. São duas barreiras sobre coisas diferentes — a chave
    # física não usa o nome, e o metadado não guarda diretório.
    guardado = conexao.execute(text("select filename from submission_files")).scalar()
    assert guardado == "passwd.png"
