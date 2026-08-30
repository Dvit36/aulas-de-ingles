"""Os exemplos de configuração têm de descrever o que a aplicação realmente lê.

Este teste existe por causa de um erro concreto: o `.env.example` documentou
`SUPABASE_ANON_KEY` e `SUPABASE_SERVICE_ROLE_KEY` por semanas depois de
`config.py` ter passado a ler `SUPABASE_PUBLISHABLE_KEY` e
`SUPABASE_SECRET_KEY`, e `tools/setup_secrets.py` gravava `LOCAL_AUTH_ENABLED`,
resto da autenticação local. Quem seguisse a documentação montava um ambiente
que parecia configurado e subia sem Supabase nenhum.

A lista de variáveis não é escrita à mão aqui: ela é extraída da árvore
sintática de `config.py`, então acompanha o código sem ninguém precisar
lembrar. As duas exceções — variáveis lidas mas deliberadamente fora do
exemplo, e chaves do exemplo consumidas por terceiros — são explícitas e
também são verificadas, para a própria lista de exceções não apodrecer.
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

import pytest

from english_leaderboard.config import Settings

RAIZ = Path(__file__).resolve().parent.parent
ENV_EXAMPLE = RAIZ / ".env.example"
SECRETS_EXAMPLE = RAIZ / ".streamlit" / "secrets.toml.example"
CONFIG = RAIZ / "english_leaderboard" / "config.py"


def _variaveis_de_config() -> tuple[frozenset[str], frozenset[str]]:
    """Nomes atuais e aliases legados que ``config.py`` consulta.

    Lido da AST, e não por regex, para pegar também as chamadas quebradas em
    várias linhas — que são a maioria em ``from_env``.
    """

    arvore = ast.parse(CONFIG.read_text(encoding="utf-8"))
    atuais: set[str] = set()
    legados: set[str] = set()
    for no in ast.walk(arvore):
        if not isinstance(no, ast.Call):
            continue
        nome = getattr(no.func, "attr", None) or getattr(no.func, "id", None)
        literais = [
            arg.value
            for arg in no.args
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
        ]
        if nome == "getenv" and literais:
            atuais.add(literais[0])
        elif nome == "_env_with_legacy" and len(literais) >= 2:
            atuais.add(literais[0])
            legados.add(literais[1])
    return frozenset(atuais), frozenset(legados)


LIDAS, ALIASES_LEGADOS = _variaveis_de_config()


# Variáveis que a aplicação lê e que o `.env.example` deliberadamente omite.
# Documentá-las convidaria a usá-las. O motivo fica registrado aqui porque é a
# única coisa que impede alguém de "consertar" a ausência mais tarde.
NAO_DOCUMENTADAS = {
    "DEMO_AUTH_ENABLED": (
        "O login demo foi removido junto com a autenticação local; a flag só "
        "semeia perfis sem conta no Auth."
    ),
    "DEMO_STUDENT_USERNAME": "Só tem efeito com DEMO_AUTH_ENABLED.",
    "DEMO_ADMIN_USERNAME": "Só tem efeito com DEMO_AUTH_ENABLED.",
    "LOGIN_MAX_ATTEMPTS": (
        "Resto do bloqueio por tentativas da autenticação local. Ainda é "
        "validada no startup, mas nenhum código a consome."
    ),
    "LOGIN_LOCK_MINUTES": "Idem LOGIN_MAX_ATTEMPTS.",
    "GITHUB_BACKUP_ENABLED": (
        "Descontinuada: o startup recusa o valor true. Documentar seria "
        "sugerir que ainda existe backup no GitHub."
    ),
    "GITHUB_BACKUP_REPO": "Idem GITHUB_BACKUP_ENABLED.",
    "GITHUB_BACKUP_TOKEN": "Idem GITHUB_BACKUP_ENABLED.",
    "GITHUB_BACKUP_PATH": "Idem GITHUB_BACKUP_ENABLED.",
    "GITHUB_BACKUP_BRANCH": "Idem GITHUB_BACKUP_ENABLED.",
}

# Chaves que aparecem nos exemplos e que `Settings.from_env()` nunca lê porque
# pertencem a outro consumidor.
TERCEIROS_PERMITIDOS = {
    "STREAMLIT_SERVER_MAX_UPLOAD_SIZE": "lida pelo próprio Streamlit",
    "STREAMLIT_BROWSER_GATHER_USAGE_STATS": "lida pelo próprio Streamlit",
    "STREAMLIT_SECRETS_FILE": "usada pelo docker-compose para montar o arquivo",
    "GOOGLE_APPLICATION_CREDENTIALS": "lida pela biblioteca de auth do Google",
    "GOOGLE_SERVICE_ACCOUNT_FILE": "usada pelo overlay docker-compose.google.yml",
}

# Sem estas a aplicação recusa subir em produção. Precisam estar no modelo de
# Secrets, que é o que se cola no painel do Streamlit Cloud.
OBRIGATORIAS_EM_PRODUCAO = (
    "SUPABASE_URL",
    "SUPABASE_PUBLISHABLE_KEY",
    "SUPABASE_SECRET_KEY",
    "SUPABASE_DB_URL",
)


def _chaves_do_env(caminho: Path) -> dict[str, str]:
    """Pares ``CHAVE=valor`` do arquivo, incluindo os comentados.

    Uma variável comentada continua sendo documentação: se o nome estiver
    errado, ela engana igual.
    """

    pares: dict[str, str] = {}
    for linha in caminho.read_text(encoding="utf-8").splitlines():
        crua = linha.strip()
        if crua.startswith("#"):
            crua = crua.lstrip("#").strip()
        if not crua or "=" not in crua:
            continue
        chave, _, valor = crua.partition("=")
        chave = chave.strip()
        if chave.isupper() and chave.replace("_", "").isalnum():
            pares[chave] = valor.strip()
    return pares


def _chaves_do_secrets(caminho: Path) -> dict[str, str]:
    """Chaves de nível raiz do modelo TOML.

    Só a raiz porque é só ela que o Streamlit exporta como variável de
    ambiente — chaves dentro de uma tabela ``[supabase]`` não chegariam a
    ``Settings.from_env()``.
    """

    dados = tomllib.loads(caminho.read_text(encoding="utf-8"))
    return {k: str(v) for k, v in dados.items() if not isinstance(v, dict)}


ENV = _chaves_do_env(ENV_EXAMPLE)
SECRETS = _chaves_do_secrets(SECRETS_EXAMPLE)


def test_a_extracao_encontrou_a_configuracao() -> None:
    """Guarda contra o próprio teste passar por não ter lido nada."""

    assert len(LIDAS) > 40, "a leitura da AST de config.py não encontrou as variáveis"
    assert "SUPABASE_DB_URL" in LIDAS
    assert ENV and SECRETS


def test_env_example_so_declara_variaveis_que_a_aplicacao_le() -> None:
    desconhecidas = set(ENV) - LIDAS - set(TERCEIROS_PERMITIDOS)
    assert not desconhecidas, (
        ".env.example declara variáveis que Settings.from_env() nunca lê: "
        f"{sorted(desconhecidas)}. Corrija o nome, remova a linha, ou "
        "registre-a em TERCEIROS_PERMITIDOS se outro consumidor a usa."
    )


def test_env_example_documenta_toda_variavel_que_a_aplicacao_le() -> None:
    faltando = LIDAS - set(ENV) - set(NAO_DOCUMENTADAS)
    assert not faltando, (
        "Settings.from_env() lê variáveis ausentes do .env.example: "
        f"{sorted(faltando)}. Documente-as ou justifique a omissão em "
        "NAO_DOCUMENTADAS."
    )


def test_secrets_example_so_declara_variaveis_que_a_aplicacao_le() -> None:
    desconhecidas = set(SECRETS) - LIDAS - set(TERCEIROS_PERMITIDOS)
    assert not desconhecidas, (
        "secrets.toml.example declara variáveis que Settings.from_env() nunca "
        f"lê: {sorted(desconhecidas)}"
    )


@pytest.mark.parametrize("nome", OBRIGATORIAS_EM_PRODUCAO)
def test_secrets_example_traz_o_que_producao_exige(nome: str) -> None:
    assert nome in SECRETS, (
        f"{nome} é obrigatória quando APP_ENV=production e não está no modelo "
        "de Secrets. Quem copiar o modelo para o Streamlit Cloud vai subir uma "
        "aplicação que se recusa a iniciar."
    )


def test_os_exemplos_nao_usam_os_nomes_antigos() -> None:
    """Os aliases legados são aceitos, mas não devem ser ensinados."""

    for rotulo, chaves in (("`.env.example`", ENV), ("secrets.toml.example", SECRETS)):
        vazados = ALIASES_LEGADOS & set(chaves)
        assert not vazados, (
            f"{rotulo} usa nomes legados {sorted(vazados)}. Eles ainda são "
            "aceitos para não derrubar ambientes implantados, mas o exemplo "
            "deve ensinar o nome atual."
        )


def test_padroes_do_setup_secrets_sao_lidos_pela_aplicacao() -> None:
    """O script gera o secrets.toml local; um nome morto ali não faz nada."""

    import tools.setup_secrets as setup_secrets

    desconhecidas = set(setup_secrets.PADROES) - LIDAS
    assert not desconhecidas, (
        "tools/setup_secrets.py gravaria chaves que Settings.from_env() ignora: "
        f"{sorted(desconhecidas)}. Foi o caso de LOCAL_AUTH_ENABLED."
    )


def test_a_lista_de_omissoes_nao_esta_obsoleta() -> None:
    """Se a variável deixou de ser lida, a justificativa perdeu o sentido."""

    obsoletas = set(NAO_DOCUMENTADAS) - LIDAS
    assert not obsoletas, (
        f"NAO_DOCUMENTADAS cita variáveis que config.py não lê mais: "
        f"{sorted(obsoletas)}. Remova-as da lista junto com o código."
    )


def test_a_lista_de_terceiros_nao_esta_obsoleta() -> None:
    """Se a aplicação passou a ler a variável, ela deixou de ser de terceiros."""

    reivindicadas = set(TERCEIROS_PERMITIDOS) & LIDAS
    assert not reivindicadas, (
        f"TERCEIROS_PERMITIDOS cita variáveis que config.py passou a ler: "
        f"{sorted(reivindicadas)}. Tire-as da lista e documente-as normalmente."
    )


def test_env_example_produz_uma_configuracao_valida(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """O caminho que o README manda seguir tem de terminar em app que sobe.

    Vale o exemplo inteiro, com os valores que ele traz: um limite incoerente
    ou um booleano mal escrito aparece aqui, e não no primeiro `streamlit run`
    de quem acabou de clonar o repositório.
    """

    for nome in LIDAS | ALIASES_LEGADOS | set(TERCEIROS_PERMITIDOS):
        monkeypatch.delenv(nome, raising=False)
    for nome, valor in ENV.items():
        if nome in TERCEIROS_PERMITIDOS:
            continue
        monkeypatch.setenv(nome, valor)

    settings = Settings.from_env(env_file=None)

    assert settings.app_env == "development"
    # O exemplo é de desenvolvimento: sem Supabase preenchido ele cai no SQLite
    # local, e isso tem de ser uma escolha visível, não um acidente.
    assert not settings.supabase_ready
    assert settings.database_url.startswith("sqlite")
    assert not settings.seed_fake_data


def test_env_example_vira_producao_apenas_completando_o_supabase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """O exemplo tem de ser um ponto de partida honesto para produção."""

    for nome in LIDAS | ALIASES_LEGADOS | set(TERCEIROS_PERMITIDOS):
        monkeypatch.delenv(nome, raising=False)
    for nome, valor in ENV.items():
        if nome in TERCEIROS_PERMITIDOS:
            continue
        monkeypatch.setenv(nome, valor)

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("SUPABASE_URL", "https://projeto.supabase.co")
    monkeypatch.setenv("SUPABASE_PUBLISHABLE_KEY", "sb_publishable_chave")
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "sb_secret_chave")
    monkeypatch.setenv(
        "SUPABASE_DB_URL",
        "postgresql+psycopg://postgres.ref:senha"
        "@aws-0-sa-east-1.pooler.supabase.com:5432/postgres",
    )

    settings = Settings.from_env(env_file=None)

    assert settings.is_production
    assert settings.supabase_ready
    assert settings.storage_ready
    assert settings.admin_secret_key() == "sb_secret_chave"


def test_o_exemplo_nao_carrega_valor_de_credencial() -> None:
    """Modelo é modelo: nenhum segredo pode nascer preenchido."""

    sensiveis = (
        "SUPABASE_PUBLISHABLE_KEY",
        "SUPABASE_SECRET_KEY",
        "SUPABASE_DB_URL",
        "SUPABASE_URL",
        "BOOTSTRAP_ADMIN_PASSWORD",
        "SMTP_PASSWORD",
    )
    preenchidas = [nome for nome in sensiveis if ENV.get(nome)]
    assert not preenchidas, (
        f".env.example traz valor em {preenchidas}. O modelo é versionado: "
        "credencial nenhuma pode nascer preenchida nele."
    )


def test_a_documentacao_do_secrets_avisa_sobre_o_nivel_raiz() -> None:
    """O erro de aninhar em [supabase] é silencioso; o aviso é a única defesa."""

    texto = SECRETS_EXAMPLE.read_text(encoding="utf-8")
    assert "raiz" in texto.lower(), (
        "O modelo de Secrets precisa explicar que só chaves de nível raiz "
        "chegam a Settings.from_env(); dentro de uma tabela elas são ignoradas "
        "sem erro."
    )


def test_o_gitignore_cobre_as_variantes_de_secrets() -> None:
    """O modelo é versionado; toda variante preenchida fica de fora.

    O padrão antigo cobria só `secrets.toml`, e um `secrets.cloud.toml` — que é
    o nome natural para separar os segredos do Cloud — entraria no commit
    seguinte sem aviso nenhum.
    """

    ignorados = (RAIZ / ".gitignore").read_text(encoding="utf-8")
    assert ".streamlit/secrets*.toml" in ignorados
    assert "!.streamlit/secrets.toml.example" in ignorados
    assert ".env\n" in ignorados
