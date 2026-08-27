"""Adaptador do Supabase Storage para o bucket privado de arquivos de aluno.

Duas regras estruturam este módulo:

1. **A chave nunca vem da interface.** ``build_key`` monta a chave a partir do
   UUID do dono, da categoria e de um id de arquivo. Para ler ou apagar, o
   chamador resolve primeiro o registro autorizado no banco e usa a chave que
   está lá. ``ensure_owned_key`` existe para tornar essa checagem explícita.

2. **Operação de aluno usa o JWT do aluno.** O cliente é construído com o
   token da sessão, para que as políticas de ``storage.objects`` sejam
   avaliadas. A chave secreta do Supabase ignora RLS e criaria objeto sem
   proprietário, então não participa de upload nem download de aluno.

O acesso à rede fica atrás de ``StorageGateway``: os testes injetam um duplo
e não tocam a rede.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol, runtime_checkable
from uuid import UUID, uuid4

CATEGORIAS = ("uploads", "activities", "documents", "processed")

# A extensão entra na chave física, então é restrita ao que a aplicação aceita.
EXTENSAO_VALIDA = re.compile(r"^[a-z0-9]{1,8}$")
UUID_EM_PATH = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)

# Janela curta: a URL assinada é para a entrega imediata de um arquivo, não
# para virar link compartilhável.
URL_EXPIRA_SEGUNDOS = 90
TIMEOUT_SEGUNDOS = 30


class StorageError(RuntimeError):
    """Falha ao falar com o Supabase Storage."""


class StorageKeyError(ValueError):
    """Chave malformada, de outro dono ou com categoria não prevista."""


@dataclass(frozen=True, slots=True)
class ObjetoArmazenado:
    provider: str
    bucket: str
    key: str
    size_bytes: int
    content_type: str


@runtime_checkable
class StorageGateway(Protocol):
    """Superfície mínima do bucket, para permitir um duplo nos testes."""

    def upload(self, key: str, data: bytes, content_type: str) -> None: ...

    def download(self, key: str) -> bytes: ...

    def remove(self, key: str) -> None: ...

    def exists(self, key: str) -> bool: ...

    def signed_url(self, key: str, expires_in: int) -> str: ...


def build_key(
    student_id: str | UUID,
    categoria: str,
    *,
    extensao: str,
    file_id: str | UUID | None = None,
) -> str:
    """Monta ``students/{student_id}/{categoria}/{file_id}.{ext}``.

    Só o UUID entra no caminho. Nome, usuário e e-mail ficam nos metadados:
    evita colisão, exposição de dado pessoal e dependência de nome mutável.
    """

    dono = str(student_id).lower()
    if not UUID_EM_PATH.match(dono):
        raise StorageKeyError(f"student_id não é um UUID: {student_id!r}")
    if categoria not in CATEGORIAS:
        raise StorageKeyError(
            f"Categoria {categoria!r} não é uma de {', '.join(CATEGORIAS)}"
        )
    limpa = (extensao or "").strip().lower().lstrip(".")
    if not EXTENSAO_VALIDA.match(limpa):
        raise StorageKeyError(f"Extensão inválida: {extensao!r}")
    identificador = str(file_id or uuid4()).lower()
    if not UUID_EM_PATH.match(identificador):
        raise StorageKeyError(f"file_id não é um UUID: {file_id!r}")
    return f"students/{dono}/{categoria}/{identificador}.{limpa}"


def parse_key(key: str) -> tuple[str, str, str]:
    """Devolve ``(student_id, categoria, arquivo)`` de uma chave válida."""

    partes = (key or "").split("/")
    if len(partes) != 4 or partes[0] != "students":
        raise StorageKeyError(f"Chave fora do formato esperado: {key!r}")
    _, dono, categoria, arquivo = partes
    if not UUID_EM_PATH.match(dono.lower()):
        raise StorageKeyError(f"Chave sem UUID de dono: {key!r}")
    if categoria not in CATEGORIAS:
        raise StorageKeyError(f"Chave com categoria inesperada: {key!r}")
    if not arquivo or arquivo.startswith("."):
        raise StorageKeyError(f"Chave sem nome de arquivo: {key!r}")
    return dono.lower(), categoria, arquivo


def ensure_owned_key(key: str, student_id: str | UUID) -> str:
    """Confirma que a chave pertence ao dono informado.

    Chamado com a chave que veio do banco e o dono derivado da sessão. Se a
    interface conseguisse injetar uma chave arbitrária, seria aqui que ela
    seria barrada.
    """

    dono, _, _ = parse_key(key)
    if dono != str(student_id).lower():
        raise StorageKeyError("Chave não pertence ao usuário da sessão")
    return key


class SupabaseStorageGateway:
    """Gateway real, com o cliente oficial autenticado pelo JWT do usuário."""

    def __init__(self, cliente, bucket: str) -> None:
        self._bucket_nome = bucket
        self._bucket = cliente.storage.from_(bucket)

    def upload(self, key: str, data: bytes, content_type: str) -> None:
        try:
            self._bucket.upload(
                key,
                data,
                {"content-type": content_type, "upsert": "false"},
            )
        except Exception as erro:  # a lib levanta tipos variados
            raise StorageError(f"Falha ao enviar {key}: {_resumo(erro)}") from erro

    def download(self, key: str) -> bytes:
        try:
            return self._bucket.download(key)
        except Exception as erro:
            raise StorageError(f"Falha ao baixar {key}: {_resumo(erro)}") from erro

    def remove(self, key: str) -> None:
        try:
            self._bucket.remove([key])
        except Exception as erro:
            raise StorageError(f"Falha ao remover {key}: {_resumo(erro)}") from erro

    def exists(self, key: str) -> bool:
        try:
            return bool(self._bucket.exists(key))
        except Exception as erro:
            raise StorageError(f"Falha ao consultar {key}: {_resumo(erro)}") from erro

    def signed_url(self, key: str, expires_in: int) -> str:
        try:
            resposta = self._bucket.create_signed_url(key, expires_in)
        except Exception as erro:
            raise StorageError(f"Falha ao assinar {key}: {_resumo(erro)}") from erro
        url = resposta.get("signedURL") or resposta.get("signedUrl")
        if not url:
            raise StorageError(f"Resposta sem URL assinada para {key}")
        return str(url)


def _resumo(erro: Exception) -> str:
    """Primeira linha do erro, para não vazar token nem corpo inteiro em log."""

    return str(erro).splitlines()[0][:200] if str(erro) else type(erro).__name__


def criar_cliente(url: str, chave_publica: str, access_token: str):
    """Cliente do Supabase autenticado como o usuário da sessão.

    O ``access_token`` do aluno vai no cabeçalho para que as políticas de
    ``storage.objects`` sejam avaliadas com ``auth.uid()`` correto. Sem ele, a
    operação correria como anônima e seria recusada.
    """

    from supabase import ClientOptions, create_client

    if not access_token:
        raise StorageError("Sessão sem token: operação de arquivo não autorizada")
    cliente = create_client(
        url,
        chave_publica,
        options=ClientOptions(
            headers={"Authorization": f"Bearer {access_token}"},
            storage_client_timeout=TIMEOUT_SEGUNDOS,
        ),
    )
    return cliente


__all__ = [
    "CATEGORIAS",
    "TIMEOUT_SEGUNDOS",
    "URL_EXPIRA_SEGUNDOS",
    "ObjetoArmazenado",
    "StorageError",
    "StorageGateway",
    "StorageKeyError",
    "SupabaseStorageGateway",
    "build_key",
    "criar_cliente",
    "ensure_owned_key",
    "parse_key",
]
