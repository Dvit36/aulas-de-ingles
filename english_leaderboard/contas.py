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

    def atualizar_username(self, user_id: str, username: str) -> str: ...

    def desativar(self, user_id: str) -> None: ...

    def reativar(self, user_id: str) -> None: ...

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

    def atualizar_username(self, user_id: str, username: str) -> str:
        """Leva o username novo ao Auth, que é quem o login consulta.

        O perfil sozinho não basta: o e-mail da conta é o que autentica.
        """

        return supabase_auth.atualizar_username(
            self.gateway,
            user_id=user_id,
            username=username,
            chave_secreta=self.chave_secreta,
            dominio=self.dominio,
        )

    def desativar(self, user_id: str) -> None:
        """Revoga o acesso no Auth.

        Marcar o perfil como inativo não bastaria: a sessão já emitida
        continuaria válida até expirar sozinha.
        """

        supabase_auth.desativar_conta(
            self.gateway, user_id=user_id, chave_secreta=self.chave_secreta
        )

    def reativar(self, user_id: str) -> None:
        """Devolve o acesso que ``desativar`` tirou.

        O par existe porque só o perfil voltar a ``active`` não desfaz o ban.
        """

        supabase_auth.reativar_conta(
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


def contas_disponiveis(settings: Settings) -> ContasSupabase | None:
    """A fachada do Auth, quando há credencial privilegiada para usá-la.

    Devolver ``None`` em vez de levantar deixa a decisão com quem for criar o
    perfil: em SQLite o perfil solto é legítimo, e em PostgreSQL a recusa vem
    de ``_identidade_para_perfil`` com a mensagem que nomeia a variável que
    falta. Chamar ``contas_de`` direto levantaria um ``ValueError`` genérico
    sobre a chave secreta mesmo onde nenhuma conta precisaria ser criada.
    """

    if not settings.contas_administraveis:
        return None
    return contas_de(settings)


class ContaObrigatoria(RuntimeError):
    """Perfil sem conta no Auth em um banco que exige ``auth.users``."""


def exige_conta_no_auth(session) -> bool:
    """O banco desta sessão amarra ``profiles.id`` a ``auth.users(id)``?

    No PostgreSQL do Supabase, sim: é a chave estrangeira declarada em
    ``0001_schema_inicial.sql``. No SQLite de desenvolvimento e da suíte não
    existe schema ``auth`` nenhum, e o perfil solto é legítimo — é o que
    permite exercitar catálogo e pontuação sem serviço de autenticação.

    Essa diferença é exatamente por que a suíte não pegava o problema: em
    SQLite o insert passa, em PostgreSQL ele viola a chave estrangeira.
    """

    return session.get_bind().dialect.name == "postgresql"


def conta_existe_no_auth(session, user_id: str) -> bool:
    """Confirma no banco que o UUID veio mesmo do Supabase Auth.

    Chamado antes de gravar o perfil. Um identificador que a Admin API não
    reconheceu — resposta inesperada, projeto trocado no meio do caminho —
    vira uma mensagem clara aqui, em vez de uma violação de chave estrangeira
    solta no meio do startup.
    """

    from sqlalchemy import text

    encontrado = session.execute(
        text("select 1 from auth.users where id = :id"), {"id": str(user_id)}
    ).first()
    return encontrado is not None


def _identidade_para_perfil(
    session,
    contas: Contas | None,
    *,
    username: str,
    display_name: str,
) -> str:
    """Devolve o UUID que o perfil vai usar, ou explica por que não há um.

    Com ``contas``, a conta nasce no Supabase Auth e o id vem de lá — é assim
    que os dois lados ficam amarrados. Sem ``contas``, só um banco sem
    ``auth.users`` aceita um perfil solto.
    """

    from .schema import new_id

    if contas is None:
        if exige_conta_no_auth(session):
            raise ContaObrigatoria(
                f"Não é possível criar o perfil {username!r} sem uma conta no "
                "Supabase Auth: profiles.id referencia auth.users(id) e o "
                "insert violaria a chave estrangeira. Defina SUPABASE_SECRET_KEY "
                "nos Secrets — é a chave privilegiada que cria a conta pela "
                "Admin API."
            )
        return new_id()

    identificador, _ = contas.criar(username=username, display_name=display_name)
    if exige_conta_no_auth(session) and not conta_existe_no_auth(
        session, identificador
    ):
        raise ContaObrigatoria(
            f"O Supabase Auth devolveu o id {identificador!r} para {username!r}, "
            "mas ele não está em auth.users. Gravar o perfil violaria a chave "
            "estrangeira. Confira se SUPABASE_URL e SUPABASE_SECRET_KEY apontam "
            "para o mesmo projeto de SUPABASE_DB_URL."
        )
    return identificador




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
    o catálogo e a pontuação, e não finge que existe login. Isso vale só onde
    o banco não tem ``auth.users``; em PostgreSQL a criação é recusada com a
    variável que falta, em vez de violar a chave estrangeira.
    """

    from sqlalchemy import select

    from .schema import Role, User
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

    identificador = _identidade_para_perfil(
        session,
        contas,
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


__all__ = [
    "ContaObrigatoria",
    "Contas",
    "ContasSupabase",
    "bootstrap_admin",
    "conta_existe_no_auth",
    "contas_de",
    "contas_disponiveis",
    "exige_conta_no_auth",
]
