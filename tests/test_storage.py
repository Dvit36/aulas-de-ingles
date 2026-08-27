"""Adaptador e serviço de arquivos, sem tocar a rede.

O duplo do gateway permite provar os caminhos que seriam caros ou impossíveis
de reproduzir contra o serviço real: falha no upload, falha no banco depois do
upload, falha na compensação.
"""

from __future__ import annotations

import hashlib
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from english_leaderboard.schema import SchemaBase
from english_leaderboard.storage import (
    CATEGORIAS,
    StorageError,
    StorageKeyError,
    build_key,
    ensure_owned_key,
    parse_key,
)
from english_leaderboard.storage_budget import LimitesStorage, StorageBudgetExceeded
from english_leaderboard.storage_service import (
    ArquivoNaoAutorizado,
    baixar_arquivo,
    orfaos_pendentes,
    reconciliar_orfaos,
    remover_arquivo,
    resolver_arquivo_autorizado,
    salvar_arquivo,
    url_temporaria,
)

ALUNO_A = "11111111-1111-1111-1111-111111111111"
ALUNO_B = "22222222-2222-2222-2222-222222222222"
LIMITES = LimitesStorage(max_total_bytes=1_000_000, max_monthly_egress_bytes=100_000)
BUCKET = "student-files"


class GatewayFalso:
    """Bucket em memória, com falhas programáveis por operação."""

    def __init__(self) -> None:
        self.objetos: dict[str, bytes] = {}
        self.falhar_em: set[str] = set()
        self.chamadas: list[tuple[str, str]] = []

    def _talvez_falhar(self, operacao: str, key: str) -> None:
        self.chamadas.append((operacao, key))
        if operacao in self.falhar_em:
            raise StorageError(f"falha simulada em {operacao}")

    def upload(self, key: str, data: bytes, content_type: str) -> None:
        self._talvez_falhar("upload", key)
        if key in self.objetos:
            raise StorageError("objeto já existe")
        self.objetos[key] = data

    def download(self, key: str) -> bytes:
        self._talvez_falhar("download", key)
        if key not in self.objetos:
            raise StorageError("objeto não encontrado")
        return self.objetos[key]

    def remove(self, key: str) -> None:
        self._talvez_falhar("remove", key)
        self.objetos.pop(key, None)

    def exists(self, key: str) -> bool:
        self._talvez_falhar("exists", key)
        return key in self.objetos

    def signed_url(self, key: str, expires_in: int) -> str:
        self._talvez_falhar("signed_url", key)
        return f"https://exemplo.invalid/{key}?expira={expires_in}"


@pytest.fixture
def conexao():
    """Tabelas vindas do próprio schema, não de um DDL escrito à mão.

    A versão anterior repetia o CREATE TABLE aqui e envelheceu: uma coluna
    nova no modelo passava despercebida até a inserção falhar. Gerar a partir
    de ``SchemaBase`` torna a divergência impossível.
    """

    engine = create_engine("sqlite+pysqlite:///:memory:")
    SchemaBase.metadata.create_all(engine)
    with engine.connect() as c:
        yield c
    engine.dispose()


def _salvar(conexao, gateway, *, dono=ALUNO_A, dados=b"conteudo", submissao=None):
    return salvar_arquivo(
        conexao,
        gateway,
        submission_id=submissao or uuid4(),
        student_id=dono,
        filename="print.jpg",
        dados=dados,
        content_type="image/jpeg",
        categoria="uploads",
        extensao="jpg",
        bucket=BUCKET,
        limites=LIMITES,
    )


# --------------------------------------------------------------- chaves

def test_key_is_built_from_the_owner_uuid_only() -> None:
    chave = build_key(ALUNO_A, "activities", extensao="JPG")

    dono, categoria, arquivo = parse_key(chave)
    assert dono == ALUNO_A
    assert categoria == "activities"
    assert arquivo.endswith(".jpg")  # extensão normalizada
    # Nome, usuário e e-mail não entram no caminho físico.
    assert "print" not in chave and "@" not in chave


def test_every_documented_category_is_accepted() -> None:
    for categoria in CATEGORIAS:
        chave = build_key(ALUNO_A, categoria, extensao="pdf")
        assert f"/{categoria}/" in chave


def test_malformed_keys_are_refused() -> None:
    with pytest.raises(StorageKeyError, match="UUID"):
        build_key("nao-e-uuid", "uploads", extensao="jpg")
    with pytest.raises(StorageKeyError, match="Categoria"):
        build_key(ALUNO_A, "secreto", extensao="jpg")
    for ruim in ("", ".", "jp/g", "muitolongaextensao"):
        with pytest.raises(StorageKeyError, match="Extensão"):
            build_key(ALUNO_A, "uploads", extensao=ruim)

    for chave in (
        f"outro/{ALUNO_A}/uploads/a.jpg",
        "students/nao-e-uuid/uploads/a.jpg",
        f"students/{ALUNO_A}/secreto/a.jpg",
        f"students/{ALUNO_A}/uploads",
        "../etc/passwd",
    ):
        with pytest.raises(StorageKeyError):
            parse_key(chave)


def test_a_key_from_another_student_is_refused() -> None:
    chave_de_b = build_key(ALUNO_B, "uploads", extensao="jpg")

    ensure_owned_key(chave_de_b, ALUNO_B)
    with pytest.raises(StorageKeyError, match="não pertence"):
        ensure_owned_key(chave_de_b, ALUNO_A)


# --------------------------------------------------------------- upload

def test_successful_upload_stores_object_and_metadata(conexao) -> None:
    gateway = GatewayFalso()

    registro = _salvar(conexao, gateway, dados=b"imagem")

    assert registro.reaproveitado is False
    assert gateway.objetos[registro.storage_key] == b"imagem"
    assert registro.checksum_sha256 == hashlib.sha256(b"imagem").hexdigest()
    linha = conexao.execute(text("select count(*) from submission_files")).scalar()
    assert linha == 1


def test_upload_failure_leaves_no_metadata_behind(conexao) -> None:
    gateway = GatewayFalso()
    gateway.falhar_em.add("upload")

    with pytest.raises(StorageError):
        _salvar(conexao, gateway)

    assert conexao.execute(text("select count(*) from submission_files")).scalar() == 0
    assert gateway.objetos == {}


def test_database_failure_after_upload_deletes_the_object(conexao) -> None:
    """O caso que deixa órfão se ninguém compensar."""

    gateway = GatewayFalso()
    submissao = uuid4()
    _salvar(conexao, gateway, submissao=submissao, dados=b"primeiro")
    # Força colisão de chave única no banco reusando o mesmo storage_key.
    conexao.execute(
        text("create unique index if not exists ix_um_por_submissao"
             " on submission_files (submission_id, position)")
    )

    gateway_falho = GatewayFalso()
    gateway_falho.objetos = gateway.objetos
    with pytest.raises(IntegrityError):
        _salvar(conexao, gateway_falho, submissao=submissao, dados=b"segundo")

    # O objeto do segundo envio foi removido pela compensação.
    assert ("remove", gateway_falho.chamadas[-1][1]) == gateway_falho.chamadas[-1]
    assert len(gateway.objetos) == 1


def test_failed_compensation_is_recorded_as_an_orphan(conexao) -> None:
    """Quando nem a remoção funciona, o objeto não pode sumir do radar."""

    gateway = GatewayFalso()
    submissao = uuid4()
    _salvar(conexao, gateway, submissao=submissao, dados=b"primeiro")
    conexao.execute(
        text("create unique index if not exists ix_um_por_submissao"
             " on submission_files (submission_id, position)")
    )
    gateway.falhar_em.add("remove")

    with pytest.raises(IntegrityError):
        _salvar(conexao, gateway, submissao=submissao, dados=b"segundo")

    pendentes = orfaos_pendentes(conexao)
    assert len(pendentes) == 1
    assert "remoção compensatória" in pendentes[0]["reason"]


def test_reupload_of_identical_content_is_idempotent(conexao) -> None:
    gateway = GatewayFalso()
    submissao = uuid4()

    primeiro = _salvar(conexao, gateway, submissao=submissao, dados=b"igual")
    segundo = _salvar(conexao, gateway, submissao=submissao, dados=b"igual")

    assert segundo.reaproveitado is True
    assert segundo.id == primeiro.id
    assert len(gateway.objetos) == 1
    assert len(gateway.chamadas) == 1  # nenhum upload novo


def test_upload_over_the_storage_budget_never_reaches_the_gateway(conexao) -> None:
    gateway = GatewayFalso()
    apertado = LimitesStorage(max_total_bytes=10, max_monthly_egress_bytes=100_000)

    with pytest.raises(StorageBudgetExceeded):
        salvar_arquivo(
            conexao,
            gateway,
            submission_id=uuid4(),
            student_id=ALUNO_A,
            filename="grande.jpg",
            dados=b"x" * 100,
            content_type="image/jpeg",
            categoria="uploads",
            extensao="jpg",
            bucket=BUCKET,
            limites=apertado,
        )

    assert gateway.chamadas == []  # recusado antes de qualquer chamada


def test_empty_file_is_refused(conexao) -> None:
    with pytest.raises(ValueError, match="vazio"):
        _salvar(conexao, GatewayFalso(), dados=b"")


# ------------------------------------------------------- leitura e acesso

def test_student_cannot_reach_another_students_file(conexao) -> None:
    gateway = GatewayFalso()
    registro = _salvar(conexao, gateway, dono=ALUNO_A)

    resolver_arquivo_autorizado(
        conexao, file_id=registro.id, student_id=ALUNO_A
    )
    with pytest.raises(ArquivoNaoAutorizado):
        resolver_arquivo_autorizado(
            conexao, file_id=registro.id, student_id=ALUNO_B
        )
    # O administrador alcança para revisar.
    resolver_arquivo_autorizado(
        conexao, file_id=registro.id, student_id=ALUNO_B, is_admin=True
    )


def test_unknown_file_id_is_refused(conexao) -> None:
    with pytest.raises(ArquivoNaoAutorizado, match="não encontrado"):
        resolver_arquivo_autorizado(
            conexao, file_id=uuid4(), student_id=ALUNO_A
        )


def test_download_counts_egress_and_respects_the_budget(conexao) -> None:
    gateway = GatewayFalso()
    dados = b"y" * 40_000
    registro = _salvar(conexao, gateway, dados=dados)

    conteudo, objeto = baixar_arquivo(
        conexao,
        gateway,
        file_id=registro.id,
        student_id=ALUNO_A,
        limites=LIMITES,
    )
    assert conteudo == dados
    assert objeto.bucket == BUCKET

    baixar_arquivo(
        conexao, gateway, file_id=registro.id, student_id=ALUNO_A, limites=LIMITES
    )
    # 3o download passaria de 100.000 de franquia.
    with pytest.raises(StorageBudgetExceeded, match="Download recusado"):
        baixar_arquivo(
            conexao,
            gateway,
            file_id=registro.id,
            student_id=ALUNO_A,
            limites=LIMITES,
        )


def test_signed_url_is_only_issued_after_authorization(conexao) -> None:
    gateway = GatewayFalso()
    registro = _salvar(conexao, gateway, dono=ALUNO_A)

    url = url_temporaria(
        conexao,
        gateway,
        file_id=registro.id,
        student_id=ALUNO_A,
        limites=LIMITES,
    )
    assert registro.storage_key in url
    assert "expira=90" in url  # janela curta por padrão

    with pytest.raises(ArquivoNaoAutorizado):
        url_temporaria(
            conexao,
            gateway,
            file_id=registro.id,
            student_id=ALUNO_B,
            limites=LIMITES,
        )
    # Nenhuma assinatura foi emitida para o aluno errado.
    assert sum(1 for op, _ in gateway.chamadas if op == "signed_url") == 1


def test_signed_url_expiry_window_is_bounded(conexao) -> None:
    gateway = GatewayFalso()
    registro = _salvar(conexao, gateway)

    for invalido in (0, -1, 3601):
        with pytest.raises(ValueError, match="Expiração"):
            url_temporaria(
                conexao,
                gateway,
                file_id=registro.id,
                student_id=ALUNO_A,
                limites=LIMITES,
                expira_em=invalido,
            )


# ----------------------------------------------------- remoção e órfãos

def test_removal_deletes_metadata_and_object(conexao) -> None:
    gateway = GatewayFalso()
    registro = _salvar(conexao, gateway)

    remover_arquivo(
        conexao, gateway, file_id=registro.id, student_id=ALUNO_A
    )

    assert conexao.execute(text("select count(*) from submission_files")).scalar() == 0
    assert gateway.objetos == {}


def test_removal_that_fails_in_the_bucket_becomes_an_orphan(conexao) -> None:
    gateway = GatewayFalso()
    registro = _salvar(conexao, gateway)
    gateway.falhar_em.add("remove")

    remover_arquivo(conexao, gateway, file_id=registro.id, student_id=ALUNO_A)

    # Metadado saiu, objeto ficou: precisa estar registrado para reconciliar.
    assert conexao.execute(text("select count(*) from submission_files")).scalar() == 0
    pendentes = orfaos_pendentes(conexao)
    assert len(pendentes) == 1
    assert pendentes[0]["key"] == registro.storage_key


def test_orphans_are_reconciled_when_the_bucket_recovers(conexao) -> None:
    gateway = GatewayFalso()
    registro = _salvar(conexao, gateway)
    gateway.falhar_em.add("remove")
    remover_arquivo(conexao, gateway, file_id=registro.id, student_id=ALUNO_A)

    # Enquanto o bucket recusa, o órfão persiste.
    assert reconciliar_orfaos(conexao, gateway) == {
        "resolvidos": 0,
        "persistentes": 1,
    }

    gateway.falhar_em.clear()
    assert reconciliar_orfaos(conexao, gateway) == {
        "resolvidos": 1,
        "persistentes": 0,
    }
    assert orfaos_pendentes(conexao) == []
    assert gateway.objetos == {}


def test_student_cannot_remove_another_students_file(conexao) -> None:
    gateway = GatewayFalso()
    registro = _salvar(conexao, gateway, dono=ALUNO_A)

    with pytest.raises(ArquivoNaoAutorizado):
        remover_arquivo(
            conexao, gateway, file_id=registro.id, student_id=ALUNO_B
        )

    assert gateway.objetos  # nada foi apagado
    assert conexao.execute(text("select count(*) from submission_files")).scalar() == 1
