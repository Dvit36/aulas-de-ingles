"""Aplica a identidade da sessão na conexão, para a RLS valer.

A aplicação fala com o PostgreSQL por conexão direta — é o que preserva as
transações do ledger, que o PostgREST não conseguiria fazer. O preço é que a
conexão autentica como ``postgres``, um papel que **ignora RLS**.

Este módulo paga esse preço: dentro do bloco, a transação passa a rodar como
``authenticated`` com as claims do usuário, então ``auth.uid()`` responde
corretamente e todas as políticas são avaliadas — exatamente como se a
consulta viesse pelo PostgREST.

Duas decisões importam para a segurança:

* ``set_config(..., true)`` é **transaction-local**. Em pooler de sessão a
  conexão é reaproveitada entre requisições; sem o escopo local, as claims de
  um aluno vazariam para o próximo.
* O bloco restaura o papel ao sair, inclusive quando a transação abortou por
  uma política ter barrado a operação.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import Connection

PAPEL_AUTENTICADO = "authenticated"
PAPEL_PROPRIETARIO = "postgres"


class SessaoInvalida(ValueError):
    """Identidade ausente ou malformada para aplicar na conexão."""


def _claims(user_id: str | UUID) -> str:
    identificador = str(user_id).strip()
    if not identificador:
        raise SessaoInvalida("Sessão sem identificador de usuário")
    try:
        UUID(identificador)
    except ValueError as erro:
        raise SessaoInvalida(f"Identificador não é um UUID: {identificador!r}") from erro
    # Só o que as políticas leem. Nada de token, e-mail ou papel da aplicação:
    # o papel administrativo vem da tabela protegida, via is_admin().
    return json.dumps({"sub": identificador, "role": PAPEL_AUTENTICADO})


def aplicar_identidade(conexao: Connection, user_id: str | UUID) -> None:
    """Passa a transação corrente a rodar como o usuário informado.

    Precisa ser chamada **por transação**: ``set_config(..., true)`` é local à
    transação, então um commit descarta as claims. Quem esquecer disso volta
    silenciosamente ao papel proprietário, que ignora RLS — por isso a
    aplicação amarra esta função ao evento de início de transação em vez de
    chamá-la à mão.
    """

    claims = _claims(user_id)
    conexao.execute(
        text("select set_config('role', :papel, true)"),
        {"papel": PAPEL_AUTENTICADO},
    )
    conexao.execute(
        text("select set_config('request.jwt.claims', :claims, true)"),
        {"claims": claims},
    )


@contextmanager
def como_usuario(conexao: Connection, user_id: str | UUID) -> Iterator[Connection]:
    """Executa o bloco sob a identidade do usuário, com RLS ativa."""

    aplicar_identidade(conexao, user_id)
    try:
        yield conexao
    finally:
        _restaurar(conexao)


@contextmanager
def como_servico(conexao: Connection) -> Iterator[Connection]:
    """Executa o bloco sem RLS, para manutenção explícita.

    Reservado a migração, reconciliação e seed. Nunca deve envolver uma
    operação disparada por aluno: seria abrir mão de toda a proteção.
    """

    _restaurar(conexao)
    try:
        yield conexao
    finally:
        _restaurar(conexao)


def _restaurar(conexao: Connection) -> None:
    """Devolve a conexão ao papel proprietário, mesmo após transação abortada."""

    try:
        conexao.execute(
            text("select set_config('role', :papel, true)"),
            {"papel": PAPEL_PROPRIETARIO},
        )
    except Exception:  # noqa: BLE001 - qualquer erro aqui exige o mesmo reparo
        # Uma política que barrou a operação aborta a transação; sem o
        # rollback, nem o reset do papel passaria.
        conexao.rollback()
        conexao.execute(
            text("select set_config('role', :papel, true)"),
            {"papel": PAPEL_PROPRIETARIO},
        )


__all__ = [
    "PAPEL_AUTENTICADO",
    "PAPEL_PROPRIETARIO",
    "SessaoInvalida",
    "aplicar_identidade",
    "como_servico",
    "como_usuario",
]
