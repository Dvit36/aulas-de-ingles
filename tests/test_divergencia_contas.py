"""O detector de perfil fora de sincronia com a conta no Auth.

Dois defeitos motivaram este módulo: renomear o perfil sem mover a conta, e
reativar sem levantar o ban. Nenhum levantava exceção; o sintoma era o aluno
não conseguir entrar.

E o próprio detector já causou um terceiro. Sua primeira versão consultava
`auth.users` direto, o que o papel `authenticated` não pode fazer: a consulta
falhava, **a transação abortava**, e a aba Alunos inteira morria com "current
transaction is aborted". Capturar a exceção em Python não desfaz o abort — daí
o SAVEPOINT. Por isso o teste que mais importa aqui não é o de detectar coisa
alguma, é o de falhar sem derrubar a tela.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

import streamlit_app
from english_leaderboard import contas as contas_modulo
from english_leaderboard.contas import (
    classificar_divergencias,
    divergencias_de_conta,
)
from english_leaderboard.schema import Role, User

DOMINIO = "robonaticos7565.invalid"
AGORA = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
FUTURO = datetime(2126, 1, 1, tzinfo=UTC)
PASSADO = datetime(2026, 1, 1, tzinfo=UTC)


def _classificar(linhas):
    return classificar_divergencias(linhas, dominio=DOMINIO, agora=AGORA)


# ------------------------------------------------------------- a classificação

def test_a_renamed_profile_whose_account_stayed_behind_is_reported() -> None:
    """O caso real: o perfil virou `luiz.brito`, o login seguia em `luiz`."""

    achados = _classificar([("id-1", "luiz.brito", True, f"luiz@{DOMINIO}", None)])

    assert [a.tipo for a in achados] == ["endereco"]
    # A mensagem precisa dizer com o que dá para entrar, não só que divergiu.
    assert f"luiz@{DOMINIO}" in achados[0].detalhe
    assert "luiz.brito" in achados[0].detalhe


def test_an_active_profile_over_a_banned_account_is_reported() -> None:
    """Reativar sem levantar o ban tranca o aluno fora sem sintoma."""

    achados = _classificar(
        [("id-2", "ana.silva", True, f"ana.silva@{DOMINIO}", FUTURO)]
    )

    assert [a.tipo for a in achados] == ["acesso_bloqueado"]


def test_an_expired_ban_blocks_nobody_and_is_not_reported() -> None:
    """`banned_until` no passado não recusa login: avisar seria ruído."""

    assert _classificar(
        [("id-3", "bia.costa", True, f"bia.costa@{DOMINIO}", PASSADO)]
    ) == []


def test_an_inactive_profile_over_a_banned_account_is_coherent() -> None:
    """Inativo e banido é o par funcionando, não uma divergência."""

    assert _classificar(
        [("id-4", "caio.dias", False, f"caio.dias@{DOMINIO}", FUTURO)]
    ) == []


def test_a_profile_without_any_account_is_reported() -> None:
    """`left join` sem par: perfil que não tem como entrar de jeito nenhum."""

    achados = _classificar([("id-5", "sem.conta", True, None, None)])

    assert [a.tipo for a in achados] == ["sem_conta"]


def test_a_matching_pair_is_not_reported() -> None:
    assert _classificar(
        [("id-6", "enzo.souza", True, f"enzo.souza@{DOMINIO}", None)]
    ) == []


def test_both_defects_on_the_same_account_are_reported_separately() -> None:
    """Renomeado e banido são problemas distintos, com correções distintas."""

    achados = _classificar([("id-7", "novo.nome", True, f"antigo@{DOMINIO}", FUTURO)])

    assert sorted(a.tipo for a in achados) == ["acesso_bloqueado", "endereco"]


# -------------------------------------------------------------- a contenção

def test_without_the_auth_schema_the_check_stays_quiet(session) -> None:
    """Em SQLite não existe o schema `auth`, e não há o que comparar."""

    assert divergencias_de_conta(session, dominio=DOMINIO) == []


def test_a_failing_query_leaves_the_session_usable(session, monkeypatch) -> None:
    """O SAVEPOINT é a razão de a falha não contaminar quem chamou.

    Aqui a função de reconciliação não existe — é o mesmo que num banco onde a
    migração `0009` ainda não passou. A consulta falha de verdade, e a sessão
    precisa continuar servindo a consulta seguinte, que foi a que morreu em
    produção.

    **Este teste não tem dentes contra a regressão real, e é honesto dizer.**
    Verificado tirando o SAVEPOINT: ele continua passando. O SQLite não invalida
    a transação depois de um comando que falha, então o modo de falha do
    PostgreSQL — "current transaction is aborted" — não existe aqui e nenhum
    teste desta suíte consegue reproduzi-lo. O que trava a regressão é o
    `test_the_screen_survives_a_detector_that_blows_up`, esse sim falha sem a
    proteção. Este fica como documentação executável do contrato.
    """

    monkeypatch.setattr(contas_modulo, "exige_conta_no_auth", lambda _s: True)

    assert divergencias_de_conta(session, dominio=DOMINIO) == []

    # A consulta da linha que quebrou a aba Alunos.
    assert session.scalars(select(User).order_by(User.display_name)).all()


# ----------------------------------------------- o diagnóstico não é fatal

class StPermissivo:
    """`st` que aceita qualquer chamada e guarda o que interessa.

    A tela usa dezenas de widgets; o que este teste afirma não é o desenho
    dela, e sim que `users_view` chega ao fim.
    """

    def __init__(self) -> None:
        self.avisos: list[str] = []
        self.session_state: dict = {}

    @property
    def avisos_de_sincronia(self) -> list[str]:
        """A tela tem avisos próprios; só interessa o do detector."""

        return [a for a in self.avisos if "fora de sincronia" in a]

    def warning(self, texto, **_):
        self.avisos.append(str(texto))

    def columns(self, quantidade, **_):
        return [self for _ in range(quantidade if isinstance(quantidade, int) else len(quantidade))]

    def selectbox(self, _rotulo, opcoes=(), **_):
        opcoes = list(opcoes)
        return opcoes[0] if opcoes else None

    def checkbox(self, *_a, **_k):
        return False

    def form_submit_button(self, *_a, **_k):
        return False

    def button(self, *_a, **_k):
        return False

    def text_input(self, _rotulo, value="", **_):
        return value

    @contextmanager
    def _bloco(self, *_a, **_k):
        yield self

    form = expander = container = _bloco

    def __getattr__(self, _nome):
        return lambda *a, **k: None


@pytest.fixture
def st_falso(monkeypatch):
    falso = StPermissivo()
    monkeypatch.setattr(streamlit_app, "st", falso)
    return falso


def test_the_screen_survives_a_detector_that_blows_up(
    session, users, settings, st_falso, monkeypatch
) -> None:
    """A regressão que este arquivo existe para não deixar voltar.

    Um diagnóstico que derruba a tela que deveria informar é pior que
    diagnóstico nenhum. Se `users_view` deixar a exceção subir, a aba Alunos
    sai do ar — e foi o que aconteceu.
    """

    def explodir(*_a, **_k):
        raise RuntimeError("permission denied for table users")

    monkeypatch.setattr(streamlit_app, "divergencias_de_conta", explodir)

    streamlit_app.users_view(session, users[Role.ADMIN], settings)

    assert st_falso.avisos_de_sincronia == []


def test_the_screen_shows_the_warning_when_there_is_a_divergence(
    session, users, settings, st_falso, monkeypatch
) -> None:
    """E quando há divergência, ela precisa de fato aparecer."""

    from english_leaderboard.contas import DivergenciaDeConta

    monkeypatch.setattr(
        streamlit_app,
        "divergencias_de_conta",
        lambda *_a, **_k: [
            DivergenciaDeConta(
                user_id="id-1",
                username="luiz.brito",
                tipo="endereco",
                detalhe="O login só aceita 'luiz@…'.",
            )
        ],
    )

    streamlit_app.users_view(session, users[Role.ADMIN], settings)

    assert len(st_falso.avisos_de_sincronia) == 1
    assert "1 conta(s)" in st_falso.avisos_de_sincronia[0]
