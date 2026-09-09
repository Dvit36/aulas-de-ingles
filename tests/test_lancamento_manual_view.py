"""As duas telas do lançamento manual: a que escreve e a que é lida.

O que estes testes travam é o que o commit da tela prometeu, e que só a tela
pode cumprir — o serviço não sabe o que foi desenhado:

* o saldo do aluno aparece **antes** de confirmar;
* as duas advertências sobre o motivo (o aluno lê; não dá para editar depois)
  aparecem juntas, no mesmo lugar, antes de confirmar;
* o motivo escrito aparece na lista de onde se estorna — sem ele não há como
  saber qual lançamento estornar;
* o botão de estornar só existe onde o serviço aceitaria;
* o aluno vê o lançamento marcado, com o motivo, e nenhum controle.

O duplo de `st` não desenha nada: guarda o que a tela pediu para desenhar, e
seu `rerun` interrompe a passada como o de verdade.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from sqlalchemy import select

import streamlit_app
from english_leaderboard.schema import LedgerTransaction, Role
from english_leaderboard.scoring import student_total
from english_leaderboard.services import create_points_adjustment, estornar_lancamento


class _Rerun(Exception):
    """O que `st.rerun()` faz: aborta a passada corrente."""


class StFalso:
    """Guarda o que a tela desenhou, e responde por um widget de cada vez."""

    def __init__(self, estado: dict) -> None:
        self.session_state = estado
        self.clicar: str | None = None
        self.alvo_id: str | None = None
        self.texto: str = ""
        self.numero: int = 1
        self.sucessos: list[str] = []
        self.erros: list[str] = []
        self.avisos: list[str] = []
        self.botoes: list[str] = []
        self.rotulos_de_texto: list[str] = []
        self.metricas: list[tuple[str, object]] = []
        # Tudo que é texto corrido na tela, para procurar advertência.
        self.escrito: list[str] = []

    # --- o que a tela desenha -------------------------------------------
    def success(self, texto, **_):
        self.sucessos.append(str(texto))

    def error(self, texto, **_):
        self.erros.append(str(texto))

    def warning(self, texto, **_):
        self.avisos.append(str(texto))
        self.escrito.append(str(texto))

    def caption(self, texto, **_):
        self.escrito.append(str(texto))

    def markdown(self, texto, **_):
        self.escrito.append(str(texto))

    def write(self, texto, **_):
        self.escrito.append(str(texto))

    def subheader(self, texto, **_):
        self.escrito.append(str(texto))

    def header(self, texto, **_):
        self.escrito.append(str(texto))

    def metric(self, rotulo, valor, **_):
        self.metricas.append((str(rotulo), valor))

    # --- o que a tela pergunta ------------------------------------------
    def button(self, rotulo, **_):
        self.botoes.append(rotulo)
        return rotulo == self.clicar

    def form_submit_button(self, rotulo, **_):
        self.botoes.append(rotulo)
        return rotulo == self.clicar

    def text_area(self, rotulo, **_):
        self.rotulos_de_texto.append(str(rotulo))
        return self.texto

    def number_input(self, _rotulo, **_):
        return self.numero

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


def _renderizador(monkeypatch, tela):
    monkeypatch.setattr(streamlit_app, "persist_committed_changes", lambda *a, **k: None)
    estado: dict = {}

    def render(*args, clicar=None, alvo=None, texto="", numero=1) -> StFalso:
        falso = StFalso(estado)
        falso.clicar = clicar
        falso.alvo_id = alvo
        falso.texto = texto
        falso.numero = numero
        monkeypatch.setattr(streamlit_app, "st", falso)
        try:
            tela(*args)
        except _Rerun:
            falso.reiniciou = True
        else:
            falso.reiniciou = False
        return falso

    render.estado = estado
    return render


@pytest.fixture
def tela_admin(monkeypatch):
    return _renderizador(monkeypatch, streamlit_app.manual_points_view)


@pytest.fixture
def tela_aluno(monkeypatch):
    return _renderizador(monkeypatch, streamlit_app._manual_points_panel)


# ------------------------------------------------- antes de confirmar

def test_o_saldo_do_aluno_aparece_antes_do_formulario(
    session, users, settings, tela_admin
) -> None:
    """Sem o saldo, 20 pontos é um número sem escala."""

    admin, aluno = users[Role.ADMIN], users[Role.STUDENT]
    create_points_adjustment(
        session,
        actor=admin,
        student_id=aluno.id,
        points=30,
        reason="Apresentação oral.",
    )
    session.commit()

    desenhada = tela_admin(session, admin, settings, alvo=aluno.id)

    assert (f"Saldo atual de {aluno.display_name}", 30) in desenhada.metricas


def test_as_duas_advertencias_do_motivo_aparecem_juntas(
    session, users, settings, tela_admin
) -> None:
    """O aluno lê, e não dá para editar depois — as duas, antes de confirmar.

    A segunda é consequência do gatilho `ledger_sem_update`, que cobre a coluna
    `reason`: motivo errado só se corrige estornando o lançamento inteiro.
    """

    admin, aluno = users[Role.ADMIN], users[Role.STUDENT]

    desenhada = tela_admin(session, admin, settings, alvo=aluno.id)

    assert any("o aluno vai ler este texto" in r.lower() for r in
               desenhada.rotulos_de_texto), desenhada.rotulos_de_texto
    aviso = next(t for t in desenhada.escrito if "não pode" in t and "editad" in t)
    assert "estornar" in aviso.lower()


def test_o_formulario_recusa_motivo_em_branco_e_nao_grava(
    session, users, settings, tela_admin
) -> None:
    admin, aluno = users[Role.ADMIN], users[Role.STUDENT]

    desenhada = tela_admin(
        session,
        admin,
        settings,
        alvo=aluno.id,
        clicar=f"Lançar para {aluno.display_name}",
        texto="   ",
        numero=10,
    )

    assert not desenhada.reiniciou
    assert desenhada.erros and "motivo" in desenhada.erros[0].lower()
    assert session.scalars(select(LedgerTransaction)).all() == []


def test_lancar_grava_e_o_recado_atravessa_a_rerun(
    session, users, settings, tela_admin
) -> None:
    admin, aluno = users[Role.ADMIN], users[Role.STUDENT]

    confirmado = tela_admin(
        session,
        admin,
        settings,
        alvo=aluno.id,
        clicar=f"Lançar para {aluno.display_name}",
        texto="Apresentação oral na aula do dia 3.",
        numero=25,
    )

    assert confirmado.reiniciou
    assert confirmado.sucessos == []
    assert student_total(session, aluno.id) == 25
    gravado = session.scalars(select(LedgerTransaction)).one()
    assert gravado.reason == "Apresentação oral na aula do dia 3."

    depois = tela_admin(session, admin, settings, alvo=aluno.id)
    assert depois.sucessos and "25 ponto(s)" in depois.sucessos[0]
    assert "manual_points_notice" not in tela_admin.estado


# ------------------------------------------------------------ a lista

def test_a_lista_mostra_o_motivo_escrito(
    session, users, settings, tela_admin
) -> None:
    """Sem o motivo à vista não há como saber qual lançamento estornar.

    Data e valor não distinguem dois lançamentos do mesmo dia, e estornar é a
    única saída para um motivo digitado errado.
    """

    admin, aluno = users[Role.ADMIN], users[Role.STUDENT]
    create_points_adjustment(
        session,
        actor=admin,
        student_id=aluno.id,
        points=10,
        reason="Participação na conversação de quinta.",
    )
    session.commit()

    desenhada = tela_admin(session, admin, settings, alvo=aluno.id)

    assert "Participação na conversação de quinta." in desenhada.escrito


def test_o_botao_de_estorno_so_aparece_onde_o_servico_aceitaria(
    session, users, settings, tela_admin
) -> None:
    """Um lançamento intacto, um já estornado e o estorno em si.

    A tela não pode oferecer o que `estornar_lancamento` vai recusar: o botão
    existiria só para produzir uma mensagem de erro.
    """

    admin, aluno = users[Role.ADMIN], users[Role.STUDENT]
    create_points_adjustment(
        session, actor=admin, student_id=aluno.id, points=10, reason="Intacto."
    )
    errado = create_points_adjustment(
        session, actor=admin, student_id=aluno.id, points=50, reason="Errei o valor."
    )
    estornar_lancamento(
        session, actor=admin, transaction_id=errado.id, reason="Estorno: eram 5."
    )
    session.commit()

    desenhada = tela_admin(session, admin, settings, alvo=aluno.id)

    assert desenhada.botoes.count("Confirmar estorno") == 1


def test_estornar_pela_tela_desfaz_o_efeito(
    session, users, settings, tela_admin
) -> None:
    admin, aluno = users[Role.ADMIN], users[Role.STUDENT]
    create_points_adjustment(
        session, actor=admin, student_id=aluno.id, points=40, reason="Lancei errado."
    )
    session.commit()

    confirmado = tela_admin(
        session,
        admin,
        settings,
        alvo=aluno.id,
        clicar="Confirmar estorno",
        texto="Estorno: os pontos eram de outro aluno.",
    )

    assert confirmado.reiniciou
    assert student_total(session, aluno.id) == 0
    depois = tela_admin(session, admin, settings, alvo=aluno.id)
    assert depois.sucessos == ["Lançamento estornado."]


def test_o_estorno_pela_tela_recusa_motivo_em_branco(
    session, users, settings, tela_admin
) -> None:
    admin, aluno = users[Role.ADMIN], users[Role.STUDENT]
    create_points_adjustment(
        session, actor=admin, student_id=aluno.id, points=40, reason="Lancei errado."
    )
    session.commit()

    desenhada = tela_admin(
        session,
        admin,
        settings,
        alvo=aluno.id,
        clicar="Confirmar estorno",
        texto="",
    )

    assert not desenhada.reiniciou
    assert desenhada.erros and "motivo" in desenhada.erros[0].lower()
    assert student_total(session, aluno.id) == 40


# ----------------------------------------------------- a tela do aluno

def test_o_aluno_ve_o_lancamento_marcado_com_o_motivo(
    session, users, tela_aluno
) -> None:
    admin, aluno = users[Role.ADMIN], users[Role.STUDENT]
    errado = create_points_adjustment(
        session, actor=admin, student_id=aluno.id, points=50, reason="Errei o valor."
    )
    estornar_lancamento(
        session, actor=admin, transaction_id=errado.id, reason="Estorno: eram 5."
    )
    create_points_adjustment(
        session, actor=admin, student_id=aluno.id, points=5, reason="O valor certo."
    )
    session.commit()

    desenhada = tela_aluno(session, aluno)

    tudo = "\n".join(desenhada.escrito)
    assert "Errei o valor." in tudo
    assert "Estorno: eram 5." in tudo
    assert "O valor certo." in tudo
    # Marcado como manual, e o par identificado nos dois sentidos.
    assert "Pontos lançados pela administração" in tudo
    assert "· estornado" in tudo
    assert "· estorno" in tudo
    # A tela do aluno não oferece controle nenhum sobre o ledger.
    assert desenhada.botoes == []


def test_quem_nunca_recebeu_lancamento_manual_nao_ve_a_secao(
    session, users, tela_aluno
) -> None:
    aluno = users[Role.STUDENT]

    desenhada = tela_aluno(session, aluno)

    assert desenhada.escrito == []
