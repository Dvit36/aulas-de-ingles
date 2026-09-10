"""Upload e entrega de arquivos, com o banco como fonte de verdade.

O ponto delicado é que binário e metadado vivem em sistemas diferentes e não
compartilham transação. A ordem escolhida é: valida orçamento, envia ao
Storage, grava os metadados. Se a gravação falhar, o objeto recém-enviado é
removido; se a remoção também falhar, fica registrado em ``storage_orphans``
para reconciliação. O que não pode acontecer é órfão em silêncio.

A leitura faz o caminho inverso: resolve o registro sob autorização, confirma
a propriedade e só então toca no Storage. Chave vinda da interface nunca é
usada.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .storage import (
    URL_EXPIRA_SEGUNDOS,
    ObjetoArmazenado,
    StorageError,
    StorageGateway,
    StorageKeyError,
    build_key,
    ensure_owned_key,
)
from .storage_budget import (
    LimitesStorage,
    garantir_egress,
    garantir_espaco,
    registrar_download_de_arquivo,
    registrar_upload,
)

PROVEDOR = "supabase"


class ArquivoNaoAutorizado(PermissionError):
    """O registro não existe ou não pertence ao usuário da sessão."""


@dataclass(frozen=True, slots=True)
class ArquivoRegistrado:
    id: str
    storage_key: str
    checksum_sha256: str
    file_size: int
    reaproveitado: bool


def _checksum(dados: bytes) -> str:
    return hashlib.sha256(dados).hexdigest()


def _registrar_orfao(
    conexao: Connection, *, bucket: str, key: str, student_id: str, motivo: str
) -> None:
    """Deixa rastro do objeto que ficou sem dono no banco."""

    # O id é gerado aqui, e não pelo default da tabela: assim a reconciliação
    # encontra a linha pelo mesmo identificador em qualquer banco.
    conexao.execute(
        text(
            "insert into storage_orphans"
            " (id, storage_provider, storage_bucket, storage_key, student_id, reason)"
            " values (:id, :p, :b, :k, :s, :r)"
        ),
        {
            "id": str(uuid4()),
            "p": PROVEDOR,
            "b": bucket,
            "k": key,
            "s": student_id,
            "r": motivo[:500],
        },
    )


def salvar_arquivo(
    conexao: Connection,
    gateway: StorageGateway,
    *,
    submission_id: str | UUID,
    student_id: str | UUID,
    filename: str,
    dados: bytes,
    content_type: str,
    categoria: str,
    extensao: str,
    bucket: str,
    limites: LimitesStorage,
    ocr_text: str | None = None,
    phash: str | None = None,
    position: int = 0,
    width: int | None = None,
    height: int | None = None,
    page_count: int | None = None,
) -> ArquivoRegistrado:
    """Envia o binário e grava os metadados, compensando se algo falhar."""

    if not dados:
        raise ValueError("Arquivo vazio")
    dono = str(student_id).lower()
    checksum = _checksum(dados)

    # Idempotência: o mesmo conteúdo do mesmo aluno na mesma submissão não
    # gera objeto novo. Evita duplicar em reenvio ou clique repetido.
    existente = conexao.execute(
        text(
            "select id, storage_key, file_size from submission_files"
            " where submission_id = :sub and checksum_sha256 = :ck"
        ),
        {"sub": str(submission_id), "ck": checksum},
    ).first()
    if existente is not None:
        return ArquivoRegistrado(
            id=str(existente[0]),
            storage_key=str(existente[1]),
            checksum_sha256=checksum,
            file_size=int(existente[2]),
            reaproveitado=True,
        )

    garantir_espaco(conexao, bytes_novos=len(dados), limites=limites)

    file_id = uuid4()
    key = build_key(dono, categoria, extensao=extensao, file_id=file_id)

    gateway.upload(key, dados, content_type)

    try:
        conexao.execute(
            text(
                "insert into submission_files"
                " (id, submission_id, student_id, filename, storage_provider,"
                "  storage_bucket, storage_key, content_type, file_size,"
                "  checksum_sha256, phash, ocr_text, position,"
                "  width, height, page_count)"
                " values (:id, :sub, :dono, :nome, :prov, :bucket, :key, :ct,"
                "         :tam, :ck, :ph, :ocr, :pos, :w, :h, :pg)"
            ),
            {
                "id": str(file_id),
                "sub": str(submission_id),
                "dono": dono,
                "nome": filename[:255],
                "prov": PROVEDOR,
                "bucket": bucket,
                "key": key,
                "ct": content_type,
                "tam": len(dados),
                "ck": checksum,
                "ph": phash,
                "ocr": ocr_text,
                "pos": position,
                "w": width,
                "h": height,
                "pg": page_count,
            },
        )
        registrar_upload(conexao)
    except Exception as erro:
        # O objeto já está no bucket, mas ninguém o referencia: precisa sair.
        try:
            gateway.remove(key)
        except Exception as falha_remocao:
            _registrar_orfao(
                conexao,
                bucket=bucket,
                key=key,
                student_id=dono,
                motivo=(
                    f"registro falhou ({type(erro).__name__}) e a remoção "
                    f"compensatória também ({type(falha_remocao).__name__})"
                ),
            )
        raise
    return ArquivoRegistrado(
        id=str(file_id),
        storage_key=key,
        checksum_sha256=checksum,
        file_size=len(dados),
        reaproveitado=False,
    )


def resolver_arquivo_autorizado(
    conexao: Connection,
    *,
    file_id: str | UUID,
    student_id: str | UUID,
    is_admin: bool = False,
) -> ObjetoArmazenado:
    """Busca a chave no banco depois de confirmar quem pode acessá-la.

    Nunca aceita chave vinda da interface: o identificador é resolvido em um
    registro, a propriedade é conferida, e só então a chave é usada.
    """

    linha = conexao.execute(
        text(
            "select storage_provider, storage_bucket, storage_key, file_size,"
            "       content_type, student_id"
            " from submission_files where id = :id"
        ),
        {"id": str(file_id)},
    ).first()
    if linha is None:
        raise ArquivoNaoAutorizado("Arquivo não encontrado")
    provider, bucket, key, tamanho, content_type, dono = linha
    if not is_admin:
        try:
            ensure_owned_key(str(key), str(student_id))
        except StorageKeyError as erro:
            raise ArquivoNaoAutorizado(str(erro)) from erro
        if str(dono).lower() != str(student_id).lower():
            raise ArquivoNaoAutorizado("Arquivo pertence a outro aluno")
    return ObjetoArmazenado(
        provider=str(provider),
        bucket=str(bucket),
        key=str(key),
        size_bytes=int(tamanho),
        content_type=str(content_type),
    )


def baixar_arquivo(
    conexao: Connection,
    gateway: StorageGateway,
    *,
    file_id: str | UUID,
    student_id: str | UUID,
    limites: LimitesStorage,
    is_admin: bool = False,
) -> tuple[bytes, ObjetoArmazenado]:
    """Entrega o conteúdo no servidor, contando o egress consumido."""

    objeto = resolver_arquivo_autorizado(
        conexao, file_id=file_id, student_id=student_id, is_admin=is_admin
    )
    garantir_egress(conexao, bytes_saida=objeto.size_bytes, limites=limites)
    dados = gateway.download(objeto.key)
    registrar_download_de_arquivo(conexao, file_id=file_id, bytes_saida=len(dados))
    return dados, objeto


def url_temporaria(
    conexao: Connection,
    gateway: StorageGateway,
    *,
    file_id: str | UUID,
    student_id: str | UUID,
    limites: LimitesStorage,
    is_admin: bool = False,
    expira_em: int = URL_EXPIRA_SEGUNDOS,
) -> str:
    """Assina uma URL curta, somente depois de autorizar o registro.

    A URL não é gravada em lugar nenhum: o banco guarda apenas a chave.
    """

    if expira_em <= 0 or expira_em > 3600:
        raise ValueError("Expiração deve ficar entre 1 e 3600 segundos")
    objeto = resolver_arquivo_autorizado(
        conexao, file_id=file_id, student_id=student_id, is_admin=is_admin
    )
    # Assinar não transfere bytes, mas quem recebe a URL vai baixar.
    garantir_egress(conexao, bytes_saida=objeto.size_bytes, limites=limites)
    url = gateway.signed_url(objeto.key, expira_em)
    registrar_download_de_arquivo(
        conexao, file_id=file_id, bytes_saida=objeto.size_bytes
    )
    return url


def remover_arquivo(
    conexao: Connection,
    gateway: StorageGateway,
    *,
    file_id: str | UUID,
    student_id: str | UUID,
    is_admin: bool = False,
) -> None:
    """Apaga metadados e objeto, nessa ordem.

    Se o objeto sobreviver à remoção, ele vira órfão registrado em vez de
    lixo invisível ocupando cota.
    """

    objeto = resolver_arquivo_autorizado(
        conexao, file_id=file_id, student_id=student_id, is_admin=is_admin
    )
    conexao.execute(
        text("delete from submission_files where id = :id"), {"id": str(file_id)}
    )
    try:
        gateway.remove(objeto.key)
    except StorageError as erro:
        _registrar_orfao(
            conexao,
            bucket=objeto.bucket,
            key=objeto.key,
            student_id=str(student_id),
            motivo=f"remoção falhou após apagar o registro: {erro}",
        )


def orfaos_pendentes(conexao: Connection) -> list[dict[str, object]]:
    """Objetos que ficaram no bucket sem registro correspondente."""

    linhas = conexao.execute(
        text(
            "select id, storage_bucket, storage_key, reason, created_at"
            " from storage_orphans where resolved_at is null"
            " order by created_at"
        )
    ).all()
    return [
        {
            "id": str(linha[0]),
            "bucket": linha[1],
            "key": linha[2],
            "reason": linha[3],
            "created_at": linha[4],
        }
        for linha in linhas
    ]


def reconciliar_orfaos(
    conexao: Connection, gateway: StorageGateway
) -> dict[str, int]:
    """Tenta remover de novo cada órfão pendente e marca os resolvidos."""

    resolvidos = 0
    persistentes = 0
    for orfao in orfaos_pendentes(conexao):
        try:
            gateway.remove(str(orfao["key"]))
        except StorageError:
            persistentes += 1
            continue
        conexao.execute(
            text(
                "update storage_orphans set resolved_at = CURRENT_TIMESTAMP"
                " where id = :id"
            ),
            {"id": orfao["id"]},
        )
        resolvidos += 1
    return {"resolvidos": resolvidos, "persistentes": persistentes}


__all__ = [
    "PROVEDOR",
    "ArquivoNaoAutorizado",
    "ArquivoRegistrado",
    "baixar_arquivo",
    "orfaos_pendentes",
    "reconciliar_orfaos",
    "remover_arquivo",
    "resolver_arquivo_autorizado",
    "salvar_arquivo",
    "url_temporaria",
]
