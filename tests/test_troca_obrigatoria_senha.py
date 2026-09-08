"""A senha temporária precisa ser trocada antes de qualquer navegação.

`create_user_account` e `reset_user_password` geram uma senha de 16 caracteres
que o administrador entrega em mãos. A obrigação de substituí-la é estado em
`profiles.must_change_password`, e a trava fica em `_guarded_route` — por onde
toda rota autenticada passa.

O teste que mais importa aqui não é o da trava: é o de que **quem já existia
não é afetado**. A migração não tem backfill, e um perfil criado antes dela
precisa continuar navegando. O contrário é o administrador perdendo acesso ao
próprio aplicativo, sem ninguém para destravá-lo.
"""

from __future__ import annotations

from contextlib import contextmanager
from uuid import uuid4

import pytest

import streamlit_app
from english_leaderboard.schema import Role, User, new_id
from english_leaderboard.services import (
    concluir_troca_de_senha,
    create_user_account,
    reset_user_password,
)


class ContasFalsas:
    def __init__(self) -> None:
        self.criadas: dict[str, str] = {}

    def criar(self, *, username: str, display_name: str) -> tuple[str, str]:
        identificador = str(uuid4())
        self.criadas[identificador] = username
        return identificador, "senha-temporaria-16x"

    def redefinir_senha(self, user_id: str) -> str:
        return "outra-senha-temporaria"

    def atualizar_username(self, user_id, username):  # pragma: no cover
        raise AssertionError("não deveria ser chamado")

    def desativar(self, user_id):  # pragma: no cover
        raise AssertionError("não deveria ser chamado")

    def reativar(self, user_id):  # pragma: no cover
        raise AssertionError("não deveria ser chamado")

    def remover(self, user_id):  # pragma: no cover
        raise AssertionError("não deveria ser chamado")


# ------------------------------------------- onde a obrigação é criada

def test_a_new_account_is_born_owing_the_change(session, users) -> None:
    """A senha devolvida ao administrador é temporária desde o primeiro minuto."""

    conta, senha = create_user_account(
        session,
        actor=users[Role.ADMIN],
        contas=ContasFalsas(),
        username="novo.aluno",
        display_name="Novo Aluno",
    )
    session.commit()

    assert senha
    assert conta.must_change_password is True


def test_resetting_a_password_puts_the_obligation_back(session, users) -> None:
    """Redefinir devolve o aluno à mesma situação da criação.

    É por isso que a marca é "senha temporária" e não "primeiro acesso": um
    sinal de primeiro acesso deixaria este caso de fora.
    """

    alvo = users[Role.STUDENT]
    assert alvo.must_change_password is False

    reset_user_password(
        session, actor=users[Role.ADMIN], contas=ContasFalsas(), user_id=alvo.id
    )
    session.commit()

    assert session.get(User, alvo.id).must_change_password is True


def test_finishing_the_change_releases_and_leaves_a_trail(session, users) -> None:
    from english_leaderboard.schema import AuditLog

    alvo = users[Role.STUDENT]
    alvo.must_change_password = True
    session.flush()

    concluir_troca_de_senha(session, actor=alvo)
    session.commit()

    assert session.get(User, alvo.id).must_change_password is False
    acoes = [linha.action for linha in session.query(AuditLog).all()]
    assert "user_password_changed" in acoes


# ------------------------------------------------------------- a trava

class StFalso:
    """Registra o que a rota desenhou, sem desenhar nada."""

    def __init__(self) -> None:
        self.session_state: dict = {}
        self.cabecalhos: list[str] = []
        self.erros: list[str] = []

    def header(self, texto, **_):
        self.cabecalhos.append(str(texto))

    def error(self, texto, **_):
        self.erros.append(str(texto))

    def form_submit_button(self, *_a, **_k):
        return False

    def button(self, *_a, **_k):
        return False

    def text_input(self, _rotulo, value="", **_):
        return value

    def checkbox(self, *_a, **_k):
        return False

    def selectbox(self, _rotulo, opcoes=(), **_):
        opcoes = list(opcoes)
        return opcoes[0] if opcoes else None

    def columns(self, quantidade, **_):
        n = quantidade if isinstance(quantidade, int) else len(quantidade)
        return [self for _ in range(n)]

    @contextmanager
    def _bloco(self, *_a, **_k):
        yield self

    form = expander = container = _bloco

    def __getattr__(self, _nome):
        return lambda *a, **k: None


@pytest.fixture
def st_falso(monkeypatch):
    falso = StFalso()
    monkeypatch.setattr(streamlit_app, "st", falso)
    return falso


def _rota_guardada(session, settings, actor, papeis):
    estado = streamlit_app.AuthenticationState(actor=actor)
    destino = {"renderizou": False}

    def render_original():
        destino["renderizou"] = True

    rota = streamlit_app.PageRoute(
        "Destino", "destino", ":material/home:", render_original
    )
    guardada = streamlit_app._guarded_route(
        rota,
        session=session,
        settings=settings,
        auth_state=estado,
        allowed_roles=frozenset(papeis),
    )
    guardada.render()
    return destino["renderizou"]


def test_a_marked_account_never_reaches_the_route_it_asked_for(
    session, users, settings, st_falso
) -> None:
    """A trava vem antes da checagem de papel: o destino não importa."""

    alvo = users[Role.STUDENT]
    alvo.must_change_password = True
    session.flush()

    chegou = _rota_guardada(session, settings, alvo, {Role.STUDENT})

    assert chegou is False
    assert "Troque sua senha" in st_falso.cabecalhos


def test_the_navigation_bar_offers_nothing_while_the_change_is_owed(
    session, users, settings, st_falso
) -> None:
    """A barra não oferece o que não abre."""

    alvo = users[Role.STUDENT]
    alvo.must_change_password = True
    session.flush()
    estado = streamlit_app.AuthenticationState(actor=alvo)

    assert streamlit_app._visible_routes(session, settings, estado) == []


# ------------------------- o cenário que não pode acontecer: perder o próprio app

def test_a_profile_created_before_the_migration_navigates_normally(
    session, users, settings, st_falso
) -> None:
    """O administrador criado à mão, antes da coluna existir, não é trancado.

    A migração `0010` cria a coluna com `default false` e **sem backfill**, de
    propósito. Este teste trava essa decisão: se alguém um dia acrescentar um
    backfill marcando todo mundo, ou inverter o `default`, o administrador
    perde acesso ao próprio aplicativo e não há quem o destrave por dentro.
    """

    # Um perfil como o que já existia em produção: inserido direto, sem passar
    # por `create_user_account`, e sem nada dizer sobre senha temporária.
    veterano = User(
        id=new_id(),
        username="luiz",
        display_name="Luiz",
        role=Role.ADMIN,
        active=True,
    )
    session.add(veterano)
    session.flush()

    assert veterano.must_change_password is False

    chegou = _rota_guardada(session, settings, veterano, {Role.ADMIN})

    assert chegou is True
    assert st_falso.cabecalhos == []
    estado = streamlit_app.AuthenticationState(actor=veterano)
    assert streamlit_app._visible_routes(session, settings, estado) != []
