"""Fluxo de submissão sem filesystem persistente.

Cobre o que o disco efêmero do Streamlit exige: processamento em memória,
limpeza garantida de qualquer temporário e nenhum resíduo local depois do
envio.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
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
    # O nome original sobrevive apenas nos metadados.
    guardado = conexao.execute(text("select filename from submission_files")).scalar()
    assert guardado == "../../etc/passwd.png"
