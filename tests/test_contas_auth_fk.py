"""Perfil e conta do Auth nascem juntos, ou não nascem.

``profiles.id`` referencia ``auth.users(id)``. Gravar um perfil com um UUID
inventado passa em SQLite, onde não existe schema ``auth``, e viola a chave
estrangeira no PostgreSQL do Supabase — que é justamente onde produção roda.
Era por isso que a suíte inteira ficava verde sobre um startup quebrado.

Aqui os dois bancos são cobertos: o gate que os distingue é testado de verdade,
a consulta a ``auth.users`` roda contra um schema ``auth`` anexado ao SQLite, e
os caminhos que só o PostgreSQL exercita fingem o dialeto — deixando explícito
o que é verificado de fato e o que é simulado.
"""

from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy import select, text

from english_leaderboard import contas as contas_mod
from english_leaderboard.config import Settings
from english_leaderboard.contas import (
    ContaObrigatoria,
    bootstrap_admin,
    conta_existe_no_auth,
    contas_disponiveis,
    exige_conta_no_auth,
)
from english_leaderboard.database import (
    create_database_engine,
    create_session_factory,
    initialize_database,
)
from english_leaderboard.schema import Role, User

BOOTSTRAP = {
    "bootstrap_admin_name": "Administrador",
    "bootstrap_admin_username": "admin.novo",
    "bootstrap_admin_password": "senha-inicial-forte-2026",
}


class ContasFalsas:
    """Dublê da Admin API: devolve um id sem tocar a rede."""

    def __init__(self, identificador: str | None = None) -> None:
        self.identificador = identificador or str(uuid4())
        self.criadas: list[str] = []

    def criar(self, *, username: str, display_name: str) -> tuple[str, str]:
        self.criadas.append(username)
        return self.identificador, "senha-temporaria"

    def redefinir_senha(self, user_id: str) -> str:  # pragma: no cover
        raise AssertionError("não deveria ser chamado")

    def atualizar_username(self, user_id: str, username: str) -> str:  # pragma: no cover
        raise AssertionError("não deveria ser chamado")

    def desativar(self, user_id: str) -> None:  # pragma: no cover
        raise AssertionError("não deveria ser chamado")

    def remover(self, user_id: str) -> None:  # pragma: no cover
        raise AssertionError("não deveria ser chamado")


@pytest.fixture
def banco(settings: Settings):
    """Banco vazio, sem o admin que a fixture ``session`` já semeia.

    ``bootstrap_admin`` sai cedo quando encontra um administrador ativo, então
    o caminho de criação só é exercitado a partir de um banco sem nenhum.
    """

    engine = create_database_engine(settings.database_url)
    initialize_database(engine)
    db = create_session_factory(engine)()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


def _anexar_auth(db) -> None:
    """Anexa um schema ``auth`` ao SQLite, para a consulta rodar de verdade.

    Sem isto a checagem de existência só poderia ser testada com um dublê, e o
    SQL — que é o que quebra em produção quando está errado — nunca seria
    executado.
    """

    db.execute(text("attach database ':memory:' as auth"))
    db.execute(text("create table auth.users (id text primary key)"))
    db.commit()


@pytest.fixture
def com_auth_users(session):
    """A fixture padrão, com um schema ``auth`` anexado."""

    _anexar_auth(session)
    return session


@pytest.fixture
def banco_com_auth(banco):
    """Banco vazio, com um schema ``auth`` anexado."""

    _anexar_auth(banco)
    return banco


def finge_postgres(monkeypatch: pytest.MonkeyPatch) -> None:
    """Faz o gate responder como no Supabase.

    O gate em si é coberto por ``test_o_gate_distingue_os_dois_bancos``; aqui
    só se força o ramo que o SQLite jamais tomaria.
    """

    monkeypatch.setattr(contas_mod, "exige_conta_no_auth", lambda _sessao: True)


def perfis(session) -> list[str]:
    return list(session.scalars(select(User.username)))


# --------------------------------------------------------------------- o gate


def test_o_gate_distingue_os_dois_bancos(session) -> None:
    """SQLite não tem auth.users; o PostgreSQL do Supabase tem."""

    assert exige_conta_no_auth(session) is False

    class BindFalso:
        class dialect:
            name = "postgresql"

    class SessaoFalsa:
        def get_bind(self):
            return BindFalso

    assert exige_conta_no_auth(SessaoFalsa()) is True


def test_a_consulta_ao_auth_users_encontra_e_nao_encontra(com_auth_users) -> None:
    """O SQL da checagem roda contra um schema auth real, não contra um dublê."""

    existente = str(uuid4())
    com_auth_users.execute(
        text("insert into auth.users (id) values (:id)"), {"id": existente}
    )

    assert conta_existe_no_auth(com_auth_users, existente) is True
    assert conta_existe_no_auth(com_auth_users, str(uuid4())) is False


# ---------------------------------------------------------- bootstrap do admin


def test_bootstrap_recusa_perfil_sem_conta_quando_o_banco_exige(
    banco, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sem Auth em PostgreSQL, a recusa vem antes do insert.

    Este é o caminho que derrubava o startup: ``contas=None`` acontece sempre
    que SUPABASE_SECRET_KEY não está definida.
    """

    finge_postgres(monkeypatch)

    with pytest.raises(ContaObrigatoria, match="SUPABASE_SECRET_KEY"):
        bootstrap_admin(banco, replace(settings, **BOOTSTRAP), None)

    assert perfis(banco) == [], "nenhum perfil pode ter sido inserido"


def test_bootstrap_recusa_id_que_o_auth_nao_reconhece(
    banco_com_auth, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """O id volta da Admin API, mas não está em auth.users: nada é gravado.

    Acontece quando SUPABASE_URL e SUPABASE_DB_URL apontam para projetos
    diferentes — o insert violaria a chave estrangeira sem explicar por quê.
    """

    finge_postgres(monkeypatch)

    with pytest.raises(ContaObrigatoria, match="não está em auth.users"):
        bootstrap_admin(
            banco_com_auth, replace(settings, **BOOTSTRAP), ContasFalsas()
        )

    assert perfis(banco_com_auth) == [], "nenhum perfil pode ter sido inserido"


def test_bootstrap_amarra_o_perfil_ao_id_devolvido_pelo_auth(
    banco_com_auth, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Com a conta presente em auth.users, o perfil nasce com o mesmo UUID."""

    finge_postgres(monkeypatch)
    identificador = str(uuid4())
    banco_com_auth.execute(
        text("insert into auth.users (id) values (:id)"), {"id": identificador}
    )
    contas = ContasFalsas(identificador)

    admin = bootstrap_admin(banco_com_auth, replace(settings, **BOOTSTRAP), contas)

    assert contas.criadas == ["admin.novo"]
    assert admin is not None
    assert admin.id == identificador, "o perfil tem de usar o UUID do Auth"
    assert admin.role == Role.ADMIN


def test_bootstrap_sem_auth_continua_valendo_no_sqlite(
    banco, settings: Settings
) -> None:
    """O caminho de desenvolvimento não pode ter sido fechado junto.

    Um perfil sem conta é legítimo onde não existe auth.users: é o que permite
    exercitar catálogo e pontuação sem serviço de autenticação.
    """

    admin = bootstrap_admin(banco, replace(settings, **BOOTSTRAP), None)

    assert admin is not None
    assert admin.username == "admin.novo"


def test_bootstrap_nao_toca_no_auth_quando_ja_existe_administrador(
    session, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fixture já traz um admin ativo: nem conta nova, nem recusa."""

    finge_postgres(monkeypatch)

    admin = bootstrap_admin(session, replace(settings, **BOOTSTRAP), None)

    assert admin is not None and admin.username == "admin"


# ------------------------------------------- escolha da fachada pela config


def test_sem_chave_privilegiada_nao_ha_fachada_do_auth() -> None:
    """Devolver None deixa a recusa para quem tem a mensagem boa.

    ``contas_de`` levantaria um ValueError genérico sobre a chave secreta —
    inclusive em bancos onde nenhuma conta precisaria ser criada.
    """

    parcial = Settings(
        supabase_url="https://ref.supabase.co",
        supabase_publishable_key="sb_publishable_x",
        supabase_db_url=(
            "postgresql+psycopg://postgres.ref:senha"
            "@aws-0-sa-east-1.pooler.supabase.com:5432/postgres"
        ),
    )

    assert parcial.supabase_ready is True
    assert parcial.contas_administraveis is False
    assert contas_disponiveis(parcial) is None


def test_com_chave_privilegiada_a_fachada_e_montada() -> None:
    completa = Settings(
        supabase_url="https://ref.supabase.co",
        supabase_publishable_key="sb_publishable_x",
        supabase_secret_key="sb_secret_x",
        supabase_db_url=(
            "postgresql+psycopg://postgres.ref:senha"
            "@aws-0-sa-east-1.pooler.supabase.com:5432/postgres"
        ),
    )

    assert completa.contas_administraveis is True
    assert contas_disponiveis(completa) is not None
