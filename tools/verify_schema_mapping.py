"""Confronta os modelos ORM novos com o schema real do PostgreSQL.

Um mapeamento errado só apareceria em produção, na primeira consulta. Este
script reflete o banco de verdade e compara tabela a tabela, coluna a coluna,
antes de qualquer chamador ser trocado.

Uso:
    python tools/verify_schema_mapping.py
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

from sqlalchemy import create_engine, inspect

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from english_leaderboard.schema import SchemaBase  # noqa: E402

# Tabelas que existem no banco mas não têm modelo: controle e migração.
SEM_MODELO = {"schema_migrations"}


def main() -> int:
    cfg = tomllib.loads(Path(".streamlit/secrets.toml").read_text(encoding="utf-8"))
    engine = create_engine(str(cfg["SUPABASE_DB_URL"]))
    inspetor = inspect(engine)

    no_banco = {
        nome for nome in inspetor.get_table_names(schema="public")
    } - SEM_MODELO
    no_orm = set(SchemaBase.metadata.tables)

    problemas: list[str] = []

    faltando = no_banco - no_orm
    sobrando = no_orm - no_banco
    if faltando:
        problemas.append(f"tabelas no banco sem modelo: {sorted(faltando)}")
    if sobrando:
        problemas.append(f"modelos sem tabela no banco: {sorted(sobrando)}")

    print(f"{'tabela':24} {'colunas ORM':>12} {'no banco':>9}  situação")
    print("-" * 64)
    for nome in sorted(no_orm & no_banco):
        colunas_banco = {c["name"]: c for c in inspetor.get_columns(nome, schema="public")}
        tabela = SchemaBase.metadata.tables[nome]
        colunas_orm = {c.name for c in tabela.columns}

        ausentes = colunas_orm - set(colunas_banco)
        extras = set(colunas_banco) - colunas_orm

        # Coluna a mais no banco só é problema se for NOT NULL sem default:
        # o ORM não conseguiria inserir uma linha.
        criticas = [
            c
            for c in extras
            if not colunas_banco[c]["nullable"] and colunas_banco[c]["default"] is None
        ]

        situacao = "ok"
        if ausentes:
            situacao = f"ORM cita colunas inexistentes: {sorted(ausentes)}"
            problemas.append(f"{nome}: {situacao}")
        elif criticas:
            situacao = f"banco exige colunas ausentes no ORM: {sorted(criticas)}"
            problemas.append(f"{nome}: {situacao}")
        elif extras:
            situacao = f"não mapeadas (opcionais): {sorted(extras)}"

        print(f"{nome:24} {len(colunas_orm):>12} {len(colunas_banco):>9}  {situacao}")

    # Tabelas gravadas por SQL direto (storage_service) não passam pelos
    # padrões do ORM: nelas, um default que só existe no PostgreSQL vira
    # NOT NULL sem valor no SQLite dos testes — e o INSERT falha.
    escritas_por_sql = {"submission_files", "storage_orphans", "storage_usage"}
    for nome in sorted(escritas_por_sql & (no_orm & no_banco)):
        colunas_banco = {
            c["name"]: c for c in inspetor.get_columns(nome, schema="public")
        }
        tabela = SchemaBase.metadata.tables[nome]
        for coluna in tabela.columns:
            no_bd = colunas_banco.get(coluna.name)
            if no_bd is None or coluna.primary_key:
                continue
            if no_bd["default"] is not None and coluna.server_default is None:
                problemas.append(
                    f"{nome}.{coluna.name}: padrão existe só no PostgreSQL; "
                    "o ORM precisa de server_default para o INSERT em SQL "
                    "direto funcionar nos dois bancos"
                )

    engine.dispose()

    if problemas:
        print("\nPROBLEMAS:")
        for item in problemas:
            print(f"  - {item}")
        return 1
    print("\nMapeamento consistente com o banco.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
