"""O cache de URL assinada precisa sobreviver à janela curta.

A janela caiu de 90 para 30 segundos para cortar a subconta do egress: o
débito é feito na assinatura, e cada busca extra do navegador dentro da janela
é egress que o contador não vê. Mas o Streamlit reroda a cada clique, e o
cache só reaproveita a URL enquanto ela tiver `MARGEM_URL_SEGUNDOS` de folga.
Fora disso, cada rerun reassina — e redebita o arquivo inteiro.

Com a margem antiga (15 s) e a janela nova (30 s), a URL ficaria
reaproveitável por só metade da vida dela, e um aluno navegando reassinaria
até cinco vezes mais que antes. A janela curta, feita para cortar a subconta,
pioraria a superconta. Estes testes travam isso.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

import streamlit_app
from english_leaderboard.storage import URL_EXPIRA_SEGUNDOS


class _Relogio:
    def __init__(self) -> None:
        self.agora = datetime(2026, 9, 10, 12, 0, 0, tzinfo=UTC)

    def avancar(self, segundos: float) -> None:
        self.agora += timedelta(seconds=segundos)


@pytest.fixture
def assinador(monkeypatch):
    relogio = _Relogio()
    assinaturas: list[str] = []

    class DatetimeControlado(datetime):
        @classmethod
        def now(cls, tz=None):
            return relogio.agora

    class StFalso:
        # Por instância: um dict no corpo da classe seria o mesmo objeto para
        # todo `StFalso`, e o cache de um teste vazaria para o seguinte.
        def __init__(self) -> None:
            self.session_state: dict = {}

    monkeypatch.setattr(streamlit_app, "datetime", DatetimeControlado)
    monkeypatch.setattr(streamlit_app, "st", StFalso())
    monkeypatch.setattr(streamlit_app, "_storage_gateway", lambda _s: None)

    def assinar(_session, *, actor, file_id, settings, gateway):
        assinaturas.append(file_id)
        return None, f"https://exemplo.invalid/{file_id}?n={len(assinaturas)}"

    monkeypatch.setattr(streamlit_app, "get_submission_file_url_for_user", assinar)

    class Arquivo:
        id = "arquivo-1"

    def pedir():
        return streamlit_app._url_assinada(None, None, Arquivo(), None)

    pedir.relogio = relogio
    pedir.assinaturas = assinaturas
    return pedir


def test_reruns_dentro_da_folga_reaproveitam_a_url(assinador) -> None:
    """O caso comum: o aluno clica várias vezes numa página aberta."""

    primeira = assinador()
    for _ in range(5):
        assinador.relogio.avancar(4)  # 20 s de cliques
        assert assinador() == primeira

    assert len(assinador.assinaturas) == 1, (
        "reruns dentro da folga reassinaram: cada um redebitaria o arquivo"
    )


def test_a_url_so_e_reassinada_quando_perde_a_folga(assinador) -> None:
    folga = URL_EXPIRA_SEGUNDOS - streamlit_app.MARGEM_URL_SEGUNDOS

    assinador()
    assinador.relogio.avancar(folga - 1)
    assinador()
    assert len(assinador.assinaturas) == 1

    assinador.relogio.avancar(2)
    assinador()
    assert len(assinador.assinaturas) == 2


def test_a_margem_nao_come_a_janela(assinador) -> None:
    """A invariante que a janela curta ameaçava, travada como número.

    Reaproveitável por pelo menos 80% da vida da URL. Antes da mudança eram 75
    de 90 (83%); com a margem ajustada, 25 de 30 (83%). Com a margem antiga e
    a janela nova, seriam 15 de 30 (50%) — e é isso que este teste recusa.
    """

    folga = URL_EXPIRA_SEGUNDOS - streamlit_app.MARGEM_URL_SEGUNDOS
    assert folga / URL_EXPIRA_SEGUNDOS >= 0.8, (
        f"a URL fica reaproveitável por só {folga} de {URL_EXPIRA_SEGUNDOS} s: "
        "o cache vai reassinar a cada rerun e a superconta de egress sobe"
    )
