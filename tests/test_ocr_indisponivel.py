"""Um motor de OCR indisponível não pode custar a prova do aluno.

`create_ocr_engine` levanta `OCRUnavailableError` quando o RapidOCR não está
instalado **e** quando ele não inicializa — modelo ONNX corrompido, memória
insuficiente, extra do deploy que não subiu junto. A segunda é a de produção.

Nenhuma das duas era tratada: o erro subia até `submit_evidence` e recusava o
envio inteiro. Bastava o motor falhar no servidor para nenhum envio de imagem
passar, e o aluno via a prova recusada por algo que não era dele.

O irmão `OCRExecutionError` — motor que existe e falha numa imagem — já era
tratado, e continua sendo. São coisas diferentes e os dois testes convivem.
"""

from __future__ import annotations

import pytest
from conftest import GatewayFalso, make_png
from sqlalchemy import select

from english_leaderboard import ocr as ocr_modulo
from english_leaderboard.ocr import OCRUnavailableError, motor_opcional
from english_leaderboard.schema import Activity, Role, Submission, SubmissionStatus
from english_leaderboard.services import UploadPayload, submit_evidence


@pytest.fixture
def motor_que_nao_inicializa(monkeypatch):
    """O caso de produção: o pacote existe e o RapidOCR não sobe."""

    def falha(*_a, **_k):
        raise OCRUnavailableError("Could not initialize the local OCR engine.")

    monkeypatch.setattr("english_leaderboard.ocr.create_ocr_engine", falha)


def test_the_helper_answers_none_instead_of_raising(motor_que_nao_inicializa) -> None:
    assert motor_opcional() is None


def test_whoever_asks_for_the_engine_itself_still_gets_the_error(
    motor_que_nao_inicializa,
) -> None:
    """A tolerância é decisão de quem chama, não silêncio embutido.

    A CLI e as ferramentas devem seguir falhando alto: para elas, motor ausente
    é motivo de parar, não de continuar sem texto.
    """

    # Pelo módulo, não por um nome importado no topo: o monkeypatch troca o
    # atributo do módulo, e uma referência ligada no import escaparia dele.
    with pytest.raises(OCRUnavailableError):
        ocr_modulo.create_ocr_engine()


def test_an_image_submission_survives_an_engine_that_will_not_start(
    session, users, settings, motor_que_nao_inicializa
) -> None:
    """O defeito que este arquivo existe para não deixar voltar.

    Antes: `OCRUnavailableError` subia de `submit_evidence` e a submissão era
    recusada. Agora ela acontece, o arquivo chega ao bucket, o texto sai vazio
    e a decisão vai para a revisão humana — que é o lugar certo quando a
    máquina não conseguiu ler.
    """

    gateway = GatewayFalso()
    atividade = session.scalars(
        select(Activity).where(Activity.code == "duolingo_beconfident")
    ).first()

    resultado = submit_evidence(
        session,
        gateway=gateway,
        actor=users[Role.STUDENT],
        activity_id=atividade.id,
        uploads=[UploadPayload("print.png", make_png(seed=7))],
        settings=settings,
        ocr_engine=None,
    )
    session.commit()

    assert resultado.submission_id is not None
    assert gateway.objetos, "a prova precisa chegar ao bucket mesmo sem OCR"

    submissao = session.get(Submission, resultado.submission_id)
    assert submissao.ocr_text == ""
    # Sem texto não há como confirmar plataforma nem contar unidades: quem
    # decide passa a ser o administrador, e não a aprovação automática.
    assert resultado.status == SubmissionStatus.NEEDS_REVIEW
    assert resultado.recognized_units == 0
