"""Aplica as migrations de supabase/migrations em ordem, uma transação por arquivo.

Registra o que já foi aplicado em public.schema_migrations, então reexecutar é
seguro: arquivos já aplicados são pulados. Falha no meio de um arquivo desfaz
aquele arquivo inteiro — nunca deixa schema pela metade.

Uso:
    python tools/apply_migrations.py [--dry-run]
"""

from __future__ import annotations

import hashlib
import sys
import tomllib
from pathlib import Path

from sqlalchemy import create_engine, text

MIGRATIONS = Path("supabase/migrations")
# RLS sem nenhuma política = ninguém lê pela API. O runner conecta como
# postgres, que ignora RLS, então a aplicação das migrations não é afetada.
# Sem isso a tabela ficaria legível via PostgREST por estar em public.
CONTROLE = (
    """
    create table if not exists public.schema_migrations (
        version    text primary key,
        checksum   char(64) not null,
        applied_at timestamptz not null default now()
    )
    """,
    "alter table public.schema_migrations enable row level security",
)


def carregar_url() -> str | None:
    segredos = Path(".streamlit/secrets.toml")
    if segredos.is_file():
        dados = tomllib.loads(segredos.read_text(encoding="utf-8"))
        if dados.get("SUPABASE_DB_URL"):
            return str(dados["SUPABASE_DB_URL"])
    import os

    return os.getenv("SUPABASE_DB_URL")


def main() -> int:
    seco = "--dry-run" in sys.argv
    url = carregar_url()
    if not url:
        print("SUPABASE_DB_URL ausente.")
        return 1

    arquivos = sorted(MIGRATIONS.glob("*.sql"))
    if not arquivos:
        print(f"Nenhuma migration em {MIGRATIONS}")
        return 1

    engine = create_engine(url, connect_args={"connect_timeout": 30})
    with engine.begin() as conexao:
        for comando in CONTROLE:
            conexao.execute(text(comando))
    with engine.connect() as conexao:
        aplicadas = {
            linha[0]: linha[1]
            for linha in conexao.execute(
                text("select version, checksum from public.schema_migrations")
            )
        }

    codigo = 0
    for arquivo in arquivos:
        versao = arquivo.stem
        sql = arquivo.read_text(encoding="utf-8")
        checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()

        if versao in aplicadas:
            if aplicadas[versao] != checksum:
                print(f"[ALTERADA] {versao}: já aplicada com outro conteúdo.")
                codigo = 1
            else:
                print(f"[ok]       {versao}: já aplicada.")
            continue

        if seco:
            print(f"[aplicaria] {versao} ({len(sql.splitlines())} linhas)")
            continue

        try:
            # begin() garante tudo-ou-nada por arquivo.
            with engine.begin() as conexao:
                conexao.execute(text(sql))
                conexao.execute(
                    text(
                        "insert into public.schema_migrations(version, checksum)"
                        " values (:v, :c)"
                    ),
                    {"v": versao, "c": checksum},
                )
        except Exception as erro:
            print(f"[FALHOU]   {versao}: {type(erro).__name__}")
            print(f"           {str(erro).splitlines()[0][:200]}")
            print("           Nada desse arquivo foi aplicado.")
            engine.dispose()
            return 1
        print(f"[aplicada] {versao}")

    engine.dispose()
    return codigo


if __name__ == "__main__":
    sys.exit(main())
