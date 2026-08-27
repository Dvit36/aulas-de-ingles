"""Conta de acesso e perfil como uma coisa só.

Depois da migração a identidade mora em dois lugares por construção: a conta
no Supabase Auth (que sabe autenticar) e o perfil no PostgreSQL (que sabe o
papel, o nome e a pontuação). O perfil referencia ``auth.users`` por chave
estrangeira, então nenhum dos dois pode ser criado sozinho sem deixar o outro
inconsistente.

Este módulo é a fachada que mantém os dois em passo. A camada de serviço fala
com ele em vez de falar com o Auth direto, e os testes injetam um duplo do
gateway — sem rede, sem chave, sem conta de verdade.

A chave privilegiada aparece aqui e em nenhum outro lugar do domínio: criar,
banir e remover conta exigem a Admin API. Quem chama já verificou que o ator é
administrador; esta camada não decide autorização, só executa.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from . import supabase_auth
from .config import Settings
from .supabase_auth import AuthGateway, HttpAuthGateway


@runtime_checkable
class Contas(Protocol):
    """Operações de conta que a camada de serviço precisa."""

    def criar(self, *, username: str, display_name: str) -> tuple[str, str]: ...

    def redefinir_senha(self, user_id: str) -> str: ...

    def desativar(self, user_id: str) -> None: ...

    def remover(self, user_id: str) -> None: ...


@dataclass(frozen=True, slots=True)
class ContasSupabase:
    """Implementação real. ``gateway`` é trocável para teste."""

    gateway: AuthGateway
    chave_secreta: str
    dominio: str

    def criar(self, *, username: str, display_name: str) -> tuple[str, str]:
        """Cria a conta e devolve ``(id, senha temporária)``.

        O id devolvido é o que o perfil vai usar como chave primária: é assim
        que os dois lados ficam amarrados.
        """

        return supabase_auth.criar_conta(
            self.gateway,
            username=username,
            display_name=display_name,
            chave_secreta=self.chave_secreta,
            dominio=self.dominio,
        )

    def redefinir_senha(self, user_id: str) -> str:
        return supabase_auth.redefinir_senha(
            self.gateway, user_id=user_id, chave_secreta=self.chave_secreta
        )

    def desativar(self, user_id: str) -> None:
        """Revoga o acesso no Auth.

        Marcar o perfil como inativo não bastaria: a sessão já emitida
        continuaria válida até expirar sozinha.
        """

        supabase_auth.desativar_conta(
            self.gateway, user_id=user_id, chave_secreta=self.chave_secreta
        )

    def remover(self, user_id: str) -> None:
        supabase_auth.remover_conta(
            self.gateway, user_id=user_id, chave_secreta=self.chave_secreta
        )


def contas_de(settings: Settings) -> ContasSupabase:
    """Monta a fachada a partir da configuração da aplicação."""

    return ContasSupabase(
        gateway=HttpAuthGateway(settings.supabase_url),
        chave_secreta=settings.admin_secret_key(),
        dominio=settings.supabase_username_domain,
    )




def bootstrap_admin(
    session, settings: Settings, contas: Contas | None = None
):
    """Garante o primeiro administrador, uma única vez e sem sobrescrever.

    Cria a conta no Supabase Auth com a senha de bootstrap e o perfil que a
    referencia. Se já existe algum administrador, não faz nada — trocar a
    senha de quem já usa o sistema por variável de ambiente seria uma porta
    dos fundos.

    Sem ``contas`` (desenvolvimento e testes, onde não há serviço de
    autenticação) o perfil é criado sozinho, sem acesso: é o suficiente para
    o catálogo e a pontuação, e não finge que existe login.
    """

    from sqlalchemy import select

    from .schema import Role, User, new_id
    from .supabase_auth import normalize_username

    existente = session.scalar(
        select(User)
        .where(User.role == Role.ADMIN, User.active.is_(True))
        .order_by(User.created_at)
    )
    if existente is not None:
        return existente
    if not (
        settings.bootstrap_admin_name
        and settings.bootstrap_admin_username
        and settings.bootstrap_admin_password
    ):
        return None
    try:
        username = normalize_username(settings.bootstrap_admin_username)
    except ValueError as erro:
        raise ValueError(f"BOOTSTRAP_ADMIN_USERNAME inválido: {erro}") from erro

    admin = session.scalar(select(User).where(User.username == username))
    if admin is not None:
        admin.role = Role.ADMIN
        admin.active = True
        admin.archived_at = None
        session.flush()
        return admin

    if contas is None:
        identificador = new_id()
    else:
        identificador, _ = contas.criar(
            username=username,
            display_name=settings.bootstrap_admin_name.strip(),
        )
    admin = User(
        id=identificador,
        username=username,
        display_name=settings.bootstrap_admin_name.strip(),
        role=Role.ADMIN,
        active=True,
    )
    session.add(admin)
    session.flush()
    return admin


__all__ = ["Contas", "ContasSupabase", "bootstrap_admin", "contas_de"]
