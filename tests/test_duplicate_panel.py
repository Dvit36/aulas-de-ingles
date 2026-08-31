"""O painel antifraude precisa, antes de tudo, ser alcançável.

A consulta que alimenta a comparação de evidências filtrava por
`DuplicateMatch.image_id`. Essa coluna deixou de existir quando
`submission_images` e os documentos viraram a tabela única `submission_files`:
o campo passou a se chamar `file_id`. Como a chamada ficou para trás, abrir o
card de qualquer submissão com imagem no modo administrador levantava
AttributeError — e a exceção nascia acima do painel, então nem a comparação nem
o formulário de aprovar/rejeitar logo abaixo chegavam a renderizar.

Nada cobria esse caminho: os testes de serviço exercitam a *criação* de
`DuplicateMatch`, nunca a leitura feita pela interface. Por isso a consulta vive
em `_duplicate_matches`, e é ela que este módulo chama — um teste que montasse a
própria query provaria apenas que o SQLAlchemy funciona.
"""

from __future__ import annotations

import pytest
from conftest import FakeDuolingoOCR, make_png
from sqlalchemy import select

import streamlit_app
from english_leaderboard.schema import (
    Activity,
    DuplicateMatch,
    Role,
    Submission,
    SubmissionStatus,
    User,
    new_id,
)
from english_leaderboard.services import UploadPayload, submit_evidence


@pytest.fixture
def duplicidade(session, users, settings, gateway):
    """Dois alunos, a mesma imagem: a suspeita que o revisor precisa comparar.

    Devolve a submissão do segundo aluno — a que o administrador abre — junto
    do intruso, porque o arquivo correspondente pertence a outro dono e é isso
    que torna a autorização da assinatura interessante.
    """

    activity = session.scalar(
        select(Activity).where(Activity.code == "duolingo_beconfident")
    )
    outro = User(
        id=new_id(),
        username="outro",
        display_name="Outro",
        role=Role.STUDENT,
        active=True,
    )
    session.add(outro)
    session.commit()

    imagem = make_png(15)
    submit_evidence(
        session,
        gateway=gateway,
        actor=users[Role.STUDENT],
        activity_id=activity.id,
        uploads=[UploadPayload("original.png", imagem)],
        settings=settings,
        ocr_engine=FakeDuolingoOCR(),
    )
    session.commit()
    resultado = submit_evidence(
        session,
        gateway=gateway,
        actor=outro,
        activity_id=activity.id,
        uploads=[UploadPayload("copia.png", imagem)],
        settings=settings,
        ocr_engine=FakeDuolingoOCR(),
    )
    session.commit()

    submission = session.get(Submission, resultado.submission_id)
    assert submission.status == SubmissionStatus.REJECTED
    return submission, outro


def test_a_consulta_do_painel_nao_usa_coluna_que_nao_existe(session, duplicidade):
    """A regressão que este módulo existe para impedir.

    Se a consulta voltar a filtrar por `image_id` — ou por qualquer atributo
    ausente de `DuplicateMatch` —, isto levanta AttributeError, exatamente como
    a interface levantava em produção.
    """

    submission, _ = duplicidade

    matches = streamlit_app._duplicate_matches(session, submission)

    assert matches, "a suspeita gravada tem de chegar ao painel"
    assert all(isinstance(match, DuplicateMatch) for match in matches)


def test_o_painel_so_recebe_as_suspeitas_da_submissao_aberta(session, duplicidade):
    """`file_id` é o lado atual da comparação; `matched_file_id` é o antigo.

    Filtrar pelo campo errado traria a suspeita do envio original para dentro
    do card do reenvio, invertendo as duas colunas da comparação.
    """

    submission, _ = duplicidade
    ids_do_envio = {arquivo.id for arquivo in submission.images}

    matches = streamlit_app._duplicate_matches(session, submission)

    assert {match.file_id for match in matches} <= ids_do_envio
    assert all(match.matched_file_id not in ids_do_envio for match in matches)


def test_submissao_sem_imagem_nao_consulta_duplicidade(session, users, settings, gateway):
    """Sem imagem não há o que comparar, e a consulta nem precisa sair."""

    activity = session.scalar(
        select(Activity).where(Activity.code == "impact_summary")
    )
    resultado = submit_evidence(
        session,
        gateway=gateway,
        actor=users[Role.STUDENT],
        activity_id=activity.id,
        uploads=[UploadPayload("nota.txt", b"conteudo textual seguro")],
        settings=settings,
        title="Atividade Impact",
        summary=(
            "Eu aprendi novas palavras e revisei exemplos importantes para a "
            "equipe. Também escrevi anotações em português para praticar o "
            "conteúdo depois e compartilhar o aprendizado com meus colegas."
        ),
    )
    session.commit()
    submission = session.get(Submission, resultado.submission_id)

    assert streamlit_app._duplicate_matches(session, submission) == []
