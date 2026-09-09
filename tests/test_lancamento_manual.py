"""Lançamento manual de pontos e o estorno que o desfaz.

O ledger é imutável por gatilho: nenhuma linha é editada ou apagada. Isso põe
todo o peso nas travas de escrita — o que entra errado fica. Estes testes
exercitam justamente elas.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from english_leaderboard.schema import (
    Activity,
    AuditLog,
    LedgerKind,
    LedgerTransaction,
    Role,
)
from english_leaderboard.scoring import student_total
from english_leaderboard.services import (
    create_points_adjustment,
    estornar_lancamento,
    lancamentos_manuais,
)


def test_lancamento_manual_grava_o_motivo_e_soma_no_saldo(session, users) -> None:
    admin = users[Role.ADMIN]
    aluno = users[Role.STUDENT]
    saldo_antes = student_total(session, aluno.id)

    lancamento = create_points_adjustment(
        session,
        actor=admin,
        student_id=aluno.id,
        points=30,
        reason="Apresentação oral na aula do dia 3.",
    )
    session.commit()

    assert lancamento.points == 30
    assert lancamento.reason == "Apresentação oral na aula do dia 3."
    assert lancamento.kind == LedgerKind.ADJUSTMENT
    assert lancamento.reverses_id is None
    assert student_total(session, aluno.id) == saldo_antes + 30


def test_lancamento_manual_recusa_valor_negativo(session, users) -> None:
    """Tirar pontos é estornar, não lançar negativo.

    Um número negativo solto não diz o que desfaz: o aluno veria a perda sem a
    causa, e o ledger não teria como ligar uma coisa à outra.
    """

    admin = users[Role.ADMIN]
    aluno = users[Role.STUDENT]

    with pytest.raises(ValueError, match="negativo"):
        create_points_adjustment(
            session,
            actor=admin,
            student_id=aluno.id,
            points=-30,
            reason="Tirando os pontos do envio errado.",
        )
    session.rollback()

    assert session.scalars(select(LedgerTransaction)).all() == []


@pytest.mark.parametrize("motivo", ["", "   ", None])
def test_lancamento_manual_recusa_motivo_em_branco(session, users, motivo) -> None:
    admin = users[Role.ADMIN]
    aluno = users[Role.STUDENT]

    with pytest.raises(ValueError, match="motivo"):
        create_points_adjustment(
            session,
            actor=admin,
            student_id=aluno.id,
            points=10,
            reason=motivo,
        )
    session.rollback()

    assert session.scalars(select(LedgerTransaction)).all() == []


def test_estorno_zera_o_efeito_sem_tocar_no_original(session, users) -> None:
    admin = users[Role.ADMIN]
    aluno = users[Role.STUDENT]
    original = create_points_adjustment(
        session,
        actor=admin,
        student_id=aluno.id,
        points=40,
        reason="Lancei no aluno errado.",
    )
    session.commit()
    escrito_em = original.occurred_at

    estorno = estornar_lancamento(
        session,
        actor=admin,
        transaction_id=original.id,
        reason="Estorno: os pontos eram da Ana, não do Enzo.",
    )
    session.commit()

    assert estorno.points == -40
    assert estorno.reverses_id == original.id
    assert estorno.reason == "Estorno: os pontos eram da Ana, não do Enzo."
    assert student_total(session, aluno.id) == 0
    # O original continua lá, intacto: a correção aponta para trás.
    session.expire_all()
    recarregado = session.get(LedgerTransaction, original.id)
    assert recarregado.points == 40
    assert recarregado.reason == "Lancei no aluno errado."
    # O SQLite devolve o instante sem fuso; o que importa é não ter mudado.
    assert recarregado.occurred_at.replace(tzinfo=None) == escrito_em.replace(
        tzinfo=None
    )


def test_estorno_registra_auditoria_apontando_o_lancamento(session, users) -> None:
    admin = users[Role.ADMIN]
    aluno = users[Role.STUDENT]
    original = create_points_adjustment(
        session,
        actor=admin,
        student_id=aluno.id,
        points=15,
        reason="Bônus de participação.",
    )
    estorno = estornar_lancamento(
        session,
        actor=admin,
        transaction_id=original.id,
        reason="Estorno: bônus duplicado.",
    )
    session.commit()

    registro = session.scalars(
        select(AuditLog).where(AuditLog.action == "points_adjustment_reversed")
    ).one()
    assert registro.entity_id == estorno.id
    assert registro.before_json["lancamento"] == original.id
    assert registro.reason == "Estorno: bônus duplicado."


def test_um_lancamento_so_pode_ser_estornado_uma_vez(session, users) -> None:
    """A trava da aplicação: dois cliques tirariam o dobro dos pontos."""

    admin = users[Role.ADMIN]
    aluno = users[Role.STUDENT]
    original = create_points_adjustment(
        session,
        actor=admin,
        student_id=aluno.id,
        points=25,
        reason="Redação extra.",
    )
    estornar_lancamento(
        session,
        actor=admin,
        transaction_id=original.id,
        reason="Estorno: a redação era de outra turma.",
    )
    session.commit()

    with pytest.raises(ValueError, match="já foi estornado"):
        estornar_lancamento(
            session,
            actor=admin,
            transaction_id=original.id,
            reason="Estorno de novo, por engano.",
        )
    session.rollback()

    assert student_total(session, aluno.id) == 0


def test_o_banco_tambem_recusa_o_segundo_estorno(session, users) -> None:
    """A segunda camada, para o caso de dois cliques passarem juntos pela trava.

    O índice único parcial `ledger_um_estorno_por_lancamento` está no modelo
    com `sqlite_where` e `postgresql_where`, então a suíte exercita a mesma
    garantia que a produção. Aqui o insert é montado à mão justamente para
    passar por fora de `estornar_lancamento`.
    """

    admin = users[Role.ADMIN]
    aluno = users[Role.STUDENT]
    original = create_points_adjustment(
        session,
        actor=admin,
        student_id=aluno.id,
        points=25,
        reason="Redação extra.",
    )
    estornar_lancamento(
        session,
        actor=admin,
        transaction_id=original.id,
        reason="Estorno: a redação era de outra turma.",
    )
    session.commit()

    session.add(
        LedgerTransaction(
            student_id=aluno.id,
            points=-25,
            kind=LedgerKind.ADJUSTMENT,
            source_type="adjustment_reversal",
            source_key="adjustment_reversal:duplicado",
            description="Estorno por fora do serviço",
            reason="Estorno duplicado.",
            reverses_id=original.id,
            created_by_id=admin.id,
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_estorno_de_estorno_e_recusado(session, users) -> None:
    admin = users[Role.ADMIN]
    aluno = users[Role.STUDENT]
    original = create_points_adjustment(
        session,
        actor=admin,
        student_id=aluno.id,
        points=25,
        reason="Redação extra.",
    )
    estorno = estornar_lancamento(
        session,
        actor=admin,
        transaction_id=original.id,
        reason="Estorno: a redação era de outra turma.",
    )
    session.commit()

    with pytest.raises(ValueError, match="estorno"):
        estornar_lancamento(
            session,
            actor=admin,
            transaction_id=estorno.id,
            reason="Devolvendo os pontos.",
        )
    session.rollback()

    assert student_total(session, aluno.id) == 0


def test_so_lancamento_manual_pode_ser_estornado(session, users) -> None:
    """Pontos de envio aprovado se corrigem revendo o envio, não estornando.

    Estornar uma aprovação deixaria o envio aprovado e os pontos ausentes:
    duas verdades contraditórias no mesmo histórico.
    """

    admin = users[Role.ADMIN]
    aluno = users[Role.STUDENT]
    atividade = session.scalars(select(Activity)).first()
    ganho = LedgerTransaction(
        student_id=aluno.id,
        points=10,
        kind=LedgerKind.DIRECT_ACTIVITY,
        source_type="submission",
        source_key="submission:qualquer",
        activity_id=atividade.id,
        description="Atividade aprovada",
        created_by_id=admin.id,
    )
    session.add(ganho)
    session.flush()

    with pytest.raises(ValueError, match="lançamentos manuais"):
        estornar_lancamento(
            session,
            actor=admin,
            transaction_id=ganho.id,
            reason="Estorno indevido.",
        )


def test_estorno_recusa_motivo_em_branco(session, users) -> None:
    admin = users[Role.ADMIN]
    aluno = users[Role.STUDENT]
    original = create_points_adjustment(
        session,
        actor=admin,
        student_id=aluno.id,
        points=25,
        reason="Redação extra.",
    )
    session.commit()

    with pytest.raises(ValueError, match="motivo"):
        estornar_lancamento(
            session,
            actor=admin,
            transaction_id=original.id,
            reason="   ",
        )
    session.rollback()

    assert student_total(session, aluno.id) == 25


def test_lista_marca_o_que_ainda_pode_ser_estornado(session, users) -> None:
    admin = users[Role.ADMIN]
    aluno = users[Role.STUDENT]
    intacto = create_points_adjustment(
        session,
        actor=admin,
        student_id=aluno.id,
        points=10,
        reason="Bônus de participação.",
    )
    errado = create_points_adjustment(
        session,
        actor=admin,
        student_id=aluno.id,
        points=50,
        reason="Errei o valor aqui.",
    )
    estorno = estornar_lancamento(
        session,
        actor=admin,
        transaction_id=errado.id,
        reason="Estorno: eram 5 pontos, não 50.",
    )
    session.commit()

    por_id = {
        item.id: item
        for item in lancamentos_manuais(session, actor=admin, student_id=aluno.id)
    }
    assert len(por_id) == 3
    assert por_id[intacto.id].estornavel
    # Já estornado: a tela não pode oferecer o botão de novo.
    assert not por_id[errado.id].estornavel
    assert por_id[errado.id].estornado
    assert por_id[errado.id].estornado_por_id == estorno.id
    # O estorno em si também não é estornável.
    assert not por_id[estorno.id].estornavel
    assert por_id[estorno.id].e_estorno


def test_o_aluno_le_os_proprios_lancamentos_e_nao_os_de_outro(
    session, users
) -> None:
    """O motivo é escrito para o aluno: ele precisa conseguir lê-lo.

    E só o dele. `lancamentos_manuais` serve às duas telas, então a checagem de
    quem pode ver mora no serviço, não na tela.
    """

    from english_leaderboard.authz import AuthorizationError
    from english_leaderboard.schema import User, new_id

    admin = users[Role.ADMIN]
    aluno = users[Role.STUDENT]
    outro = User(
        id=new_id(),
        username="outro.aluno",
        display_name="Outro Aluno",
        role=Role.STUDENT,
    )
    session.add(outro)
    create_points_adjustment(
        session,
        actor=admin,
        student_id=aluno.id,
        points=10,
        reason="Participação na conversação.",
    )
    session.commit()

    meus = lancamentos_manuais(session, actor=aluno, student_id=aluno.id)
    assert [item.reason for item in meus] == ["Participação na conversação."]

    with pytest.raises(AuthorizationError):
        lancamentos_manuais(session, actor=aluno, student_id=outro.id)
