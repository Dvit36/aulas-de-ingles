"""Prova, contra o bucket real, que um aluno não alcança arquivo de outro.

Cria dois alunos e um administrador pela Admin API, autentica cada um para
obter o JWT e usa o adaptador de verdade — o mesmo código que a aplicação
usará. Remove tudo que criou ao final, inclusive em caso de erro.

Uso:
    python tools/verify_storage.py
"""

from __future__ import annotations

import json
import sys
import tomllib
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from sqlalchemy import create_engine, text

from english_leaderboard.storage import (
    StorageError,
    SupabaseStorageGateway,
    build_key,
    criar_cliente,
)

SEGREDOS = Path(".streamlit/secrets.toml")
CONTEUDO = b"comprovante-de-teste"
resultados: list[tuple[bool, str]] = []


def registrar(ok: bool, descricao: str) -> None:
    resultados.append((ok, descricao))
    print(f"  {'PASSOU ' if ok else 'FALHOU '} {descricao}")


def config() -> dict[str, str]:
    dados = tomllib.loads(SEGREDOS.read_text(encoding="utf-8"))
    return {k: str(v) for k, v in dados.items() if not isinstance(v, dict)}


def _chamar(url: str, chave: str, corpo: dict | None, metodo: str, bearer: str | None = None):
    dados = json.dumps(corpo).encode() if corpo is not None else None
    req = urllib.request.Request(url, data=dados, method=metodo)
    req.add_header("apikey", chave)
    req.add_header("Authorization", f"Bearer {bearer or chave}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resposta:
            corpo_resp = resposta.read()
            return json.loads(corpo_resp) if corpo_resp else {}
    except urllib.error.HTTPError as erro:
        raise RuntimeError(f"{erro.code}: {erro.read().decode()[:200]}") from erro


def criar_usuario(cfg: dict[str, str], email: str, senha: str) -> str:
    resposta = _chamar(
        f"{cfg['SUPABASE_URL']}/auth/v1/admin/users",
        cfg["SUPABASE_SECRET_KEY"],
        {"email": email, "password": senha, "email_confirm": True},
        "POST",
    )
    return resposta["id"]


def autenticar(cfg: dict[str, str], email: str, senha: str) -> str:
    """Faz login como o aluno para obter o JWT que as políticas avaliam."""

    resposta = _chamar(
        f"{cfg['SUPABASE_URL']}/auth/v1/token?grant_type=password",
        cfg["SUPABASE_PUBLISHABLE_KEY"],
        {"email": email, "password": senha},
        "POST",
    )
    return resposta["access_token"]


def gateway_de(cfg: dict[str, str], token: str) -> SupabaseStorageGateway:
    cliente = criar_cliente(
        cfg["SUPABASE_URL"], cfg["SUPABASE_PUBLISHABLE_KEY"], token
    )
    return SupabaseStorageGateway(cliente, cfg.get("STORAGE_BUCKET", "student-files"))


def main() -> int:
    cfg = config()
    if not cfg.get("SUPABASE_SECRET_KEY", "").startswith("sb_secret_"):
        print("SUPABASE_SECRET_KEY ausente.")
        return 1
    bucket = cfg.get("STORAGE_BUCKET", "student-files")
    dominio = cfg.get("SUPABASE_USERNAME_DOMAIN", "teste.invalid")
    marca = uuid.uuid4().hex[:8]
    print(f"bucket: {bucket}   marca: {marca}\n")

    criados: list[str] = []
    engine = create_engine(cfg["SUPABASE_DB_URL"], connect_args={"connect_timeout": 30})
    chaves: dict[str, str] = {}

    try:
        contas = {}
        for papel in ("a", "b", "admin"):
            usuario = f"stcheck-{papel}-{marca}"
            email = f"{usuario}@{dominio}"
            senha = uuid.uuid4().hex + "Aa1!"
            uid = criar_usuario(cfg, email, senha)
            criados.append(uid)
            contas[papel] = {
                "id": uid,
                "usuario": usuario,
                "token": autenticar(cfg, email, senha),
            }
        with engine.begin() as c:
            for papel, dados in contas.items():
                c.execute(
                    text(
                        "insert into public.profiles (id, username, display_name, role)"
                        " values (:i, :u, :d, :r)"
                    ),
                    {
                        "i": dados["id"],
                        "u": dados["usuario"],
                        "d": f"Teste {papel}",
                        "r": "admin" if papel == "admin" else "student",
                    },
                )
        print("3 contas criadas e autenticadas.\n")

        ga = gateway_de(cfg, contas["a"]["token"])
        gb = gateway_de(cfg, contas["b"]["token"])
        gadmin = gateway_de(cfg, contas["admin"]["token"])

        print("Upload na própria pasta:")
        chaves["a"] = build_key(contas["a"]["id"], "uploads", extensao="txt")
        chaves["b"] = build_key(contas["b"]["id"], "uploads", extensao="txt")
        for papel, gw in (("a", ga), ("b", gb)):
            try:
                gw.upload(chaves[papel], CONTEUDO, "text/plain")
                registrar(True, f"aluno {papel.upper()} envia para a própria pasta")
            except StorageError as erro:
                registrar(False, f"aluno {papel.upper()} não conseguiu enviar: {erro}")
                return 1

        print("\nTentativas de acesso cruzado:")
        chave_invasora = build_key(contas["b"]["id"], "uploads", extensao="txt")
        try:
            ga.upload(chave_invasora, b"invasao", "text/plain")
            registrar(False, "aluno A gravou na pasta do aluno B")
        except StorageError:
            registrar(True, "aluno A gravando na pasta do B é barrado pela política")

        try:
            ga.download(chaves["b"])
            registrar(False, "aluno A leu o arquivo do aluno B")
        except StorageError:
            registrar(True, "aluno A lendo arquivo do B é barrado pela política")

        try:
            ga.signed_url(chaves["b"], 60)
            registrar(False, "aluno A assinou URL do arquivo do aluno B")
        except StorageError:
            registrar(True, "aluno A assinando URL do B é barrado pela política")

        ga.remove(chaves["b"])
        try:
            conteudo_b = gb.download(chaves["b"])
            registrar(
                conteudo_b == CONTEUDO,
                "arquivo do B sobreviveu à tentativa de remoção pelo A",
            )
        except StorageError:
            registrar(False, "o arquivo do aluno B foi removido pelo aluno A")

        print("\nAcesso legítimo:")
        registrar(ga.download(chaves["a"]) == CONTEUDO, "aluno A lê o próprio arquivo")
        registrar(
            gadmin.download(chaves["a"]) == CONTEUDO,
            "administrador lê o arquivo do aluno",
        )

        url = ga.signed_url(chaves["a"], 60)
        try:
            with urllib.request.urlopen(url, timeout=20) as r:
                registrar(r.read() == CONTEUDO, "URL assinada entrega o conteúdo")
        except urllib.error.HTTPError as erro:
            registrar(False, f"URL assinada devolveu {erro.code}")

        publica = (
            f"{cfg['SUPABASE_URL']}/storage/v1/object/public/{bucket}/{chaves['a']}"
        )
        try:
            with urllib.request.urlopen(publica, timeout=20):
                registrar(False, "BUCKET ESTÁ PÚBLICO — leitura sem assinatura funcionou")
        except urllib.error.HTTPError as erro:
            registrar(
                erro.code in (400, 401, 403, 404),
                f"leitura sem assinatura é recusada ({erro.code})",
            )

    finally:
        print("\nLimpando...")
        try:
            gadmin = gateway_de(cfg, contas["admin"]["token"])
            for chave in chaves.values():
                try:
                    gadmin.remove(chave)
                except StorageError:
                    pass
        except Exception:
            pass
        with engine.begin() as c:
            c.execute(
                text("delete from public.profiles where id = any(:ids)"),
                {"ids": criados},
            )
        for uid in criados:
            try:
                _chamar(
                    f"{cfg['SUPABASE_URL']}/auth/v1/admin/users/{uid}",
                    cfg["SUPABASE_SECRET_KEY"],
                    None,
                    "DELETE",
                )
            except RuntimeError as erro:
                print(f"  aviso: usuário {uid} não removido: {erro}")
        engine.dispose()

    falhas = [d for ok, d in resultados if not ok]
    print(f"\n{len(resultados) - len(falhas)}/{len(resultados)} verificações passaram.")
    return 1 if falhas else 0


if __name__ == "__main__":
    sys.exit(main())
