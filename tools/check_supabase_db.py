"""Verifica a conexão com o PostgreSQL do Supabase sem expor a senha.

Uso:
    python scripts_check_db.py

Lê SUPABASE_DB_URL do .streamlit/secrets.toml ou do ambiente. Nada é impresso
além do host, da versão do servidor e do resultado — a senha nunca aparece.
"""

from __future__ import annotations

import os
import sys
import tomllib
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url


def carregar_url() -> str | None:
    segredos = Path(".streamlit/secrets.toml")
    if segredos.is_file():
        dados = tomllib.loads(segredos.read_text(encoding="utf-8"))
        valor = dados.get("SUPABASE_DB_URL")
        if valor:
            return str(valor)
    return os.getenv("SUPABASE_DB_URL")


def main() -> int:
    bruto = carregar_url()
    if not bruto or "SENHA" in bruto or "SEU-REF" in bruto:
        print("SUPABASE_DB_URL ausente ou ainda com placeholder.")
        return 1

    url = make_url(bruto)
    print(f"host   : {url.host}")
    print(f"porta  : {url.port}")
    print(f"usuário: {url.username}")

    if url.host and "pooler" not in url.host:
        print("AVISO  : não é o Session pooler. Em produção isso falha (IPv6).")

    try:
        engine = create_engine(bruto, connect_args={"connect_timeout": 15})
        with engine.connect() as conexao:
            versao = conexao.execute(text("select version()")).scalar()
            agora = conexao.execute(text("select now()")).scalar()
            quem = conexao.execute(text("select current_user")).scalar()
        engine.dispose()
    except Exception as erro:
        # Nunca ecoa a URL: ela contém a senha.
        print(f"FALHOU : {type(erro).__name__}: {str(erro).splitlines()[0][:160]}")
        return 1

    print(f"servidor: {str(versao).split(',')[0]}")
    print(f"usuário conectado: {quem}")
    print(f"hora do servidor : {agora}")
    print("CONEXÃO OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
