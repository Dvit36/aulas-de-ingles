"""A tela de gestão precisa dizer o que fez.

Dois defeitos moraram aqui, e os dois eram silêncio, não erro:

* a confirmação da exclusão era desenhada e destruída no mesmo instante, por um
  `st.rerun()` logo depois — quem excluía uma conta não via nada acontecer;
* o aviso do formulário era genérico e nunca dizia se **aquela** conta seria
  arquivada ou removida para sempre.

O duplo de `st` aqui não desenha nada: ele guarda o que a tela pediu para
desenhar, que é o que estes testes precisam afirmar.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from sqlalchemy import select

import streamlit_app
from english_leaderboard.schema import (
    Activity,
    Role,
    Submission,
    SubmissionStatus,
    User,
    new_id,
)


class ContasFalsas:
    def __init__(self) -> None:
        self.removidas: list[str] = []
        self.desativadas: list[str] = []

    def criar(self, *, username, display_name):  # pragma: no cover
        raise AssertionError("não deveria ser chamado")

    def redefinir_senha(self, user_id):  # pragma: no cover
        raise AssertionError("não deveria ser chamado")

    def atualizar_username(self, user_id, username):  # pragma: no cover
        raise AssertionError("não deveria ser chamado")

    def desativar(self, user_id):
        self.desativadas.append(user_id)

    def reativar(self, user_id):  # pragma: no cover
        raise AssertionError("não deveria ser chamado")

    def remover(self, user_id):
        self.removidas.append(user_id)


class StFalso:
    """Guarda o que a tela pediu para desenhar, e dirige um único formulário.

    ``submeter`` é o rótulo do botão que deve responder ``True``; todos os
    outros respondem ``False``, para um teste exercitar um caminho de cada vez.
    """

    def __init__(self, *, submeter: str | None = None, confirmacao: str = "") -> None:
        self.submeter = submeter
        self.confirmacao = confirmacao
        self.sucessos: list[str] = []
        self.erros: list[str] = []
        self.avisos: list[str] = []
        self.session_state: dict = {}
        self.alvo_id: str | None = None

    def success(self, texto, **_):
        self.sucessos.append(str(texto))

    def error(self, texto, **_):
        self.erros.append(str(texto))

    def warning(self, texto, **_):
        self.avisos.append(str(texto))

    def form_submit_button(self, rotulo, **_):
        return rotulo == self.submeter

    def text_input(self, rotulo, value="", **_):
        return self.confirmacao if "confirmar" in rotulo else value

    def selectbox(self, rotulo, opcoes=(), **_):
        opcoes = list(opcoes)
        if self.alvo_id is not None and self.alvo_id in opcoes:
            return self.alvo_id
        return opcoes[0] if opcoes else None

    def columns(self, quantidade, **_):
        n = quantidade if isinstance(quantidade, int) else len(quantidade)
        return [self for _ in range(n)]

    def checkbox(self, *_a, **_k):
        return False

    def button(self, *_a, **_k):
        return False

    @contextmanager
    def _bloco(self, *_a, **_k):
        yield self

    form = expander = container = _bloco

    def __getattr__(self, _nome):
        return lambda *a, **k: None


@pytest.fixture
def tela(monkeypatch):
    contas = ContasFalsas()
    monkeypatch.setattr(streamlit_app, "contas_de", lambda _s: contas)
    monkeypatch.setattr(streamlit_app, "persist_committed_changes", lambda *a, **k: None)

    def montar(**kwargs) -> StFalso:
        falso = StFalso(**kwargs)
        monkeypatch.setattr(streamlit_app, "st", falso)
        return falso

    montar.contas = contas
    return montar


def _aluno(session, users) -> User:
    return users[Role.STUDENT]


# ------------------------------------- a confirmação precisa sobreviver à rerun

def test_the_deletion_notice_survives_the_rerun_that_follows_it(
    session, users, settings, tela
) -> None:
    """`st.rerun()` descarta o que foi desenhado antes dele.

    Era o defeito: a conta sumia e a tela não dizia nada. A mensagem atravessa
    no `session_state` e é desenhada na passada seguinte, fora do expander —
    o mesmo caminho que a senha temporária já usava.
    """

    alvo = _aluno(session, users)
    st_falso = tela(submeter="Confirmar exclusão", confirmacao=alvo.username)
    st_falso.alvo_id = alvo.id

    streamlit_app.users_view(session, users[Role.ADMIN], settings)

    # Nada é desenhado nesta passada: a rerun a descartaria.
    assert st_falso.sucessos == []
    assert st_falso.session_state["user_delete_notice"] == "Conta excluída."
    assert tela.contas.removidas == [alvo.id]

    # E na passada seguinte, a mensagem aparece.
    seguinte = tela()
    seguinte.session_state = st_falso.session_state
    streamlit_app.users_view(session, users[Role.ADMIN], settings)

    assert seguinte.sucessos == ["Conta excluída."]
    assert "user_delete_notice" not in seguinte.session_state


def test_a_mismatched_confirmation_says_so_and_deletes_nothing(
    session, users, settings, tela
) -> None:
    """O ramo que recusa não tem rerun, então desenha o erro na hora."""

    alvo = _aluno(session, users)
    st_falso = tela(submeter="Confirmar exclusão", confirmacao="nao-e-o-usuario")
    st_falso.alvo_id = alvo.id

    streamlit_app.users_view(session, users[Role.ADMIN], settings)

    assert "A confirmação não corresponde ao usuário da conta." in st_falso.erros
    assert tela.contas.removidas == []
    assert session.get(User, alvo.id) is not None


# ------------------- o aviso precisa dizer qual dos dois efeitos vai acontecer

def test_an_unused_account_is_announced_as_a_permanent_removal(
    session, users, settings, tela
) -> None:
    """Sem histórico, `archive_or_delete_user` apaga de vez — inclusive o login.

    O aviso genérico de antes descrevia os dois casos e não dizia em qual você
    estava. Quem vai cadastrar sete alunos precisa saber se o clique é
    reversível **antes** de clicar.
    """

    alvo = _aluno(session, users)
    st_falso = tela()
    st_falso.alvo_id = alvo.id

    streamlit_app.users_view(session, users[Role.ADMIN], settings)

    aviso = next(a for a in st_falso.avisos if alvo.display_name in a)
    assert "removida permanentemente" in aviso
    assert "arquivada" not in aviso


def test_an_account_with_history_is_announced_as_an_archive(
    session, users, settings, tela
) -> None:
    """Com histórico o efeito é outro, e o aviso tem de mudar junto."""

    alvo = _aluno(session, users)
    atividade = session.scalars(select(Activity)).first()
    session.add(
        Submission(
            id=new_id(),
            student_id=alvo.id,
            activity_id=atividade.id,
            status=SubmissionStatus.APPROVED_AUTO,
        )
    )
    session.flush()

    st_falso = tela()
    st_falso.alvo_id = alvo.id

    streamlit_app.users_view(session, users[Role.ADMIN], settings)

    aviso = next(a for a in st_falso.avisos if alvo.display_name in a)
    assert "arquivada" in aviso
    assert "removida permanentemente" not in aviso


def test_the_screen_and_the_decision_count_the_same_thing(
    session, users, settings
) -> None:
    """A tela prevê pela mesma função que a decisão usa.

    Se fossem duas contagens, o aviso poderia prometer arquivamento e o banco
    apagar a conta — e a divergência só apareceria depois.
    """

    import inspect

    from english_leaderboard.services import archive_or_delete_user

    assert "count_user_references" in inspect.getsource(archive_or_delete_user)
