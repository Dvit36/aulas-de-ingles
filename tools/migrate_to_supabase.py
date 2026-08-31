"""Migra o SQLite e os uploads legados para Supabase Auth, PostgreSQL e Storage.

Nada da origem é apagado ou alterado: o banco e os arquivos antigos são
abertos somente para leitura.

Idempotência: só os usuários ganham identificador novo, porque ``profiles.id``
precisa ser o UUID de ``auth.users``. Todo o resto conserva o UUID legado,
então reexecutar pula o que já existe pela chave primária. O mapa de usuários
fica em um manifesto, o que também torna a execução retomável.

Credenciais: ``--execute`` cria as contas no Auth com senha aleatória. Elas são
gravadas em ``migration-credentials.json`` (0600, fora do Git), porque o domínio
de e-mail é ``.invalid`` e não existe recuperação por e-mail — sem esse arquivo
as contas nascem inacessíveis. **Distribua as senhas aos alunos e apague o
arquivo em seguida**: é senha em claro, e existe só para essa entrega.

Uso:
    python tools/migrate_to_supabase.py                 # dry-run (padrão)
    python tools/migrate_to_supabase.py --execute
    python tools/migrate_to_supabase.py --rollback
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tomllib
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from english_leaderboard.storage import (  # noqa: E402
    StorageError,
    SupabaseStorageGateway,
    build_key,
)

SEGREDOS = Path(".streamlit/secrets.toml")
MANIFESTO = Path("migration-manifest.json")
CREDENCIAIS = Path("migration-credentials.json")
ORIGEM_DB = "sqlite:///./data/app.db"
ORIGEM_UPLOADS = Path("data/uploads")

# O SQLite guarda o NOME do enum (APPROVED_AUTO); o PostgreSQL espera o VALOR
# (approved_auto). Converter é obrigatório, não cosmético.
def _enum(valor: str | None) -> str | None:
    return valor.lower() if valor else valor


@dataclass
class Relatorio:
    iniciado_em: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat()
    )
    modo: str = "dry-run"
    usuarios: dict[str, str] = field(default_factory=dict)
    contagens: dict[str, dict[str, int]] = field(default_factory=dict)
    arquivos: dict[str, Any] = field(default_factory=dict)
    conflitos: list[str] = field(default_factory=list)
    orfaos: list[str] = field(default_factory=list)
    erros: list[str] = field(default_factory=list)

    def contar(self, entidade: str, chave: str, quantidade: int = 1) -> None:
        alvo = self.contagens.setdefault(
            entidade, {"origem": 0, "migrados": 0, "existentes": 0, "falhas": 0}
        )
        alvo[chave] = alvo.get(chave, 0) + quantidade


def config() -> dict[str, str]:
    dados = tomllib.loads(SEGREDOS.read_text(encoding="utf-8"))
    return {k: str(v) for k, v in dados.items() if not isinstance(v, dict)}


def admin_api(cfg, caminho, corpo=None, metodo="POST"):
    chave = cfg["SUPABASE_SECRET_KEY"]
    dados = json.dumps(corpo).encode() if corpo is not None else None
    req = urllib.request.Request(
        f"{cfg['SUPABASE_URL']}/auth/v1/{caminho}", data=dados, method=metodo
    )
    req.add_header("apikey", chave)
    req.add_header("Authorization", f"Bearer {chave}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resposta:
            bruto = resposta.read()
            return json.loads(bruto) if bruto else {}
    except urllib.error.HTTPError as erro:
        raise RuntimeError(f"{erro.code}: {erro.read().decode()[:200]}") from erro


def carregar_manifesto() -> dict[str, Any]:
    if MANIFESTO.is_file():
        return json.loads(MANIFESTO.read_text(encoding="utf-8"))
    return {"usuarios": {}, "arquivos": {}}


def gravar_manifesto(dados: dict[str, Any]) -> None:
    MANIFESTO.write_text(
        json.dumps(dados, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def registrar_credencial(username: str, senha: str) -> None:
    """Acrescenta uma senha gerada ao arquivo de credenciais, com modo 0600.

    Fica fora do manifesto de propósito: o manifesto é o estado da migração,
    versionável e útil para o rollback; isto é segredo em claro, descartável, e
    não pode acabar no mesmo arquivo por acidente.

    A gravação é por usuário, e não uma vez ao final, porque a senha só existe
    em memória entre a chamada ao Auth e este ponto. Uma falha no meio do laço
    deixaria a conta criada e a senha perdida — e, com domínio ``.invalid``,
    conta sem senha é conta inacessível. Pelo mesmo motivo o arquivo é lido e
    mesclado antes de reescrito: uma execução retomada não pode apagar as
    senhas que a anterior gravou.
    """

    existentes: dict[str, str] = {}
    if CREDENCIAIS.is_file():
        existentes = json.loads(CREDENCIAIS.read_text(encoding="utf-8"))
    existentes[username] = senha

    # O modo vai no open, e não num chmod posterior, para o arquivo não existir
    # nem por um instante legível por outros.
    descritor = os.open(
        CREDENCIAIS, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600
    )
    with os.fdopen(descritor, "w", encoding="utf-8") as arquivo:
        json.dump(existentes, arquivo, ensure_ascii=False, indent=2, sort_keys=True)
        arquivo.write("\n")
    # Um arquivo de execução anterior pode ter nascido com outro modo: o open
    # acima só aplica o 0600 na criação.
    os.chmod(CREDENCIAIS, 0o600)


# ---------------------------------------------------------------- usuários

def migrar_usuarios(origem, cfg, manifesto, relatorio, executar: bool) -> dict[str, str]:
    """Cria contas no Auth e devolve o mapa id_legado -> uuid novo."""

    mapa: dict[str, str] = dict(manifesto.get("usuarios", {}))
    dominio = cfg.get("SUPABASE_USERNAME_DOMAIN", "robonaticos7565.invalid")
    linhas = origem.execute(
        text("select id, username, display_name, role, active from users")
    ).all()
    relatorio.contar("usuarios", "origem", len(linhas))

    for legado, username, nome, _papel, _ativo in linhas:
        if legado in mapa:
            relatorio.contar("usuarios", "existentes")
            continue
        email = username if "@" in username else f"{username}@{dominio}"
        if not executar:
            mapa[legado] = f"(novo uuid para {username})"
            relatorio.contar("usuarios", "migrados")
            continue
        senha = uuid.uuid4().hex + "Aa1!"
        try:
            resposta = admin_api(
                cfg,
                "admin/users",
                {
                    "email": email,
                    "password": senha,
                    "email_confirm": True,
                    "user_metadata": {"display_name": nome, "legacy_id": legado},
                },
            )
        except RuntimeError as erro:
            if "already been registered" in str(erro):
                relatorio.conflitos.append(f"usuário {username} já existe no Auth")
                relatorio.contar("usuarios", "falhas")
                continue
            relatorio.erros.append(f"usuário {username}: {erro}")
            relatorio.contar("usuarios", "falhas")
            continue
        # Antes de contabilizar: a conta já existe no Auth, e a senha só existe
        # aqui. Registrar primeiro é o que garante que ela não se perca.
        registrar_credencial(username, senha)
        mapa[legado] = resposta["id"]
        relatorio.contar("usuarios", "migrados")
        relatorio.usuarios[username] = resposta["id"]

    manifesto["usuarios"] = mapa
    return mapa


# ------------------------------------------------------------- tabelas

TABELAS = [
    (
        "activities",
        (
            "select id, code, name, points, unit_threshold, requires_images,"
            " requires_summary, requires_title_or_url, summary_min_chars,"
            " content_review_required, auto_approvable, active, config_json"
            " from activities"
        ),
        (
            "insert into public.activities (id, code, name, points,"
            " unit_threshold, requires_images, requires_summary,"
            " requires_title_or_url, summary_min_chars, content_review_required,"
            " auto_approvable, active, config_json) values"
            " (:c0,:c1,:c2,:c3,:c4,:c5,:c6,:c7,:c8,:c9,:c10,:c11,"
            " cast(:c12 as jsonb)) on conflict (id) do nothing"
        ),
        None,
    ),
    (
        "resources",
        "select id, title, url, description, position, active from resources",
        (
            "insert into public.resources (id, title, url, description,"
            " position, active) values (:c0,:c1,:c2,:c3,:c4,:c5)"
            " on conflict (id) do nothing"
        ),
        None,
    ),
]


def migrar_tabela(origem, destino, spec, mapa, relatorio, executar) -> None:
    nome, consulta, insercao, transformar = spec
    linhas = origem.execute(text(consulta)).all()
    relatorio.contar(nome, "origem", len(linhas))
    if not executar:
        relatorio.contar(nome, "migrados", len(linhas))
        return
    for linha in linhas:
        valores = list(linha)
        if transformar:
            valores = transformar(valores, mapa)
            if valores is None:
                relatorio.contar(nome, "falhas")
                continue
        parametros = {f"c{i}": v for i, v in enumerate(valores)}
        try:
            resultado = destino.execute(text(insercao), parametros)
        except Exception as erro:
            relatorio.erros.append(f"{nome}: {type(erro).__name__}")
            relatorio.contar(nome, "falhas")
            continue
        if resultado.rowcount:
            relatorio.contar(nome, "migrados")
        else:
            relatorio.contar(nome, "existentes")


def _mapear_usuario(valor, mapa):
    return mapa.get(valor) if valor else None


def migrar_dominio(origem, destino, mapa, relatorio, executar) -> None:
    """Perfis, submissões e pontuação, na ordem das dependências."""

    # perfis
    linhas = origem.execute(
        text("select id, username, display_name, role, active from users")
    ).all()
    relatorio.contar("profiles", "origem", len(linhas))
    for legado, username, nome, papel, ativo in linhas:
        novo = mapa.get(legado)
        if not executar:
            relatorio.contar("profiles", "migrados")
            continue
        if not novo:
            relatorio.conflitos.append(f"usuário {username} não pôde ser mapeado")
            relatorio.contar("profiles", "falhas")
            continue
        resultado = destino.execute(
            text(
                "insert into public.profiles (id, username, display_name, role,"
                " active) values (:i,:u,:d,:r,:a) on conflict (id) do nothing"
            ),
            {
                "i": novo,
                "u": username,
                "d": nome,
                "r": _enum(papel),
                "a": bool(ativo),
            },
        )
        relatorio.contar("profiles", "migrados" if resultado.rowcount else "existentes")

    for spec in TABELAS:
        migrar_tabela(origem, destino, spec, mapa, relatorio, executar)

    if not executar:
        for nome, consulta in (
            ("submissions", "select count(*) from submissions"),
            ("rule_checks", "select count(*) from rule_checks"),
            ("lesson_units", "select count(*) from lesson_units"),
            ("lesson_batches", "select count(*) from lesson_batches"),
            ("ledger_transactions", "select count(*) from ledger_transactions"),
            ("audit_logs", "select count(*) from audit_logs"),
        ):
            total = origem.execute(text(consulta)).scalar() or 0
            relatorio.contar(nome, "origem", total)
            relatorio.contar(nome, "migrados", total)
        return

    # submissões
    for linha in origem.execute(
        text(
            "select id, student_id, activity_id, status, received_at, processed_at,"
            " decided_at, decided_by_id, title, url, summary, ocr_text,"
            " detected_platform, confidence, declared_units, recognized_units,"
            " points_awarded, rule_snapshot_json, admin_reason, version"
            " from submissions"
        )
    ).all():
        dono = mapa.get(linha[1])
        if not dono:
            relatorio.conflitos.append(f"submissão {linha[0]} sem dono mapeado")
            relatorio.contar("submissions", "falhas")
            continue
        resultado = destino.execute(
            text(
                "insert into public.submissions (id, student_id, activity_id,"
                " status, received_at, processed_at, decided_at, decided_by_id,"
                " title, url, summary, ocr_text, detected_platform, confidence,"
                " declared_units, recognized_units, points_awarded, rule_snapshot,"
                " review_note, version) values (:i,:s,:a,cast(:st as"
                " public.submission_status),:rec,:pro,:dec,:by,:t,:u,:sum,:ocr,"
                " :plat,:conf,:du,:ru,:pa,cast(:rs as jsonb),:rn,:v)"
                " on conflict (id) do nothing"
            ),
            {
                "i": linha[0], "s": dono, "a": linha[2], "st": _enum(linha[3]),
                "rec": linha[4], "pro": linha[5], "dec": linha[6],
                "by": mapa.get(linha[7]), "t": linha[8], "u": linha[9],
                "sum": linha[10], "ocr": linha[11], "plat": linha[12],
                "conf": linha[13] or 0, "du": linha[14] or 0, "ru": linha[15] or 0,
                "pa": linha[16] or 0, "rs": linha[17] or "{}", "rn": linha[18],
                "v": linha[19] or 1,
            },
        )
        relatorio.contar(
            "submissions", "migrados" if resultado.rowcount else "existentes"
        )
        relatorio.contar("submissions", "origem")

    # unidades, lotes e ledger
    for linha in origem.execute(
        text(
            "select id, submission_id, student_id, activity_group, unit_index,"
            " approved_at from lesson_units"
        )
    ).all():
        dono = mapa.get(linha[2])
        if not dono:
            relatorio.contar("lesson_units", "falhas")
            continue
        r = destino.execute(
            text(
                "insert into public.lesson_units (id, submission_id, student_id,"
                " activity_group, unit_index, approved_at)"
                " values (:i,:s,:d,:g,:x,:t) on conflict (id) do nothing"
            ),
            {"i": linha[0], "s": linha[1], "d": dono, "g": linha[3],
             "x": linha[4], "t": linha[5]},
        )
        relatorio.contar("lesson_units", "migrados" if r.rowcount else "existentes")
        relatorio.contar("lesson_units", "origem")

    for linha in origem.execute(
        text("select id, student_id, activity_group, sequence from lesson_batches")
    ).all():
        dono = mapa.get(linha[1])
        if not dono:
            relatorio.contar("lesson_batches", "falhas")
            continue
        r = destino.execute(
            text(
                "insert into public.lesson_batches (id, student_id,"
                " activity_group, sequence) values (:i,:d,:g,:s)"
                " on conflict (id) do nothing"
            ),
            {"i": linha[0], "d": dono, "g": linha[2], "s": linha[3]},
        )
        relatorio.contar("lesson_batches", "migrados" if r.rowcount else "existentes")
        relatorio.contar("lesson_batches", "origem")

    for linha in origem.execute(
        text("select batch_id, unit_id from lesson_batch_units")
    ).all():
        r = destino.execute(
            text(
                "insert into public.lesson_batch_units (batch_id, unit_id)"
                " values (:b,:u) on conflict (unit_id) do nothing"
            ),
            {"b": linha[0], "u": linha[1]},
        )
        relatorio.contar(
            "lesson_batch_units", "migrados" if r.rowcount else "existentes"
        )
        relatorio.contar("lesson_batch_units", "origem")

    for linha in origem.execute(
        text(
            "select id, student_id, points, kind, source_type, source_id,"
            " source_key, activity_id, submission_id, description, occurred_at,"
            " created_by_id from ledger_transactions"
        )
    ).all():
        dono = mapa.get(linha[1])
        if not dono:
            relatorio.contar("ledger_transactions", "falhas")
            continue
        r = destino.execute(
            text(
                "insert into public.ledger_transactions (id, student_id, points,"
                " kind, source_type, source_id, source_key, activity_id,"
                " submission_id, description, occurred_at, created_by_id)"
                " values (:i,:d,:p,cast(:k as public.ledger_kind),:st,:si,:sk,"
                " :a,:sub,:desc,:t,:by) on conflict (id) do nothing"
            ),
            {"i": linha[0], "d": dono, "p": linha[2], "k": _enum(linha[3]),
             "st": linha[4], "si": linha[5], "sk": linha[6], "a": linha[7],
             "sub": linha[8], "desc": linha[9], "t": linha[10],
             "by": mapa.get(linha[11])},
        )
        relatorio.contar(
            "ledger_transactions", "migrados" if r.rowcount else "existentes"
        )
        relatorio.contar("ledger_transactions", "origem")


# ------------------------------------------------------------- arquivos

def migrar_arquivos(
    origem, destino, gateway, cfg, mapa, manifesto, relatorio, executar
) -> None:
    """Envia cada imagem ao Storage, conferindo o checksum antes e depois."""

    bucket = cfg.get("STORAGE_BUCKET", "student-files")
    ja_migrados: dict[str, str] = dict(manifesto.get("arquivos", {}))
    linhas = origem.execute(
        text(
            "select i.id, i.submission_id, i.storage_key, i.client_filename,"
            " i.mime_type, i.size_bytes, i.sha256, i.phash, i.width, i.height,"
            " s.student_id"
            " from submission_images i join submissions s on s.id = i.submission_id"
        )
    ).all()
    relatorio.contar("arquivos", "origem", len(linhas))
    conferidos = 0

    for linha in linhas:
        (fid, sub, chave_local, nome, mime, tamanho, sha, phash,
         largura, altura, dono_legado) = linha
        caminho = ORIGEM_UPLOADS / chave_local
        if not caminho.is_file():
            relatorio.orfaos.append(f"registro {fid} sem arquivo em disco")
            relatorio.contar("arquivos", "falhas")
            continue
        dados = caminho.read_bytes()
        sha_real = hashlib.sha256(dados).hexdigest()
        if sha_real != sha:
            relatorio.conflitos.append(
                f"arquivo {chave_local}: checksum do disco difere do registro"
            )
            relatorio.contar("arquivos", "falhas")
            continue
        conferidos += 1

        dono = mapa.get(dono_legado)
        if not dono:
            relatorio.contar("arquivos", "falhas")
            continue
        if str(fid) in ja_migrados:
            relatorio.contar("arquivos", "existentes")
            continue
        extensao = Path(chave_local).suffix.lstrip(".").lower() or "bin"
        if not executar:
            relatorio.contar("arquivos", "migrados")
            continue

        nova_chave = build_key(dono, "uploads", extensao=extensao, file_id=fid)
        try:
            gateway.upload(nova_chave, dados, mime)
        except StorageError as erro:
            if "exists" not in str(erro).lower():
                relatorio.erros.append(f"arquivo {fid}: {erro}")
                relatorio.contar("arquivos", "falhas")
                continue
        # Checksum depois: confirma que o objeto no destino é o mesmo.
        try:
            devolvido = gateway.download(nova_chave)
        except StorageError as erro:
            relatorio.erros.append(f"arquivo {fid} não pôde ser conferido: {erro}")
            relatorio.contar("arquivos", "falhas")
            continue
        if hashlib.sha256(devolvido).hexdigest() != sha:
            relatorio.conflitos.append(f"arquivo {fid}: checksum divergiu no destino")
            relatorio.contar("arquivos", "falhas")
            continue

        try:
            destino.execute(
                text(
                    "insert into public.submission_files (id, submission_id,"
                    " student_id, filename, storage_provider, storage_bucket,"
                    " storage_key, content_type, file_size, checksum_sha256,"
                    " phash, width, height) values (:i,:s,:d,:n,'supabase',"
                    " :b,:k,:ct,:sz,:ck,:ph,:w,:h) on conflict (id) do nothing"
                ),
                {"i": fid, "s": sub, "d": dono, "n": nome or "arquivo",
                 "b": bucket, "k": nova_chave, "ct": mime, "sz": tamanho,
                 "ck": sha, "ph": phash, "w": largura, "h": altura},
            )
        except Exception as erro:
            # O objeto já subiu: sem o registro, viraria órfão silencioso.
            relatorio.orfaos.append(f"{nova_chave} (registro falhou: {type(erro).__name__})")
            relatorio.contar("arquivos", "falhas")
            continue
        ja_migrados[str(fid)] = nova_chave
        relatorio.contar("arquivos", "migrados")

    manifesto["arquivos"] = ja_migrados
    relatorio.arquivos = {
        "checksums_conferidos_na_origem": conferidos,
        "total_em_disco": len(list(ORIGEM_UPLOADS.glob("*"))) if ORIGEM_UPLOADS.is_dir() else 0,
    }


# ------------------------------------------------------------- rollback

def rollback(cfg, manifesto, relatorio) -> None:
    """Desfaz a migração usando o manifesto. Não toca na origem."""

    engine = create_engine(cfg["SUPABASE_DB_URL"])
    ids = list(manifesto.get("usuarios", {}).values())
    chaves = list(manifesto.get("arquivos", {}).values())
    if not ids and not chaves:
        print("Manifesto vazio: nada a desfazer.")
        return

    gateway = _gateway_de_servico(cfg)
    removidos = 0
    for chave in chaves:
        try:
            gateway.remove(chave)
            removidos += 1
        except StorageError:
            relatorio.orfaos.append(f"{chave} não pôde ser removido")
    with engine.begin() as c:
        # O ledger é imutável por gatilho; desabilitar é explícito e temporário.
        c.execute(
            text("alter table public.ledger_transactions disable trigger ledger_sem_delete")
        )
        for tabela, coluna in (
            ("submission_files", "student_id"),
            ("lesson_batch_units", None),
            ("lesson_units", "student_id"),
            ("lesson_batches", "student_id"),
            ("ledger_transactions", "student_id"),
            ("submissions", "student_id"),
            ("profiles", "id"),
        ):
            if coluna is None:
                c.execute(
                    text(
                        "delete from public.lesson_batch_units where unit_id in"
                        " (select id from public.lesson_units where student_id = any(:i))"
                    ),
                    {"i": ids},
                )
                continue
            c.execute(
                text(f"delete from public.{tabela} where {coluna} = any(:i)"),
                {"i": ids},
            )
        c.execute(
            text("alter table public.ledger_transactions enable trigger ledger_sem_delete")
        )
    for identificador in ids:
        try:
            admin_api(cfg, f"admin/users/{identificador}", metodo="DELETE")
        except RuntimeError:
            pass
    engine.dispose()
    print(f"Rollback concluído: {len(ids)} conta(s), {removidos} objeto(s) removidos.")
    MANIFESTO.unlink(missing_ok=True)


def _gateway_de_servico(cfg) -> SupabaseStorageGateway:
    """Gateway com a chave privilegiada: migração é operação administrativa."""

    from supabase import ClientOptions, create_client

    cliente = create_client(
        cfg["SUPABASE_URL"],
        cfg["SUPABASE_SECRET_KEY"],
        options=ClientOptions(storage_client_timeout=60),
    )
    return SupabaseStorageGateway(cliente, cfg.get("STORAGE_BUCKET", "student-files"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="executa de verdade")
    parser.add_argument("--rollback", action="store_true", help="desfaz pela manifesto")
    parser.add_argument("--report", default="migration-report.json")
    args = parser.parse_args()

    cfg = config()
    manifesto = carregar_manifesto()
    relatorio = Relatorio(modo="execute" if args.execute else "dry-run")

    if args.rollback:
        relatorio.modo = "rollback"
        rollback(cfg, manifesto, relatorio)
        Path(args.report).write_text(
            json.dumps(relatorio.__dict__, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        return 0

    origem_engine = create_engine(ORIGEM_DB)
    destino_engine = create_engine(cfg["SUPABASE_DB_URL"])
    gateway = _gateway_de_servico(cfg) if args.execute else None

    with origem_engine.connect() as origem:
        if args.execute:
            with destino_engine.begin() as destino:
                mapa = migrar_usuarios(origem, cfg, manifesto, relatorio, True)
                gravar_manifesto(manifesto)
                migrar_dominio(origem, destino, mapa, relatorio, True)
                migrar_arquivos(
                    origem, destino, gateway, cfg, mapa, manifesto, relatorio, True
                )
            gravar_manifesto(manifesto)
        else:
            mapa = migrar_usuarios(origem, cfg, manifesto, relatorio, False)
            migrar_dominio(origem, None, mapa, relatorio, False)
            migrar_arquivos(
                origem, None, None, cfg, mapa, manifesto, relatorio, False
            )

    origem_engine.dispose()
    destino_engine.dispose()

    Path(args.report).write_text(
        json.dumps(relatorio.__dict__, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    print(f"Modo: {relatorio.modo}\n")
    print(f"{'entidade':24} {'origem':>7} {'migrar':>7} {'existe':>7} {'falha':>6}")
    print("-" * 56)
    for entidade, contagem in sorted(relatorio.contagens.items()):
        print(
            f"{entidade:24} {contagem['origem']:>7} {contagem['migrados']:>7}"
            f" {contagem['existentes']:>7} {contagem['falhas']:>6}"
        )
    if relatorio.arquivos:
        print(f"\narquivos: {relatorio.arquivos}")
    for rotulo, itens in (
        ("Conflitos", relatorio.conflitos),
        ("Órfãos", relatorio.orfaos),
        ("Erros", relatorio.erros),
    ):
        if itens:
            print(f"\n{rotulo}:")
            for item in itens[:10]:
                print(f"  - {item}")
    print(f"\nRelatório: {args.report}")
    if args.execute and CREDENCIAIS.is_file():
        print(
            f"\nSenhas geradas: {CREDENCIAIS} (modo 0600, fora do Git).\n"
            "Distribua aos alunos e APAGUE o arquivo em seguida — é senha em "
            "claro, e o domínio .invalid não tem recuperação por e-mail."
        )
    if not args.execute:
        print("Nada foi alterado. Use --execute para valer.")
    return 1 if relatorio.erros else 0


if __name__ == "__main__":
    sys.exit(main())
