"""Configuração dos serviços oficiais do Supabase: Auth, PostgreSQL e Storage.

Sem rede: só carregamento, validação e as garantias de que a chave privilegiada
não escapa para caminho de aluno.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from english_leaderboard.config import Settings

CHAVES = (
    "SUPABASE_URL",
    "SUPABASE_PUBLISHABLE_KEY",
    "SUPABASE_SECRET_KEY",
    "SUPABASE_DB_URL",
    "SUPABASE_USERNAME_DOMAIN",
    "STORAGE_BUCKET",
)

POOLER = (
    "postgresql+psycopg://postgres.ref:senha"
    "@aws-0-sa-east-1.pooler.supabase.com:5432/postgres"
)


def producao(**extras) -> Settings:
    """Configuração de produção completa, para isolar a regra sob teste.

    Produção exige o Supabase inteiro; sem isso qualquer validação específica
    ficaria escondida atrás do erro de configuração incompleta.
    """

    base = {
        "app_env": "production",
        "supabase_url": "https://ref.supabase.co",
        "supabase_publishable_key": "sb_publishable_x",
        "supabase_db_url": POOLER,
    }
    return Settings(**{**base, **extras})


@pytest.fixture
def limpo(monkeypatch):
    for chave in CHAVES:
        monkeypatch.delenv(chave, raising=False)
    return monkeypatch


def test_settings_load_supabase_from_root_level_env(limpo) -> None:
    """São chaves planas de propósito: só a raiz vira env var no Streamlit."""

    limpo.setenv("SUPABASE_URL", "https://abc.supabase.co/")
    limpo.setenv("SUPABASE_PUBLISHABLE_KEY", "sb_publishable_xyz")
    limpo.setenv("SUPABASE_SECRET_KEY", "sb_secret_xyz")
    limpo.setenv("SUPABASE_DB_URL", POOLER)
    limpo.setenv("SUPABASE_USERNAME_DOMAIN", "Equipe.Invalid")
    limpo.setenv("STORAGE_BUCKET", "student-files")

    settings = Settings.from_env(env_file=None)

    assert settings.supabase_url == "https://abc.supabase.co"  # sem barra final
    assert settings.supabase_username_domain == "equipe.invalid"  # normalizado
    assert settings.storage_bucket == "student-files"
    assert settings.supabase_ready is True
    # O Storage compartilha as credenciais do Supabase: basta o bucket.
    assert settings.storage_ready is True


def test_missing_configuration_is_reported_as_not_ready(limpo) -> None:
    settings = Settings.from_env(env_file=None)

    assert settings.supabase_ready is False
    assert settings.storage_ready is False
    # Ausência não é erro: o legado ainda roda enquanto a migração acontece.
    settings.validate()


def test_swapped_supabase_keys_are_refused() -> None:
    """Trocar as duas chaves de lugar vazaria a privilegiada para o cliente."""

    with pytest.raises(ValueError, match="prefixo sb_publishable_"):
        Settings(supabase_publishable_key="sb_secret_errado").validate()

    with pytest.raises(ValueError, match="prefixo sb_secret_"):
        Settings(supabase_secret_key="sb_publishable_errado").validate()


def test_direct_ipv6_connection_is_refused_in_production() -> None:
    """O Streamlit Cloud é IPv4; a conexão direta do Supabase é IPv6."""

    direta = "postgresql+psycopg://postgres:senha@db.ref.supabase.co:5432/postgres"

    with pytest.raises(ValueError, match="Session pooler"):
        producao(supabase_db_url=direta).validate()

    # Fora de produção a direta é aceita: útil em máquina com IPv6.
    Settings(app_env="development", supabase_db_url=direta).validate()
    # O pooler passa em produção.
    producao().validate()


def test_production_requires_the_whole_supabase_configuration() -> None:
    """Faltando qualquer peça, a aplicação não sobe em produção.

    Identidade, dados e arquivos vivem no Supabase: subir sem a configuração
    completa só adiaria a falha para a primeira tela do aluno.
    """

    for ausente in ("supabase_url", "supabase_publishable_key", "supabase_db_url"):
        with pytest.raises(RuntimeError, match="obrigatório configurar"):
            producao(**{ausente: ""}).validate()


def test_non_postgres_database_url_is_refused() -> None:
    with pytest.raises(ValueError, match="PostgreSQL"):
        Settings(supabase_db_url="sqlite:///./data/app.db").validate()


def test_username_domain_must_be_a_bare_domain() -> None:
    with pytest.raises(ValueError, match="só o domínio"):
        Settings(supabase_username_domain="aluno@equipe.invalid").validate()


def test_admin_secret_key_is_explicit_and_fails_loudly_when_absent() -> None:
    """O acesso é por método para todo uso da chave privilegiada ser rastreável."""

    configurado = Settings(supabase_secret_key="sb_secret_xyz")
    assert configurado.admin_secret_key() == "sb_secret_xyz"

    with pytest.raises(ValueError, match="SUPABASE_SECRET_KEY não configurada"):
        Settings().admin_secret_key()


def test_privileged_key_is_never_read_outside_administrative_code() -> None:
    """Guarda de arquitetura: a chave que ignora RLS não pode vazar.

    Se um caminho de aluno passar a lê-la, este teste falha e obriga a decisão
    a ser consciente em vez de acidental.
    """

    raiz = Path(__file__).resolve().parent.parent
    permitidos = {
        "english_leaderboard/config.py",  # define e expõe pelo método
        "tests/test_supabase_config.py",
    }
    ofensores = []
    for caminho in [*raiz.glob("*.py"), *(raiz / "english_leaderboard").glob("*.py")]:
        relativo = caminho.relative_to(raiz).as_posix()
        if relativo in permitidos:
            continue
        texto = caminho.read_text(encoding="utf-8")
        if "supabase_secret_key" in texto:
            ofensores.append(relativo)

    assert ofensores == [], (
        f"Uso direto de supabase_secret_key fora do config: {ofensores}. "
        "Use Settings.admin_secret_key() em código administrativo."
    )


def test_secrets_example_has_no_real_values() -> None:
    raiz = Path(__file__).resolve().parent.parent
    exemplo = (raiz / ".streamlit" / "secrets.toml.example").read_text(encoding="utf-8")

    for chave in ("SUPABASE_URL", "SUPABASE_PUBLISHABLE_KEY", "SUPABASE_DB_URL"):
        assert chave in exemplo
    # Placeholders, nunca credenciais de verdade.
    assert "sb_publishable_SUA_CHAVE" in exemplo
    assert "sb_secret_SUA_CHAVE" in exemplo
    assert ".supabase.co" in exemplo
    assert "SEU-PROJETO" in exemplo


def test_settings_can_be_replaced_for_tests() -> None:
    base = Settings()
    ajustado = replace(base, supabase_url="https://x.supabase.co")
    assert ajustado.supabase_url.endswith(".supabase.co")
    assert base.supabase_url == ""
