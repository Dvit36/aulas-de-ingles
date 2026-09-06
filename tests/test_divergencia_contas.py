"""O detector de perfil fora de sincronia com a conta no Auth.

Os dois defeitos que motivaram este módulo não levantavam exceção e não
apareciam em teste nenhum: o perfil mudava, o Auth não, e o sintoma era o
aluno não conseguir entrar. A regra fica separada da consulta justamente para
caber aqui, sem PostgreSQL.
"""

from __future__ import annotations

from datetime import UTC, datetime

from english_leaderboard.contas import (
    classificar_divergencias,
    divergencias_de_conta,
)

DOMINIO = "robonaticos7565.invalid"
AGORA = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
FUTURO = datetime(2126, 1, 1, tzinfo=UTC)
PASSADO = datetime(2026, 1, 1, tzinfo=UTC)


def _classificar(linhas):
    return classificar_divergencias(linhas, dominio=DOMINIO, agora=AGORA)


def test_a_renamed_profile_whose_account_stayed_behind_is_reported() -> None:
    """O caso real: o perfil virou `luiz.brito`, o login seguia em `luiz`."""

    achados = _classificar(
        [("id-1", "luiz.brito", True, f"luiz@{DOMINIO}", None)]
    )

    assert [a.tipo for a in achados] == ["endereco"]
    # A mensagem precisa dizer com o que dá para entrar, não só que divergiu.
    assert f"luiz@{DOMINIO}" in achados[0].detalhe
    assert "luiz.brito" in achados[0].detalhe


def test_an_active_profile_over_a_banned_account_is_reported() -> None:
    """Reativar sem levantar o ban tranca o aluno fora sem sintoma.

    É o pior dos dois: a tela mostra ativo e não há nome antigo com que
    entrar.
    """

    achados = _classificar(
        [("id-2", "ana.silva", True, f"ana.silva@{DOMINIO}", FUTURO)]
    )

    assert [a.tipo for a in achados] == ["acesso_bloqueado"]


def test_an_expired_ban_blocks_nobody_and_is_not_reported() -> None:
    """`banned_until` no passado não recusa login: avisar seria ruído."""

    assert _classificar(
        [("id-3", "bia.costa", True, f"bia.costa@{DOMINIO}", PASSADO)]
    ) == []


def test_an_inactive_profile_over_a_banned_account_is_coherent() -> None:
    """Inativo e banido é o par funcionando, não uma divergência."""

    assert _classificar(
        [("id-4", "caio.dias", False, f"caio.dias@{DOMINIO}", FUTURO)]
    ) == []


def test_a_profile_without_any_account_is_reported() -> None:
    """`left join` sem par: perfil que não tem como entrar de jeito nenhum."""

    achados = _classificar([("id-5", "sem.conta", True, None, None)])

    assert [a.tipo for a in achados] == ["sem_conta"]


def test_a_matching_pair_is_not_reported() -> None:
    assert _classificar(
        [("id-6", "enzo.souza", True, f"enzo.souza@{DOMINIO}", None)]
    ) == []


def test_both_defects_on_the_same_account_are_reported_separately() -> None:
    """Renomeado e banido são problemas distintos, com correções distintas."""

    achados = _classificar(
        [("id-7", "novo.nome", True, f"antigo@{DOMINIO}", FUTURO)]
    )

    assert sorted(a.tipo for a in achados) == ["acesso_bloqueado", "endereco"]


def test_without_the_auth_schema_the_check_stays_quiet(session) -> None:
    """Em SQLite não existe `auth.users`, e não há o que comparar.

    A alternativa seria a consulta estourar em desenvolvimento e na suíte.
    """

    assert divergencias_de_conta(session, dominio=DOMINIO) == []
