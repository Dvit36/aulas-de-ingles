"""Prova, contra o banco real, que um aluno não alcança dado de outro.

Cria dois alunos de teste pela Admin API do Supabase, simula a sessão de cada
um com set_config('request.jwt.claims', ...) e tenta os ataques que a RLS
precisa barrar. Remove tudo que criou ao final, inclusive em caso de erro.

Uso:
    python tools/verify_rls.py

===========================================================================
ANTES DE ESCREVER QUALQUER VERIFICAÇÃO SOB RLS, LEIA AS TRÊS ARMADILHAS
===========================================================================

**1. Zero linhas não é ausência — é invisibilidade.** Sob RLS, o que o papel
não pode ver não vira erro: vira consulta vazia, `None`, contagem `0`. Uma
conferência feita sob o mesmo papel que fez a escrita responde "o que este
papel enxerga", e não "o que está gravado". Isso já produziu um falso
"FALHA DE SEGURANÇA" e uma linha de auditoria dada como não gravada quando
estava. Use `conferir()` abaixo: ela sempre lê como dono.

Pior: quando o **código de produção** faz isso, não há nem falso negativo para
notar. `garantir_espaco` somava `submission_files` sob o papel do aluno,
enxergava só os arquivos dele, e o teto de custo do mês decidia contra um
número menor que o real — sem erro nenhum, por semanas.

**2. `session.begin_nested()` reaplica a identidade da sessão.** O SAVEPOINT
dispara `after_begin`, que chama `aplicar_identidade` de novo com o que estiver
em `session.info`. Uma troca de papel feita à mão volta atrás sem avisar, e a
medição passa a responder sobre outra pessoa. Para medir o papel do aluno,
declare a sessão inteira como dele — ou trabalhe no nível da `Connection`, como
este arquivo faz.

E cuidado com o inverso: `Session.rollback()` numa sessão que compartilha a
`Connection` derruba a transação externa junto. Já fez cinco casos de teste
passarem por `function does not exist` em vez de pela trava que deviam provar.

**3. Enumerar tabelas lendo o código acha o que você lembra de procurar.**
O mapa das escritas do envio foi montado lendo `submit_evidence`, e ficou
faltando `storage_usage` — que é escrita dois níveis abaixo, dentro de
`salvar_arquivo`. Quem achou foi rodar o caminho de verdade. Depois de
corrigir, **rode de novo**: a enumeração corrigida ainda é enumeração.
"""

from __future__ import annotations

import json
import sys
import tomllib
import urllib.error
import urllib.request
import uuid
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, text

SEGREDOS = Path(".streamlit/secrets.toml")
resultados: list[tuple[bool, str]] = []


def registrar(ok: bool, descricao: str) -> None:
    resultados.append((ok, descricao))
    print(f"  {'PASSOU ' if ok else 'FALHOU '} {descricao}")


def config() -> dict[str, str]:
    dados = tomllib.loads(SEGREDOS.read_text(encoding="utf-8"))
    return {k: str(v) for k, v in dados.items() if not isinstance(v, dict)}


def admin_api(cfg: dict[str, str], metodo: str, caminho: str, corpo: dict | None = None):
    url = f"{cfg['SUPABASE_URL']}/auth/v1/{caminho}"
    dados = json.dumps(corpo).encode() if corpo else None
    req = urllib.request.Request(url, data=dados, method=metodo)
    chave = cfg["SUPABASE_SECRET_KEY"]
    req.add_header("apikey", chave)
    req.add_header("Authorization", f"Bearer {chave}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resposta:
            corpo_resp = resposta.read()
            return json.loads(corpo_resp) if corpo_resp else {}
    except urllib.error.HTTPError as erro:
        detalhe = erro.read().decode()[:200]
        raise RuntimeError(f"Admin API {erro.code}: {detalhe}") from erro


@contextmanager
def como(conexao, user_id: str | None):
    """Executa o bloco como se fosse a sessão daquele usuário."""

    conexao.execute(text("select set_config('role', 'authenticated', true)"))
    reivindicacoes = json.dumps(
        {"sub": user_id, "role": "authenticated"} if user_id else {}
    )
    conexao.execute(
        text("select set_config('request.jwt.claims', :c, true)"),
        {"c": reivindicacoes},
    )
    try:
        yield
    finally:
        # Uma tentativa barrada aborta a transação; sem o rollback o reset do
        # papel também falharia e mascararia o resultado real do teste.
        try:
            conexao.execute(text("select set_config('role', 'postgres', true)"))
        except Exception:
            conexao.rollback()
            conexao.execute(text("select set_config('role', 'postgres', true)"))


def conferir(conexao, sql: str, params: dict | None = None):
    """Lê **sempre como dono**, e devolve o papel que estava valendo.

    É o par de `como()`: aquela age sob o papel do aluno, esta confere o que
    ficou gravado. Existe porque a conferência sob o papel do aluno é a
    armadilha número 1 do topo deste arquivo — ela devolve zero linhas por
    invisibilidade e ninguém desconfia.

    Quem quiser conferir o que o aluno **enxerga** — que é outra pergunta,
    legítima — use `conexao.execute` dentro do bloco `como()`, e escreva no
    teste que a pergunta é essa.
    """

    papel = conexao.execute(text("select current_setting('role', true)")).scalar()
    conexao.execute(text("select set_config('role', 'postgres', true)"))
    try:
        return conexao.execute(text(sql), params or {}).all()
    finally:
        if papel and papel not in ("postgres", "none"):
            conexao.execute(
                text("select set_config('role', :p, true)"), {"p": papel}
            )


def main() -> int:
    cfg = config()
    if not cfg.get("SUPABASE_SECRET_KEY", "").startswith("sb_secret_"):
        print("SUPABASE_SECRET_KEY ausente ou inválida.")
        return 1

    marca = uuid.uuid4().hex[:8]
    dominio = cfg.get("SUPABASE_USERNAME_DOMAIN", "teste.invalid")
    criados: list[str] = []
    engine = create_engine(cfg["SUPABASE_DB_URL"], connect_args={"connect_timeout": 30})

    try:
        contas = {}
        for papel in ("a", "b", "admin"):
            usuario = f"rlscheck-{papel}-{marca}"
            resposta = admin_api(
                cfg,
                "POST",
                "admin/users",
                {
                    "email": f"{usuario}@{dominio}",
                    "password": uuid.uuid4().hex,
                    "email_confirm": True,
                },
            )
            contas[papel] = (resposta["id"], usuario)
            criados.append(resposta["id"])
        print(f"Criadas 3 contas de teste (marca {marca}).\n")

        with engine.begin() as c:
            for papel, (uid, usuario) in contas.items():
                c.execute(
                    text(
                        "insert into public.profiles (id, username, display_name, role)"
                        " values (:i, :u, :d, :r)"
                    ),
                    {
                        "i": uid,
                        "u": usuario,
                        "d": f"Teste {papel}",
                        "r": "admin" if papel == "admin" else "student",
                    },
                )
            atividade = c.execute(
                text("select id from public.activities limit 1")
            ).scalar()
            atividade_criada = atividade is None
            if atividade is None:
                atividade = c.execute(
                    text(
                        "insert into public.activities (code, name, points)"
                        " values (:c, 'Atividade de teste', 10) returning id"
                    ),
                    {"c": f"rlscheck_{marca}"},
                ).scalar()
            envios = {}
            for papel in ("a", "b"):
                envios[papel] = c.execute(
                    text(
                        "insert into public.submissions (student_id, activity_id, status)"
                        " values (:s, :a, 'needs_review') returning id"
                    ),
                    {"s": contas[papel][0], "a": atividade},
                ).scalar()

        id_a, id_b = contas["a"][0], contas["b"][0]
        id_admin = contas["admin"][0]

        print("Isolamento entre alunos:")
        with engine.connect() as c:
            with como(c, id_a):
                vistos = [
                    str(r[0])
                    for r in c.execute(text("select id from public.submissions"))
                ]
            registrar(
                vistos == [str(envios["a"])],
                f"aluno A enxerga só a própria submissão ({len(vistos)} visível)",
            )

            with como(c, id_a):
                perfis = c.execute(text("select count(*) from public.profiles")).scalar()
            registrar(perfis == 1, f"aluno A enxerga só o próprio perfil ({perfis})")

            with como(c, id_admin):
                total = c.execute(
                    text("select count(*) from public.submissions")
                ).scalar()
            registrar(total >= 2, f"administrador enxerga as submissões ({total})")

        print("\nTentativas de ataque:")
        with engine.connect() as c:
            # student_id forjado na inserção
            with como(c, id_a):
                try:
                    c.execute(
                        text(
                            "insert into public.submissions (student_id, activity_id)"
                            " values (:s, :a)"
                        ),
                        {"s": id_b, "a": atividade},
                    )
                    registrar(False, "inserir submissão com student_id de outro")
                except Exception:
                    registrar(True, "inserir submissão com student_id de outro é barrado")
                finally:
                    c.rollback()

            # auto-promoção a administrador
            with como(c, id_a):
                try:
                    c.execute(
                        text("update public.profiles set role='admin' where id=:i"),
                        {"i": id_a},
                    )
                    registrar(False, "aluno se promover a administrador")
                except Exception:
                    registrar(True, "aluno se promover a administrador é barrado")
                finally:
                    c.rollback()

            # editar submissão de outro
            with como(c, id_a):
                afetadas = c.execute(
                    text(
                        "update public.submissions set title='invadido' where id=:i"
                    ),
                    {"i": envios["b"]},
                ).rowcount
                c.rollback()
            registrar(afetadas == 0, "alterar submissão de outro não afeta linha alguma")

            # storage_key apontando para a pasta de outro aluno
            with engine.connect() as c2:
                try:
                    c2.execute(
                        text(
                            "insert into public.submission_files"
                            " (submission_id, student_id, filename, storage_key,"
                            "  content_type, file_size, checksum_sha256)"
                            " values (:sub, :dono, 'x.jpg', :chave, 'image/jpeg', 10, :ck)"
                        ),
                        {
                            "sub": envios["a"],
                            "dono": id_a,
                            "chave": f"students/{id_b}/uploads/{uuid.uuid4()}.jpg",
                            "ck": "0" * 64,
                        },
                    )
                    registrar(False, "storage_key apontando para a pasta de outro aluno")
                except Exception:
                    registrar(True, "storage_key apontando para pasta de outro é barrada")
                finally:
                    c2.rollback()

        print("\nImutabilidade do ledger:")
        # Tudo dentro de uma transação descartada: o gatilho impede apagar
        # lançamento, então um insert commitado ficaria no banco para sempre.
        with engine.connect() as c:
            transacao = c.begin()
            c.execute(
                text(
                    "insert into public.ledger_transactions"
                    " (student_id, points, kind, source_type, source_key)"
                    " values (:s, 10, 'direct_activity', 'teste', :k)"
                ),
                {"s": id_a, "k": f"rlscheck:{marca}"},
            )
            for rotulo, sql in (
                (
                    "update",
                    (
                        "update public.ledger_transactions set points=999"
                        " where source_key=:k"
                    ),
                ),
                (
                    "delete",
                    "delete from public.ledger_transactions where source_key=:k",
                ),
            ):
                ponto = c.begin_nested()
                try:
                    c.execute(text(sql), {"k": f"rlscheck:{marca}"})
                    registrar(False, f"{rotulo} no ledger")
                    ponto.commit()
                except Exception:
                    registrar(True, f"{rotulo} no ledger é bloqueado por gatilho")
                    ponto.rollback()
            transacao.rollback()

    finally:
        print("\nLimpando dados de teste...")
        with engine.begin() as c:
            c.execute(
                text("delete from public.submissions where student_id = any(:ids)"),
                {"ids": criados},
            )
            c.execute(
                text("delete from public.profiles where id = any(:ids)"), {"ids": criados}
            )
            # Só remove a atividade se foi este teste que a criou.
            if locals().get("atividade_criada"):
                c.execute(
                    text("delete from public.activities where code like 'rlscheck\\_%'")
                )
        for uid in criados:
            try:
                admin_api(cfg, "DELETE", f"admin/users/{uid}")
            except RuntimeError as erro:
                print(f"  aviso: não removeu o usuário {uid}: {erro}")
        engine.dispose()

    falhas = [d for ok, d in resultados if not ok]
    print(f"\n{len(resultados) - len(falhas)}/{len(resultados)} verificações passaram.")
    return 1 if falhas else 0


if __name__ == "__main__":
    sys.exit(main())
