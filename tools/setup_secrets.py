"""Monta o .streamlit/secrets.toml local sem expor segredos.

As senhas são lidas por getpass: não aparecem na tela, no histórico do shell
nem em nenhum log. O script cuida do prefixo do driver e do percent-encoding,
que são as duas fontes usuais de erro na string de conexão.

Uso:
    python tools/setup_secrets.py
"""

from __future__ import annotations

import os
import stat
import sys
import tomllib
from getpass import getpass
from pathlib import Path
from urllib.parse import quote

DESTINO = Path(".streamlit/secrets.toml")

# Valores públicos do projeto. O Project URL e a publishable key são feitos
# para ficar visíveis; senha do banco e secret key jamais entram aqui.
#
# Toda chave aqui precisa ser lida por Settings.from_env(): escrever um nome
# que a aplicação ignora produz um secrets.toml que parece configurado e não
# configura nada. Era o caso de LOCAL_AUTH_ENABLED, resto da autenticação
# local. O teste tests/test_config_examples.py cobre este dicionário.
PADROES = {
    "APP_ENV": "development",
    "SEED_FAKE_DATA": "false",
    "SUPABASE_USERNAME_DOMAIN": "robonaticos7565.invalid",
}


def _perguntar(rotulo: str, atual: str | None) -> str:
    sufixo = f" [{atual}]" if atual else ""
    resposta = input(f"{rotulo}{sufixo}: ").strip()
    return resposta or (atual or "")


def _perguntar_segredo(rotulo: str, ja_definido: bool) -> str | None:
    extra = " (Enter mantém o atual)" if ja_definido else ""
    valor = getpass(f"{rotulo}{extra}: ").strip()
    return valor or None


def montar_db_url(usuario: str, senha: str, host: str, porta: str) -> str:
    """Monta a URL do pooler com o driver certo e a senha escapada."""

    # safe="" garante que @ : / ? # virem percent-encoding.
    return (
        f"postgresql+psycopg://{quote(usuario, safe='')}:{quote(senha, safe='')}"
        f"@{host}:{porta}/postgres"
    )


def carregar_existente() -> dict[str, str]:
    if not DESTINO.is_file():
        return {}
    try:
        dados = tomllib.loads(DESTINO.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as erro:
        print(f"secrets.toml atual é inválido ({erro}); será refeito.")
        return {}
    return {k: str(v) for k, v in dados.items() if not isinstance(v, dict)}


def main() -> int:
    atual = carregar_existente()
    print("Configuração local do Supabase. Nada é exibido ao digitar senhas.\n")

    url = _perguntar("Project URL", atual.get("SUPABASE_URL"))
    if not url.startswith("https://"):
        print("Project URL deve começar com https://")
        return 1
    referencia = url.removeprefix("https://").split(".")[0]

    publishable = _perguntar(
        "Publishable key (sb_publishable_...)", atual.get("SUPABASE_PUBLISHABLE_KEY")
    )
    if publishable.startswith("sb_secret_"):
        print("Essa é a chave secreta. A publishable começa com sb_publishable_")
        return 1

    host = _perguntar(
        "Host do Session pooler", "aws-0-us-east-2.pooler.supabase.com"
    )
    if "pooler" not in host:
        print("AVISO: host sem 'pooler'. A conexão direta é IPv6 e falha no Cloud.")

    novo = dict(atual)
    for chave, valor in PADROES.items():
        novo.setdefault(chave, valor)
    novo["SUPABASE_URL"] = url.rstrip("/")
    novo["SUPABASE_PUBLISHABLE_KEY"] = publishable

    senha = _perguntar_segredo(
        "Senha do banco", bool(atual.get("SUPABASE_DB_URL"))
    )
    if senha:
        novo["SUPABASE_DB_URL"] = montar_db_url(
            f"postgres.{referencia}", senha, host, "5432"
        )
    elif not atual.get("SUPABASE_DB_URL"):
        print("Senha do banco é obrigatória na primeira execução.")
        return 1

    segredo = _perguntar_segredo(
        "Secret key rotacionada (sb_secret_...)", bool(atual.get("SUPABASE_SECRET_KEY"))
    )
    if segredo:
        if segredo.startswith("sb_publishable_"):
            print("Essa é a chave pública. A privilegiada começa com sb_secret_")
            return 1
        novo["SUPABASE_SECRET_KEY"] = segredo

    # O Storage usa as mesmas credenciais do Supabase; só o bucket é novo.
    novo["STORAGE_BUCKET"] = _perguntar(
        "Bucket privado do Storage", atual.get("STORAGE_BUCKET") or "student-files"
    )

    linhas = [
        "# Gerado por tools/setup_secrets.py. NÃO versionar: contém credenciais.",
        "",
    ]
    for chave in sorted(novo):
        valor = novo[chave].replace('"', '\\"')
        linhas.append(f'{chave} = "{valor}"')

    DESTINO.parent.mkdir(parents=True, exist_ok=True)
    DESTINO.write_text("\n".join(linhas) + "\n", encoding="utf-8")
    os.chmod(DESTINO, stat.S_IRUSR | stat.S_IWUSR)  # 0600

    print(f"\n{DESTINO} escrito com permissão 0600.")
    print("Chaves gravadas:", ", ".join(sorted(novo)))
    print("\nVerifique a conexão com:")
    print("  .venv/bin/python tools/check_supabase_db.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
