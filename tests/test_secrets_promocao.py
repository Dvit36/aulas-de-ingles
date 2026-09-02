"""Booleano no secrets.toml não vira variável de ambiente — e o default sabe.

A aplicação lê configuração por `os.environ`. Num deploy do Streamlit Cloud
quem preenche `os.environ` é o próprio Streamlit, a partir do `secrets.toml`, e
ele promove apenas valores `str`, `int` e `float`
(`streamlit/runtime/secrets.py`, `_maybe_set_environment_variable`; `bool` é
excluído de propósito, porque é subclasse de `int`).

O efeito em produção foi este: `SEED_FAKE_DATA = false`, booleano do TOML,
existia em `st.secrets` e nunca em `os.environ`. `os.getenv` devolvia `None`, o
default de `seed_fake_data` era `True`, e `seed_database()` — que roda a cada
startup — semeava cinco alunos Demo com envios e pontuação no banco de
produção. A chave parecia configurada e configurava o contrário.

O default invertido é o que torna a falha inofensiva: semear dados falsos passa
a exigir opt-in explícito, e uma variável ausente, malformada ou que não chegou
ao processo produz banco limpo. Os testes abaixo exercitam a fronteira pelo
carregador real do Streamlit, porque foi exatamente ali que o valor se perdeu —
um teste que só chamasse `Settings.from_env` com `os.environ` preenchido à mão
nunca teria visto o problema.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from streamlit.runtime.secrets import Secrets

from english_leaderboard.config import Settings

RAIZ = Path(__file__).resolve().parent.parent
CHAVES = ("SEED_FAKE_DATA", "GITHUB_BACKUP_ENABLED", "GOOGLE_SHEETS_AUTO_SYNC")


# O que a validação de produção exige. Como strings, que é justamente a forma
# que o Streamlit promove — o ponto do módulo é o contraste com o booleano.
PRODUCAO = """\
APP_ENV = "production"
SUPABASE_URL = "https://exemplo.supabase.co"
SUPABASE_PUBLISHABLE_KEY = "sb_publishable_teste"
SUPABASE_SECRET_KEY = "sb_secret_teste"
SUPABASE_DB_URL = "postgresql+psycopg://postgres.ref:s@aws-0-us-east-2.pooler.supabase.com:5432/postgres"
"""


@pytest.fixture
def ambiente_limpo(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Diretório de app isolado, com `os.environ` restaurado ao final.

    O `chdir` reproduz o runtime: o Streamlit resolve `secrets.files` — cujo
    padrão é `.streamlit/secrets.toml` — relativo ao diretório da aplicação.

    A cópia do ambiente não é zelo excessivo: `_maybe_set_environment_variable`
    escreve no `os.environ` de verdade, e o `monkeypatch` não desfaz o que não
    foi ele que pôs. Sem restaurar, um teste contamina o seguinte.
    """

    original = os.environ.copy()
    for chave in CHAVES:
        monkeypatch.delenv(chave, raising=False)
    monkeypatch.chdir(tmp_path)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(original)


def _carregar_secrets(conteudo: str, tmp_path: Path) -> Secrets:
    """Escreve um secrets.toml e o carrega como o Streamlit carregaria."""

    destino = tmp_path / ".streamlit" / "secrets.toml"
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text(conteudo, encoding="utf-8")
    segredos = Secrets()
    segredos.load_if_toml_exists()
    return segredos


def test_booleano_do_toml_nao_chega_a_os_environ(ambiente_limpo, tmp_path):
    """A premissa do resto do módulo, verificada e não assumida.

    Se um dia o Streamlit passar a promover `bool`, este teste falha e avisa
    que a defesa abaixo virou redundante — o que é uma boa notícia, mas
    precisa ser lida, não descoberta.
    """

    segredos = _carregar_secrets(PRODUCAO + "SEED_FAKE_DATA = false\n", tmp_path)

    assert segredos["SEED_FAKE_DATA"] is False, "o valor existe em st.secrets"
    assert os.getenv("SEED_FAKE_DATA") is None, "mas não é promovido a env var"
    # Uma string no mesmo arquivo é promovida: a diferença é o tipo, não a chave.
    assert os.getenv("APP_ENV") == "production"


def test_seed_fake_data_nao_liga_sozinho_com_booleano_no_secrets(
    ambiente_limpo, tmp_path
):
    """A regressão que este módulo existe para impedir.

    Com o default anterior (`True`), este é o caminho exato pelo qual a
    produção ganhou cinco alunos Demo.
    """

    _carregar_secrets(PRODUCAO + "SEED_FAKE_DATA = false\n", tmp_path)

    settings = Settings.from_env(env_file=None)

    assert settings.is_production
    assert settings.seed_fake_data is False, (
        "o booleano do TOML não chega a os.environ; com default=True isso "
        "semeia alunos falsos em produção a cada startup"
    )


def test_variavel_ausente_tambem_nao_semeia(ambiente_limpo, tmp_path):
    """Nem toda perda de valor vem do Streamlit: a chave pode só não existir."""

    _carregar_secrets(PRODUCAO, tmp_path)

    assert Settings.from_env(env_file=None).seed_fake_data is False


def test_semear_continua_possivel_com_a_string_certa(ambiente_limpo, tmp_path):
    """Invertido o default, o opt-in tem de continuar funcionando."""

    _carregar_secrets(PRODUCAO + 'SEED_FAKE_DATA = "true"\n', tmp_path)

    assert os.getenv("SEED_FAKE_DATA") == "true"
    assert Settings.from_env(env_file=None).seed_fake_data is True


def test_os_outros_booleanos_lidos_falham_para_o_lado_seguro(
    ambiente_limpo, tmp_path
):
    """`SEED_FAKE_DATA` era o único com default perigoso; que continue sendo.

    `GITHUB_BACKUP_ENABLED` ausente apenas deixa de levantar a recusa, e
    `GOOGLE_SHEETS_AUTO_SYNC` ausente deixa a sincronização desligada. Se algum
    deles ganhar `default=True`, uma configuração perdida passa a ligar
    sozinha um comportamento que ninguém pediu.
    """

    _carregar_secrets(
        PRODUCAO + "GITHUB_BACKUP_ENABLED = false\nGOOGLE_SHEETS_AUTO_SYNC = false\n",
        tmp_path,
    )

    # Nenhum dos dois foi promovido, então ambos caem no default.
    assert all(os.getenv(chave) is None for chave in CHAVES)
    settings = Settings.from_env(env_file=None)
    assert settings.google_sheets_auto_sync is False
    assert settings.seed_fake_data is False


def test_o_exemplo_de_secrets_nao_usa_booleano_de_toml():
    """O exemplo é o que as pessoas copiam: ele não pode ensinar o erro."""

    import tomllib

    exemplo = RAIZ / ".streamlit" / "secrets.toml.example"
    dados = tomllib.loads(exemplo.read_text(encoding="utf-8"))
    booleanos = sorted(
        chave for chave, valor in dados.items() if isinstance(valor, bool)
    )

    assert not booleanos, (
        f"{exemplo.name} traz booleano de TOML em {booleanos}. O Streamlit não "
        "promove bool para os.environ: escreva como string entre aspas."
    )
