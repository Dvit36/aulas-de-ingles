"""Travas de orçamento do Supabase Storage.

Não existe teto de gasto que pause o serviço. Estes testes garantem que a
recusa acontece na aplicação, antes da operação e portanto antes de virar
cobrança.
"""

from __future__ import annotations

from uuid import uuid4

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, text

from english_leaderboard.schema import SchemaBase
from english_leaderboard.config import Settings
from english_leaderboard.storage_budget import (
    LimitesStorage,
    StorageBudgetExceeded,
    bytes_armazenados,
    garantir_egress,
    garantir_espaco,
    periodo_atual,
    registrar_download,
    registrar_upload,
    uso_atual,
)

LIMITES = LimitesStorage(
    max_total_bytes=1_000_000,
    max_monthly_egress_bytes=500_000,
)


@pytest.fixture
def conexao():
    """Espelho mínimo do schema, em SQLite: a lógica é SQL portátil."""

    engine = create_engine("sqlite+pysqlite:///:memory:")
    SchemaBase.metadata.create_all(engine)
    with engine.connect() as c:
        yield c
    engine.dispose()


def _gravar(conexao, *tamanhos: int) -> None:
    """Grava metadados mínimos; só ``file_size`` importa para o orçamento."""

    for tamanho in tamanhos:
        conexao.execute(
            text(
                "insert into submission_files"
                " (id, submission_id, student_id, filename, storage_provider,"
                "  storage_bucket, storage_key, content_type, file_size,"
                "  checksum_sha256)"
                " values (:id, :sub, :dono, 'a.jpg', 'supabase', 'student-files',"
                "         :key, 'image/jpeg', :t, :ck)"
            ),
            {
                "id": str(uuid4()),
                "sub": str(uuid4()),
                "dono": str(uuid4()),
                "key": f"students/{uuid4()}/uploads/{uuid4()}.jpg",
                "t": tamanho,
                "ck": f"{tamanho:064d}",
            },
        )


def test_storage_is_measured_from_metadata_not_from_the_bucket(conexao) -> None:
    """Consultar o bucket gastaria operação de Classe A justamente para saber
    se ainda há orçamento. A soma vem dos metadados, que é de graça."""

    assert bytes_armazenados(conexao) == 0
    _gravar(conexao, 1_000, 2_500, 500)
    assert bytes_armazenados(conexao) == 4_000


def test_upload_that_would_exceed_the_quota_is_refused_before_sending(conexao) -> None:
    _gravar(conexao, 900_000)

    # Cabe: 900k + 50k fica sob 1M.
    garantir_espaco(conexao, bytes_novos=50_000, limites=LIMITES)

    with pytest.raises(StorageBudgetExceeded, match="Envio recusado"):
        garantir_espaco(conexao, bytes_novos=200_000, limites=LIMITES)


def test_exactly_at_the_limit_passes_and_one_byte_over_does_not(conexao) -> None:
    _gravar(conexao, 999_999)
    garantir_espaco(conexao, bytes_novos=1, limites=LIMITES)
    with pytest.raises(StorageBudgetExceeded):
        garantir_espaco(conexao, bytes_novos=2, limites=LIMITES)


def test_zero_or_negative_size_is_rejected(conexao) -> None:
    for tamanho in (0, -1):
        with pytest.raises(ValueError):
            garantir_espaco(conexao, bytes_novos=tamanho, limites=LIMITES)


def test_counters_accumulate_atomically(conexao) -> None:
    registrar_upload(conexao)
    registrar_upload(conexao)
    registrar_download(conexao, bytes_saida=1_500)

    uso = uso_atual(conexao)
    assert uso.period == periodo_atual()
    assert uso.upload_count == 2
    assert uso.download_count == 1
    assert uso.egress_bytes == 1_500


def test_upload_does_not_count_as_egress(conexao) -> None:
    """Só a saída é cobrada; enviar não consome a franquia de egress."""

    for _ in range(50):
        registrar_upload(conexao)

    assert uso_atual(conexao).egress_bytes == 0
    garantir_egress(conexao, bytes_saida=1, limites=LIMITES)


def test_monthly_egress_budget_blocks_a_runaway_download_loop(conexao) -> None:
    """O risco real não é uso normal: é um laço baixando sem parar."""

    entregues = 0
    for _ in range(500):
        try:
            garantir_egress(conexao, bytes_saida=50_000, limites=LIMITES)
        except StorageBudgetExceeded:
            break
        registrar_download(conexao, bytes_saida=50_000)
        entregues += 1

    # 500.000 de franquia / 50.000 por download = 10, e não as 500 tentativas.
    assert entregues == 10
    assert uso_atual(conexao).egress_bytes == 500_000


def test_storage_and_egress_budgets_are_independent(conexao) -> None:
    registrar_download(conexao, bytes_saida=500_000)

    with pytest.raises(StorageBudgetExceeded, match="Download recusado"):
        garantir_egress(conexao, bytes_saida=1, limites=LIMITES)

    # Espaço tem franquia própria e continua liberado.
    garantir_espaco(conexao, bytes_novos=1_000, limites=LIMITES)


def test_budget_resets_between_months(conexao) -> None:
    janeiro = datetime(2026, 1, 15, tzinfo=timezone.utc)
    fevereiro = datetime(2026, 2, 1, tzinfo=timezone.utc)

    registrar_download(conexao, bytes_saida=500_000, momento=janeiro)
    with pytest.raises(StorageBudgetExceeded):
        garantir_egress(
            conexao, bytes_saida=1, limites=LIMITES, momento=janeiro
        )

    garantir_egress(conexao, bytes_saida=1, limites=LIMITES, momento=fevereiro)
    assert uso_atual(conexao, momento=fevereiro).egress_bytes == 0


def test_negative_sizes_are_rejected(conexao) -> None:
    with pytest.raises(ValueError):
        garantir_egress(conexao, bytes_saida=0, limites=LIMITES)
    with pytest.raises(ValueError):
        registrar_download(conexao, bytes_saida=-1)


def test_configured_limits_cannot_exceed_the_free_allowance() -> None:
    """Passar da franquia significa aceitar cobrança: tem de ser explícito."""

    Settings(storage_max_total_bytes=1024**3).validate()

    with pytest.raises(ValueError, match="franquia"):
        Settings(storage_max_total_bytes=1024**3 + 1).validate()
    with pytest.raises(ValueError, match="franquia"):
        Settings(storage_max_monthly_egress_bytes=10 * 1024**3 + 1).validate()
    for campo in ("storage_max_total_bytes", "storage_max_monthly_egress_bytes"):
        with pytest.raises(ValueError, match="positivo"):
            Settings(**{campo: 0}).validate()


def test_per_file_limit_respects_the_free_plan_ceiling() -> None:
    """O plano gratuito recusa arquivo individual acima de 50 MB."""

    with pytest.raises(ValueError, match="50 MB"):
        Settings(
            max_upload_bytes=60 * 1024**2,
            max_upload_total_bytes=200 * 1024**2,
            max_document_expanded_bytes=200 * 1024**2,
        ).validate()

    # 50 MB exatos passam: é o teto do plano, não um valor proibido.
    Settings(
        max_upload_bytes=50 * 1024**2,
        max_upload_total_bytes=200 * 1024**2,
        max_document_expanded_bytes=200 * 1024**2,
    ).validate()


def test_defaults_leave_wide_margin_over_the_expected_season() -> None:
    """~48 MB por temporada projetados; o padrão dá 10x de folga e fica na
    metade da franquia gratuita."""

    padrao = Settings()
    assert padrao.storage_max_total_bytes == 500 * 1024**2
    assert padrao.storage_max_total_bytes < 1024**3
    assert padrao.storage_max_monthly_egress_bytes < 10 * 1024**3
    assert padrao.storage_bucket == "student-files"


def test_usage_summary_is_readable_for_the_admin(conexao) -> None:
    _gravar(conexao, 500_000)
    registrar_download(conexao, bytes_saida=100_000)

    resumo = uso_atual(conexao).resumo(LIMITES)

    assert "0.5 MB de 1 MB" in resumo
    assert "1 download(s)" in resumo
