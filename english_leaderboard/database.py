"""Engine, sessão e a amarração entre sessão e identidade.

Dois bancos convivem por motivos distintos: o PostgreSQL do Supabase é o
destino de produção, e o SQLite em memória é o que permite à suíte exercitar
os *mesmos* modelos sem depender de rede. O schema é único — ``schema.py`` —
e a portabilidade fica nos tipos, não em modelos paralelos.

A decisão central deste módulo é como a RLS chega até o ORM. A aplicação fala
com o PostgreSQL por conexão direta, e essa conexão autentica como o dono do
banco, que **ignora RLS**. Para as políticas valerem, cada transação precisa
declarar a identidade do usuário. Fazer isso à mão seria frágil: bastaria um
``commit()`` no meio de uma tela para as claims caírem — ``set_config`` é
local à transação — e as consultas seguintes voltariam ao papel proprietário
sem nenhum sinal. Por isso a identidade é gravada na sessão e reaplicada
automaticamente a cada início de transação.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event, inspect, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from .rls_session import aplicar_identidade

# Chave em ``Session.info``: guarda a identidade que as transações assumem.
CHAVE_IDENTIDADE = "rls_user_id"
# Marca a sessão como manutenção deliberada, sem RLS.
CHAVE_SERVICO = "rls_servico"


class IdentidadeAusente(RuntimeError):
    """Sessão PostgreSQL aberta sem dizer sob qual identidade vai rodar."""


def create_database_engine(database_url: str, **kwargs: Any) -> Engine:
    options: dict[str, Any] = {"future": True, "pool_pre_ping": True, **kwargs}
    if database_url.startswith("sqlite"):
        options.setdefault("connect_args", {"check_same_thread": False, "timeout": 30})
        if ":memory:" in database_url:
            options.setdefault("poolclass", StaticPool)
    elif database_url.startswith("postgresql"):
        # Session pooler do Supabase: a conexão é reaproveitada entre
        # requisições, então o pool local fica pequeno e recicla antes do
        # tempo limite do lado do servidor.
        options.setdefault("pool_size", 5)
        options.setdefault("max_overflow", 2)
        options.setdefault("pool_recycle", 900)
        options.setdefault(
            "connect_args",
            {"connect_timeout": 15, "application_name": "english-leaderboard"},
        )
    engine = create_engine(database_url, **options)

    if engine.dialect.name == "sqlite":

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_connection: Any, _: Any) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=30000")
            if ":memory:" not in database_url:
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.close()

    return engine


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


# Tabelas que ``supabase/migrations`` cria e sem as quais a aplicação não sobe.
TABELAS_ESSENCIAIS = ("profiles", "activities", "submissions", "ledger_transactions")


def initialize_database(engine: Engine) -> None:
    """Deixa o banco pronto — de formas diferentes conforme quem manda nele.

    No PostgreSQL o schema pertence a ``supabase/migrations``: criar tabela a
    partir do ORM produziria um schema sem RLS, sem gatilho de imutabilidade e
    sem as restrições que o SQL declara. Aqui só se confere que as migrations
    rodaram.
    """

    from . import schema

    if engine.dialect.name == "postgresql":
        _conferir_migrations(engine)
        return

    schema.SchemaBase.metadata.create_all(engine)
    _gatilhos_de_ledger_sqlite(engine)


def _conferir_migrations(engine: Engine) -> None:
    presentes = set(inspect(engine).get_table_names(schema="public"))
    faltando = [nome for nome in TABELAS_ESSENCIAIS if nome not in presentes]
    if faltando:
        raise RuntimeError(
            "Banco sem as tabelas "
            f"{', '.join(faltando)}. Aplique supabase/migrations "
            "(python tools/apply_migrations.py) antes de subir a aplicação."
        )


def _gatilhos_de_ledger_sqlite(engine: Engine) -> None:
    """Reproduz no SQLite a imutabilidade que o PostgreSQL garante por gatilho.

    Sem isto a suíte não conseguiria provar que correção de pontuação é
    lançamento compensatório, e não edição do histórico.
    """

    with engine.begin() as connection:
        for verbo in ("UPDATE", "DELETE"):
            connection.execute(
                text(
                    f"""
                    CREATE TRIGGER IF NOT EXISTS ledger_transactions_no_{verbo.lower()}
                    BEFORE {verbo} ON ledger_transactions
                    BEGIN
                        SELECT RAISE(ABORT, 'ledger transactions are immutable');
                    END
                    """
                )
            )


@event.listens_for(Session, "after_begin")
def _assumir_identidade(session: Session, _transaction: Any, connection: Any) -> None:
    """Reaplica a identidade a cada transação nova da sessão.

    É aqui que a RLS deixa de depender de disciplina: um ``commit()`` no meio
    da tela descarta as claims, e a transação seguinte as recebe de volta sem
    ninguém precisar lembrar.
    """

    if connection.dialect.name != "postgresql":
        return
    user_id = session.info.get(CHAVE_IDENTIDADE)
    if user_id is not None:
        aplicar_identidade(connection, user_id)
        return
    if not session.info.get(CHAVE_SERVICO):
        raise IdentidadeAusente(
            "Sessão PostgreSQL sem identidade. Use session_scope(..., user_id=...) "
            "para operação de usuário ou servico=True para manutenção."
        )


def _cleanup_pending_uploads(session: Session) -> None:
    for path in session.info.pop("created_upload_paths", []):
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            pass


@event.listens_for(Session, "after_rollback")
def _remove_uploads_after_rollback(session: Session) -> None:
    _cleanup_pending_uploads(session)


@event.listens_for(Session, "after_commit")
def _forget_uploads_after_commit(session: Session) -> None:
    session.info.pop("created_upload_paths", None)


@contextmanager
def session_scope(
    factory: sessionmaker[Session],
    *,
    user_id: str | None = None,
    servico: bool = False,
) -> Iterator[Session]:
    """Sessão transacional. No PostgreSQL, exige declarar sob que identidade.

    ``user_id`` faz as consultas rodarem como o aluno, com todas as políticas
    avaliadas. ``servico=True`` roda como dono do banco, sem RLS, e existe só
    para migração, semeadura e reconciliação — nunca para algo disparado por
    um aluno.
    """

    if user_id is not None and servico:
        raise ValueError("Escolha identidade de usuário ou de serviço, não as duas")

    session = factory()
    if user_id is not None:
        session.info[CHAVE_IDENTIDADE] = str(user_id)
    if servico:
        session.info[CHAVE_SERVICO] = True
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


__all__ = [
    "CHAVE_IDENTIDADE",
    "CHAVE_SERVICO",
    "IdentidadeAusente",
    "create_database_engine",
    "create_session_factory",
    "initialize_database",
    "session_scope",
]
