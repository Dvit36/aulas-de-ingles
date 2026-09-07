"""A tela de gestão precisa dizer o que fez, e perguntar antes do irreversível.

Três defeitos moraram aqui, e os três eram silêncio:

* a confirmação da exclusão era desenhada e destruída no mesmo instante, por um
  `st.rerun()` logo depois;
* o aviso era genérico e nunca dizia se **aquela** conta seria arquivada ou
  removida para sempre;
* o formulário de exclusão não chegava a executar o ramo do submit em produção,
  enquanto o de redefinir senha, no expander vizinho, funcionava. A confirmação
  passou a ser dois botões com chave própria, como no catálogo de atividades.

O duplo de `st` não desenha nada: guarda o que a tela pediu para desenhar. E o
`rerun` dele **interrompe** a passada, como o de verdade — sem isso um teste
afirmaria coisas sobre código que o Streamlit nunca executaria.
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


class _Rerun(Exception):
    """O que `st.rerun()` faz: aborta a passada corrente."""


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
    """Guarda o que a tela desenhou e clica em um botão por passada."""

    def __init__(self, estado: dict) -> None:
        self.session_state = estado
        self.clicar: str | None = None
        self.alvo_id: str | None = None
        self.sucessos: list[str] = []
        self.erros: list[str] = []
        self.avisos: list[str] = []
        self.botoes: list[str] = []

    # --- o que a tela desenha -------------------------------------------
    def success(self, texto, **_):
        self.sucessos.append(str(texto))

    def error(self, texto, **_):
        self.erros.append(str(texto))

    def warning(self, texto, **_):
        self.avisos.append(str(texto))

    # --- o que a tela pergunta ------------------------------------------
    def button(self, rotulo, **_):
        self.botoes.append(rotulo)
        return rotulo == self.clicar

    def form_submit_button(self, rotulo, **_):
        self.botoes.append(rotulo)
        return rotulo == self.clicar

    def text_input(self, _rotulo, value="", **_):
        return value

    def checkbox(self, *_a, **_k):
        return False

    def selectbox(self, _rotulo, opcoes=(), **_):
        opcoes = list(opcoes)
        if self.alvo_id is not None and self.alvo_id in opcoes:
            return self.alvo_id
        return opcoes[0] if opcoes else None

    def columns(self, quantidade, **_):
        n = quantidade if isinstance(quantidade, int) else len(quantidade)
        return [self for _ in range(n)]

    def rerun(self, *_a, **_k):
        raise _Rerun

    @contextmanager
    def _bloco(self, *_a, **_k):
        yield self

    form = expander = container = _bloco

    def __getattr__(self, _nome):
        return lambda *a, **k: None


@pytest.fixture
def tela(monkeypatch):
    """Renderiza `users_view` quantas vezes o teste precisar, com estado vivo."""

    contas = ContasFalsas()
    monkeypatch.setattr(streamlit_app, "contas_de", lambda _s: contas)
    monkeypatch.setattr(streamlit_app, "persist_committed_changes", lambda *a, **k: None)
    estado: dict = {}

    def render(session, actor, settings, *, clicar=None, alvo=None) -> StFalso:
        falso = StFalso(estado)
        falso.clicar = clicar
        falso.alvo_id = alvo
        monkeypatch.setattr(streamlit_app, "st", falso)
        try:
            streamlit_app.users_view(session, actor, settings)
        except _Rerun:
            falso.reiniciou = True
        else:
            falso.reiniciou = False
        return falso

    render.contas = contas
    render.estado = estado
    return render


# ------------------------------------------------------ os dois passos

def test_deleting_an_account_takes_two_deliberate_clicks(
    session, users, settings, tela
) -> None:
    """Pedir e confirmar são passadas distintas, como no catálogo.

    O formulário que havia aqui não executava o ramo do submit em produção.
    Estes botões têm chave própria e nenhum widget de identidade variável.
    """

    admin, alvo = users[Role.ADMIN], users[Role.STUDENT]

    primeira = tela(session, admin, settings, alvo=alvo.id)
    assert "Excluir ou arquivar esta conta" in primeira.botoes
    assert tela.contas.removidas == []

    pedido = tela(
        session, admin, settings, alvo=alvo.id, clicar="Excluir ou arquivar esta conta"
    )
    assert pedido.reiniciou
    assert tela.estado["pending_user_delete"] == alvo.id
    assert tela.contas.removidas == []

    confirmado = tela(session, admin, settings, alvo=alvo.id, clicar="Sim, excluir")
    assert confirmado.reiniciou
    assert tela.contas.removidas == [alvo.id]
    # A mensagem atravessa a rerun no estado, em vez de morrer com a passada.
    assert confirmado.sucessos == []
    assert tela.estado["user_delete_notice"] == "Conta excluída."

    depois = tela(session, admin, settings)
    assert depois.sucessos == ["Conta excluída."]
    assert "user_delete_notice" not in tela.estado


def test_cancelling_leaves_the_account_alone(session, users, settings, tela) -> None:
    admin, alvo = users[Role.ADMIN], users[Role.STUDENT]

    tela(session, admin, settings, alvo=alvo.id, clicar="Excluir ou arquivar esta conta")
    cancelado = tela(session, admin, settings, alvo=alvo.id, clicar="Cancelar")

    assert cancelado.reiniciou
    assert "pending_user_delete" not in tela.estado
    assert tela.contas.removidas == []
    assert session.get(User, alvo.id) is not None


def test_switching_accounts_cancels_a_pending_confirmation(
    session, users, settings, tela
) -> None:
    """A pendência guarda o id, não um booleano.

    Um booleano transferiria para outra pessoa a confirmação que se pediu para
    esta — e o clique seguinte apagaria a conta errada.
    """

    admin, alvo = users[Role.ADMIN], users[Role.STUDENT]

    tela(session, admin, settings, alvo=alvo.id, clicar="Excluir ou arquivar esta conta")
    assert tela.estado["pending_user_delete"] == alvo.id

    outra = tela(session, admin, settings, alvo=admin.id)

    # Com outra conta selecionada, a tela volta a pedir, não a confirmar.
    assert "Excluir ou arquivar esta conta" in outra.botoes
    assert "Sim, excluir" not in outra.botoes


# ------------------- o aviso precisa dizer qual dos dois efeitos vai acontecer

def test_an_unused_account_is_announced_as_a_permanent_removal(
    session, users, settings, tela
) -> None:
    """Sem histórico, `archive_or_delete_user` apaga de vez — inclusive o login."""

    admin, alvo = users[Role.ADMIN], users[Role.STUDENT]
    tela(session, admin, settings, alvo=alvo.id, clicar="Excluir ou arquivar esta conta")

    confirmacao = tela(session, admin, settings, alvo=alvo.id)

    aviso = next(a for a in confirmacao.avisos if alvo.display_name in a)
    assert "removida permanentemente" in aviso
    assert "arquivada" not in aviso
    assert "Sim, excluir" in confirmacao.botoes


def test_an_account_with_history_is_announced_as_an_archive(
    session, users, settings, tela
) -> None:
    """Com histórico o efeito é outro, e o aviso e o botão mudam junto."""

    admin, alvo = users[Role.ADMIN], users[Role.STUDENT]
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

    tela(session, admin, settings, alvo=alvo.id, clicar="Excluir ou arquivar esta conta")
    confirmacao = tela(session, admin, settings, alvo=alvo.id)

    aviso = next(a for a in confirmacao.avisos if alvo.display_name in a)
    assert "arquivada" in aviso
    assert "removida permanentemente" not in aviso
    assert "Sim, arquivar" in confirmacao.botoes


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
