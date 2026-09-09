"""O ranking passou a vir de um agregado, e o ledger fechou.

Em produção o aluno não lê mais o ledger dos colegas — `ledger_leitura_propria`
— e nunca leu o perfil deles — `perfil_proprio_leitura`. A consulta que montava
o leaderboard, rodando sob o papel dele, devolveria uma linha só. Por isso o
PostgreSQL passa por `public.ranking()`, que é `SECURITY DEFINER` e devolve
soma e nome, nunca a linha.

A suíte roda em SQLite, onde não há RLS nem a função: **estes testes não provam
que o vazamento fechou**. Isso se verifica contra o PostgreSQL, com duas contas
reais, e está registrado em `docs/VALIDACAO_EM_PRODUCAO.md`. O que se trava
aqui é o contrato dos dois lados do desvio — o formato das linhas, a regra de
empate e o que deixou de ser exposto.
"""

from __future__ import annotations

from datetime import timedelta

from english_leaderboard.schema import (
    LedgerKind,
    LedgerTransaction,
    Role,
    User,
    new_id,
    utcnow,
)
from english_leaderboard.scoring import _ranking_no_banco, leaderboard_rows


def _aluno(session, username: str, nome: str, *, active: bool = True) -> User:
    aluno = User(
        id=new_id(), username=username, display_name=nome,
        role=Role.STUDENT, active=active,
    )
    session.add(aluno)
    session.flush()
    return aluno


def _pontuar(session, aluno: User, pontos: int) -> None:
    session.add(
        LedgerTransaction(
            id=new_id(), student_id=aluno.id, points=pontos,
            kind=LedgerKind.ADJUSTMENT, source_type="adjustment",
            source_key=f"teste:{new_id()}", description="teste",
        )
    )
    session.flush()


def test_o_desvio_escolhe_a_funcao_so_no_postgresql(session) -> None:
    """A condição do desvio, travada nos dois sentidos.

    Se ela passar a responder `True` no SQLite, a suíte inteira tentaria uma
    função que não existe. Se passar a responder `False` no PostgreSQL, o
    ranking em produção volta a mostrar um aluno só — e sem erro nenhum, que é
    o modo de falhar que este projeto já pagou caro.
    """

    assert _ranking_no_banco(session) is False

    class SessaoDeOutroBanco:
        def get_bind(self):
            class Bind:
                class dialect:
                    name = "postgresql"
            return Bind()

    assert _ranking_no_banco(SessaoDeOutroBanco()) is True


def test_o_ranking_nao_expoe_o_nome_de_usuario(session, users) -> None:
    """`username` é identificador de login e não é desenhado em tela nenhuma.

    Ele saiu do dicionário inteiro, e não virou campo vazio: campo que existe
    sem valor convida alguém a usá-lo depois.
    """

    _pontuar(session, users[Role.STUDENT], 10)

    linha = leaderboard_rows(session)[0]

    assert set(linha) == {"position", "student_id", "student", "points"}


def test_o_id_do_aluno_vem_como_texto(session, users) -> None:
    """O crachá "Você" e o `next_rival` comparam este campo com `actor.id`.

    O ORM devolve o UUID já como texto; o driver do PostgreSQL devolveria
    objeto `UUID`, e toda comparação passaria a ser falsa em silêncio. Por isso
    o caminho do PostgreSQL faz `student_id::text`. Aqui só dá para travar o
    lado do SQLite — o outro se verifica em produção.
    """

    aluno = users[Role.STUDENT]
    _pontuar(session, aluno, 10)

    linha = leaderboard_rows(session)[0]

    assert isinstance(linha["student_id"], str)
    assert linha["student_id"] == aluno.id


def test_empate_ocupa_a_mesma_posicao_e_desempata_pelo_nome(session, users) -> None:
    """A regra de posição mora na aplicação, e não no banco.

    É o que faz os dois caminhos do agregado produzirem o mesmo ranking: eles
    devolvem soma e nome, e a ordem sai daqui.
    """

    ana = _aluno(session, "ana.silva", "Ana Silva")
    bruno = _aluno(session, "bruno.costa", "Bruno Costa")
    lider = users[Role.STUDENT]
    _pontuar(session, lider, 50)
    _pontuar(session, ana, 20)
    _pontuar(session, bruno, 20)
    session.commit()

    board = leaderboard_rows(session)

    assert [(linha["student"], linha["position"]) for linha in board] == [
        ("Student", 1),
        ("Ana Silva", 2),
        ("Bruno Costa", 2),
    ]


def test_aluno_inativo_fica_de_fora(session, users) -> None:
    """Sem `include_inactive`: o parâmetro não tinha chamador e saiu.

    A regra passou a ser uma só, igual nos dois caminhos — quem está inativo
    não aparece. Parâmetro morto é onde diferença entre os dois se esconderia.
    """

    desligado = _aluno(session, "ex.aluno", "Ex Aluno", active=False)
    _pontuar(session, desligado, 90)
    _pontuar(session, users[Role.STUDENT], 10)
    session.commit()

    board = leaderboard_rows(session)

    assert [linha["student"] for linha in board] == ["Student"]


def test_o_periodo_recorta_a_soma(session, users) -> None:
    aluno = users[Role.STUDENT]
    session.add(
        LedgerTransaction(
            id=new_id(), student_id=aluno.id, points=100,
            kind=LedgerKind.ADJUSTMENT, source_type="adjustment",
            source_key=f"teste:{new_id()}", description="antigo",
            occurred_at=utcnow() - timedelta(days=90),
        )
    )
    _pontuar(session, aluno, 7)
    session.commit()

    assert leaderboard_rows(session)[0]["points"] == 107
    recorte = leaderboard_rows(session, start=utcnow() - timedelta(days=1))
    assert recorte[0]["points"] == 7
