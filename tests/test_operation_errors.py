"""Cada falha precisa dizer ao usuário o que fazer a seguir.

`StorageBudgetExceeded` e `StorageError` são `RuntimeError` e caíam no genérico
"a operação falhou, consulte o log". Para as duas essa é a resposta errada:
cota esgotada é informação sobre o sistema, não defeito a investigar, e Storage
fora do ar não é erro do que a pessoa digitou.
"""

from __future__ import annotations

import pytest

import streamlit_app
from english_leaderboard.authz import AuthorizationError
from english_leaderboard.storage import StorageError
from english_leaderboard.storage_budget import StorageBudgetExceeded


class StColetor:
    def __init__(self) -> None:
        self.erros: list[str] = []

    def error(self, texto: str, **_: object) -> None:
        self.erros.append(texto)


@pytest.fixture
def coletor(monkeypatch):
    falso = StColetor()
    monkeypatch.setattr(streamlit_app, "st", falso)
    return falso


def test_cota_estourada_diz_que_o_espaco_acabou(coletor) -> None:
    """A mensagem original traz os números do período e tem de sobreviver."""

    erro = StorageBudgetExceeded(
        "Download recusado: o egress do mês (2048.0 MB) atingiu o limite de "
        "2048 MB. Nada foi transferido."
    )

    streamlit_app.show_operation_error("baixar_arquivo", erro)

    (mensagem,) = coletor.erros
    assert "egress do mês" in mensagem
    assert "2048" in mensagem
    assert "mentores" in mensagem
    assert "referência" not in mensagem.lower(), (
        "cota esgotada não é um defeito a investigar no log"
    )


def test_storage_fora_do_ar_nao_culpa_o_que_o_usuario_digitou(coletor) -> None:
    erro = StorageError("Falha ao assinar students/x/uploads/y.jpg: timeout")

    streamlit_app.show_operation_error("assinar_arquivo", erro)

    (mensagem,) = coletor.erros
    assert "armazenamento de arquivos não respondeu" in mensagem
    assert "Referência" in mensagem
    assert "timeout" not in mensagem, "o detalhe técnico fica no log, não na tela"


def test_as_duas_falhas_de_storage_nao_dizem_a_mesma_coisa(coletor) -> None:
    """São situações diferentes para quem está usando."""

    streamlit_app.show_operation_error(
        "ctx", StorageBudgetExceeded("Download recusado: limite atingido.")
    )
    streamlit_app.show_operation_error("ctx", StorageError("indisponível"))

    orcamento, indisponivel = coletor.erros
    assert orcamento != indisponivel


def test_erros_de_dominio_continuam_aparecendo_na_integra(coletor) -> None:
    streamlit_app.show_operation_error("ctx", ValueError("Atividade inativa"))
    streamlit_app.show_operation_error("ctx", LookupError("Arquivo não encontrado"))
    streamlit_app.show_operation_error("ctx", AuthorizationError("Acesso negado"))

    assert coletor.erros == [
        "Atividade inativa",
        "Arquivo não encontrado",
        "Acesso negado",
    ]


def test_o_inesperado_continua_indo_para_o_log_com_referencia(coletor) -> None:
    streamlit_app.show_operation_error("ctx", RuntimeError("estouro interno"))

    (mensagem,) = coletor.erros
    assert "A operação falhou" in mensagem
    assert "estouro interno" not in mensagem
