"""Listagem paginada de submissões, e o que a paginação não pode quebrar.

A listagem carregava tudo o que os filtros alcançassem, sem teto. Com a página,
duas coisas passam a poder dar errado em silêncio: o total discordar das
páginas, e um filtro aplicado em Python depois do corte esvaziar páginas
inteiras. As duas têm teste aqui.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from english_leaderboard.authz import AuthorizationError
from english_leaderboard.schema import (
    Activity,
    Role,
    Submission,
    SubmissionStatus,
    User,
    new_id,
    utcnow,
)
from english_leaderboard.services import (
    PAGINA_PADRAO,
    count_submissions,
    list_submissions,
)


@pytest.fixture
def muitos_envios(session, users):
    """Mais submissões do que cabem numa página, com status variados."""

    activity = session.scalar(
        select(Activity).where(Activity.code == "impact_summary")
    )
    aluno = users[Role.STUDENT]
    base = utcnow()
    criadas = []
    for indice in range(PAGINA_PADRAO + 7):
        submission = Submission(
            student_id=aluno.id,
            activity_id=activity.id,
            declared_units=1,
            status=(
                SubmissionStatus.NEEDS_REVIEW
                if indice % 3 == 0
                else SubmissionStatus.REJECTED
            ),
            received_at=base - timedelta(minutes=indice),
            rule_snapshot_json={},
        )
        session.add(submission)
        criadas.append(submission)
    session.commit()
    return criadas


def test_a_pagina_tem_teto_e_o_total_conta_tudo(muitos_envios, session, users) -> None:
    aluno = users[Role.STUDENT]
    total = count_submissions(session, actor=aluno)

    assert total == PAGINA_PADRAO + 7
    assert len(list_submissions(session, actor=aluno)) == PAGINA_PADRAO


def test_as_paginas_cobrem_o_conjunto_sem_repetir(
    muitos_envios, session, users
) -> None:
    """Offset e ordenação juntos: nenhuma submissão sumida, nenhuma duas vezes."""

    aluno = users[Role.STUDENT]
    total = count_submissions(session, actor=aluno)

    vistos: list[str] = []
    offset = 0
    while offset < total:
        pagina = list_submissions(session, actor=aluno, offset=offset)
        vistos.extend(item.id for item in pagina)
        offset += PAGINA_PADRAO

    assert len(vistos) == total
    assert len(set(vistos)) == total, "a mesma submissão apareceu em duas páginas"


def test_a_ordem_continua_do_mais_recente_para_o_mais_antigo(
    muitos_envios, session, users
) -> None:
    aluno = users[Role.STUDENT]
    primeira = list_submissions(session, actor=aluno)
    segunda = list_submissions(session, actor=aluno, offset=PAGINA_PADRAO)

    recebidos = [item.received_at for item in primeira + segunda]
    assert recebidos == sorted(recebidos, reverse=True)


def test_o_recorte_por_status_acontece_na_consulta(
    muitos_envios, session, users
) -> None:
    """Filtrar em Python depois de paginar esvaziaria páginas inteiras.

    A fila de revisão pega os pendentes: se o corte viesse depois do limite, a
    primeira página poderia não trazer nenhum.
    """

    aluno = users[Role.STUDENT]
    pendentes = {SubmissionStatus.NEEDS_REVIEW}

    total = count_submissions(session, actor=aluno, statuses=pendentes)
    pagina = list_submissions(session, actor=aluno, statuses=pendentes)

    assert total > 0
    assert len(pagina) == min(total, PAGINA_PADRAO)
    assert all(item.status == SubmissionStatus.NEEDS_REVIEW for item in pagina)


def test_contagem_e_listagem_enxergam_os_mesmos_filtros(
    muitos_envios, session, users
) -> None:
    """Se as duas divergirem, o rodapé promete páginas que não existem."""

    aluno = users[Role.STUDENT]
    for statuses in (
        {SubmissionStatus.NEEDS_REVIEW},
        {SubmissionStatus.REJECTED},
        {SubmissionStatus.NEEDS_REVIEW, SubmissionStatus.REJECTED},
    ):
        total = count_submissions(session, actor=aluno, statuses=statuses)
        todos = list_submissions(session, actor=aluno, statuses=statuses, limit=None)
        assert total == len(todos)


def test_colecao_de_status_vazia_nao_seleciona_nada(
    muitos_envios, session, users
) -> None:
    """Coleção vazia é um filtro, não a ausência de filtro."""

    aluno = users[Role.STUDENT]

    assert count_submissions(session, actor=aluno, statuses=set()) == 0
    assert list_submissions(session, actor=aluno, statuses=set()) == []


def test_a_visao_todos_da_fila_nao_filtra_nada(muitos_envios, session, users) -> None:
    """Armadilha criada pela semântica de coleção vazia.

    `statuses=set()` passou a significar "nenhum status serve". Se
    `_review_statuses("all")` devolvesse um conjunto vazio como sentinela de
    "sem filtro", a aba **Todos os envios** ficaria permanentemente vazia — e
    nada além deste teste avisaria.
    """

    import streamlit_app

    todos = streamlit_app._review_statuses("all")
    assert todos, "'all' precisa listar os status, não ser um conjunto vazio"

    aluno = users[Role.STUDENT]
    assert count_submissions(session, actor=aluno, statuses=todos) == count_submissions(
        session, actor=aluno
    )


def test_limite_invalido_e_recusado(session, users) -> None:
    aluno = users[Role.STUDENT]

    with pytest.raises(ValueError, match="limit"):
        list_submissions(session, actor=aluno, limit=0)
    with pytest.raises(ValueError, match="offset"):
        list_submissions(session, actor=aluno, offset=-1)


def test_a_paginacao_nao_afrouxa_a_autorizacao(muitos_envios, session, users) -> None:
    """O aluno continua preso ao próprio histórico, com ou sem página."""

    outro = User(
        id=new_id(),
        username="outro-aluno",
        display_name="Outro",
        role=Role.STUDENT,
        active=True,
    )
    session.add(outro)
    session.commit()

    aluno = users[Role.STUDENT]
    with pytest.raises(AuthorizationError):
        list_submissions(session, actor=outro, student_id=aluno.id)
    with pytest.raises(AuthorizationError):
        count_submissions(session, actor=outro, student_id=aluno.id)

    # Sem pedir aluno nenhum, cada um vê só o que é seu.
    assert count_submissions(session, actor=outro) == 0
    assert count_submissions(session, actor=aluno) == PAGINA_PADRAO + 7
