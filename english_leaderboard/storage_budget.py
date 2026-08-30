"""Trava de orçamento do Supabase Storage.

O plano gratuito inclui 1 GB de espaço e 10 GB de egress por mês; acima disso
a cobrança é real e não existe teto que pause o serviço. O limite tem de ser
aplicado antes de cada operação, aqui.

Espaço sai da soma dos metadados, que é de graça. Egress é contado nesta
tabela porque o Storage não expõe o número em tempo real — e é justamente o
egress que costuma escapar, já que cada download de comprovante soma.

Nenhuma dessas travas protege contra alguém que tenha as chaves e escreva
direto no bucket. Elas protegem contra o risco real: um laço na aplicação.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.engine import Connection

UPLOAD = "upload_count"
DOWNLOAD = "download_count"


class StorageBudgetExceeded(RuntimeError):
    """Operação recusada porque estouraria o orçamento configurado."""


@dataclass(frozen=True, slots=True)
class UsoStorage:
    period: str
    egress_bytes: int
    upload_count: int
    download_count: int
    stored_bytes: int

    def resumo(self, limites: LimitesStorage) -> str:
        mb = 1024 * 1024
        return (
            f"{self.stored_bytes / mb:.1f} MB de "
            f"{limites.max_total_bytes / mb:.0f} MB · "
            f"egress {self.egress_bytes / mb:.1f} MB de "
            f"{limites.max_monthly_egress_bytes / mb:.0f} MB · "
            f"{self.upload_count} envio(s), {self.download_count} download(s)"
        )


@dataclass(frozen=True, slots=True)
class LimitesStorage:
    max_total_bytes: int
    max_monthly_egress_bytes: int


def periodo_atual(momento: datetime | None = None) -> str:
    agora = momento or datetime.now(timezone.utc)
    return f"{agora.year:04d}-{agora.month:02d}"


def bytes_armazenados(conexao: Connection) -> int:
    """Espaço ocupado, somado dos metadados em vez de consultado no bucket."""

    return int(
        conexao.execute(
            text("select coalesce(sum(file_size), 0) from submission_files")
        ).scalar()
        or 0
    )


def uso_atual(conexao: Connection, *, momento: datetime | None = None) -> UsoStorage:
    periodo = periodo_atual(momento)
    linha = conexao.execute(
        text(
            "select egress_bytes, upload_count, download_count"
            " from storage_usage where period = :p"
        ),
        {"p": periodo},
    ).first()
    return UsoStorage(
        period=periodo,
        egress_bytes=int(linha[0]) if linha else 0,
        upload_count=int(linha[1]) if linha else 0,
        download_count=int(linha[2]) if linha else 0,
        stored_bytes=bytes_armazenados(conexao),
    )


def garantir_espaco(
    conexao: Connection,
    *,
    bytes_novos: int,
    limites: LimitesStorage,
) -> None:
    """Recusa antes de enviar, para não pagar pelo objeto que seria rejeitado."""

    if bytes_novos <= 0:
        raise ValueError("Tamanho do objeto deve ser positivo")
    ocupado = bytes_armazenados(conexao)
    if ocupado + bytes_novos > limites.max_total_bytes:
        raise StorageBudgetExceeded(
            f"Envio recusado: ocuparia {(ocupado + bytes_novos) / 1024 / 1024:.1f} MB "
            f"de um limite de {limites.max_total_bytes / 1024 / 1024:.0f} MB. "
            "Aumente STORAGE_MAX_TOTAL_BYTES ou libere espaço."
        )


def garantir_egress(
    conexao: Connection,
    *,
    bytes_saida: int,
    limites: LimitesStorage,
    momento: datetime | None = None,
) -> None:
    """Recusa o download quando o egress do mês acabou.

    Egress é o que mais costuma escapar: cada visualização de comprovante
    soma, e o Storage não avisa quando a franquia está no fim.
    """

    if bytes_saida <= 0:
        raise ValueError("Tamanho da saída deve ser positivo")
    uso = uso_atual(conexao, momento=momento)
    if uso.egress_bytes + bytes_saida > limites.max_monthly_egress_bytes:
        mb = 1024 * 1024
        raise StorageBudgetExceeded(
            f"Download recusado: o egress do mês ({uso.egress_bytes / mb:.1f} MB) "
            f"atingiu o limite de {limites.max_monthly_egress_bytes / mb:.0f} MB. "
            "Nada foi transferido."
        )


def registrar_upload(conexao: Connection, *, momento: datetime | None = None) -> None:
    """Contabiliza um envio já concluído. Upload não conta como egress."""

    _somar(conexao, {"upload_count": 1}, momento=momento)


def registrar_download(
    conexao: Connection, *, bytes_saida: int, momento: datetime | None = None
) -> None:
    """Contabiliza uma entrega já feita, somando ao egress cobrado."""

    if bytes_saida < 0:
        raise ValueError("Tamanho da saída não pode ser negativo")
    _somar(
        conexao,
        {"download_count": 1, "egress_bytes": bytes_saida},
        momento=momento,
    )


def _somar(
    conexao: Connection,
    incrementos: dict[str, int],
    *,
    momento: datetime | None = None,
) -> None:
    """Upsert atômico: contagens concorrentes somam em vez de se sobrescrever."""

    colunas = ", ".join(incrementos)
    valores = ", ".join(f":{c}" for c in incrementos)
    atualizacoes = ", ".join(
        f"{c} = storage_usage.{c} + :{c}" for c in incrementos
    )
    conexao.execute(
        text(
            f"insert into storage_usage (period, {colunas})"
            f" values (:p, {valores})"
            f" on conflict (period) do update set {atualizacoes},"
            # CURRENT_TIMESTAMP em vez de now(): idêntico no PostgreSQL e no
            # SQLite, que é onde os testes rodam sem rede.
            " updated_at = CURRENT_TIMESTAMP"
        ),
        {"p": periodo_atual(momento), **incrementos},
    )


__all__ = [
    "DOWNLOAD",
    "UPLOAD",
    "LimitesStorage",
    "StorageBudgetExceeded",
    "UsoStorage",
    "bytes_armazenados",
    "garantir_egress",
    "garantir_espaco",
    "periodo_atual",
    "registrar_download",
    "registrar_upload",
    "uso_atual",
]
