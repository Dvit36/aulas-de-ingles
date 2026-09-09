"""Autenticação pelo Supabase Auth, sem rede.

O duplo do gateway permite provar os caminhos que importam: credencial
errada, sessão expirada, renovação, e a garantia de que token não vaza para
representação nem para mensagem de erro.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from english_leaderboard.supabase_auth import (
    AuthError,
    CredenciaisInvalidas,
    Sessao,
    SessaoExpirada,
    atualizar_username,
    criar_conta,
    desativar_conta,
    entrar,
    gerar_senha_temporaria,
    reativar_conta,
    redefinir_senha,
    remover_conta,
    renovar,
    sair,
    trocar_senha,
    username_para_email,
)

DOMINIO = "robonaticos7565.invalid"
PUBLICA = "sb_publishable_teste"
SECRETA = "sb_secret_teste"
AGORA = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
UID = "11111111-1111-1111-1111-111111111111"


class GatewayFalso:
    """Duplo do gateway HTTP.

    ``verbos`` fica separado de ``chamadas`` de propósito: o verbo é o que
    diferencia atualizar conta de criar conta no mesmo caminho, e um duplo que
    só guarda corpo e chave não consegue provar qual dos dois foi enviado.
    """

    def __init__(self) -> None:
        self.chamadas: list[tuple[str, dict, str]] = []
        self.verbos: list[str] = []
        self.credenciais: list[tuple[str, str]] = []
        self.respostas: dict[str, dict] = {}
        self.erros: dict[str, Exception] = {}

    def _registrar(
        self, verbo: str, caminho: str, corpo: dict, chave: str, autorizacao: str | None
    ) -> dict:
        self.chamadas.append((caminho, corpo, chave))
        self.verbos.append(verbo)
        # `apikey` e `Authorization` respondem a perguntas diferentes, e um
        # duplo que guardasse só uma não conseguiria provar que não voltaram a
        # ser a mesma coisa.
        self.credenciais.append((chave, autorizacao or chave))
        if caminho in self.erros:
            raise self.erros[caminho]
        return self.respostas.get(caminho, {})

    def post(self, caminho, corpo, *, chave, autorizacao=None) -> dict:
        return self._registrar("POST", caminho, corpo, chave, autorizacao)

    def put(self, caminho, corpo, *, chave, autorizacao=None) -> dict:
        return self._registrar("PUT", caminho, corpo, chave, autorizacao)

    def delete(self, caminho, *, chave, autorizacao=None) -> dict:
        return self._registrar("DELETE", caminho, {}, chave, autorizacao)


def _resposta_sessao(expires_in: int = 3600) -> dict:
    return {
        "access_token": "token-de-acesso",
        "refresh_token": "token-de-renovacao",
        "expires_in": expires_in,
        "user": {"id": UID},
    }


# --------------------------------------------------- usuário vira e-mail

def test_username_is_translated_to_the_internal_address() -> None:
    assert username_para_email("ana.silva", DOMINIO) == f"ana.silva@{DOMINIO}"
    # Normaliza como o resto do sistema.
    assert username_para_email("  Ana.Silva  ", DOMINIO) == f"ana.silva@{DOMINIO}"


def test_migrated_accounts_keep_their_address() -> None:
    """Quem veio do modelo antigo já tem um e-mail no campo username."""

    assert (
        username_para_email("luiz@gmail.com", DOMINIO) == "luiz@gmail.com"
    )


def test_invalid_username_or_domain_is_refused() -> None:
    with pytest.raises(ValueError):
        username_para_email("a b", DOMINIO)
    for ruim in ("", "semponto", "@"):
        with pytest.raises((AuthError, ValueError)):
            username_para_email("ana", ruim)


# --------------------------------------------------------------- login

def test_successful_login_returns_a_session(monkeypatch) -> None:
    gateway = GatewayFalso()
    gateway.respostas["token?grant_type=password"] = _resposta_sessao()

    sessao = entrar(
        gateway,
        username="ana.silva",
        password="qualquer",
        chave_publica=PUBLICA,
        dominio=DOMINIO,
        agora=AGORA,
    )

    assert sessao.user_id == UID
    assert sessao.expires_at == AGORA + timedelta(hours=1)
    # O login usa a chave pública, nunca a privilegiada.
    _, corpo, chave = gateway.chamadas[0]
    assert chave == PUBLICA
    assert corpo["email"] == f"ana.silva@{DOMINIO}"


def test_wrong_password_is_reported_without_leaking_the_body() -> None:
    gateway = GatewayFalso()
    gateway.erros["token?grant_type=password"] = CredenciaisInvalidas(
        "Usuário ou senha incorretos"
    )

    with pytest.raises(CredenciaisInvalidas, match="Usuário ou senha incorretos"):
        entrar(
            gateway,
            username="ana.silva",
            password="errada",
            chave_publica=PUBLICA,
            dominio=DOMINIO,
        )


def test_empty_password_never_reaches_the_service() -> None:
    gateway = GatewayFalso()

    with pytest.raises(CredenciaisInvalidas, match="Informe a senha"):
        entrar(
            gateway,
            username="ana.silva",
            password="",
            chave_publica=PUBLICA,
            dominio=DOMINIO,
        )

    assert gateway.chamadas == []


def test_incomplete_response_is_refused() -> None:
    gateway = GatewayFalso()
    gateway.respostas["token?grant_type=password"] = {"access_token": "x"}

    with pytest.raises(AuthError, match="incompleta"):
        entrar(
            gateway,
            username="ana.silva",
            password="s",
            chave_publica=PUBLICA,
            dominio=DOMINIO,
        )


# ------------------------------------------------------------ sessão

def test_tokens_do_not_leak_into_representations() -> None:
    """Um log acidental da sessão não pode expor o token."""

    sessao = Sessao(
        user_id=UID,
        access_token="token-secreto-de-acesso",
        refresh_token="token-secreto-de-renovacao",
        expires_at=AGORA,
    )

    assert "token-secreto" not in repr(sessao)
    assert "token-secreto" not in str(sessao)
    assert UID in repr(sessao)


def test_expiry_and_renewal_window() -> None:
    sessao = Sessao(UID, "a", "r", AGORA + timedelta(seconds=90))

    assert sessao.expirada(AGORA) is False
    assert sessao.expirada(AGORA + timedelta(seconds=91)) is True

    # A renovação é antecipada em 60s, para não usar um token que vence no
    # meio da requisição. Com 90s restantes ainda não é hora.
    assert sessao.precisa_renovar(AGORA) is False
    # Com 30s restantes, é.
    assert sessao.precisa_renovar(AGORA + timedelta(seconds=60)) is True
    # E uma sessão já vencida obviamente precisa.
    assert sessao.precisa_renovar(AGORA + timedelta(seconds=120)) is True


def test_refresh_returns_a_new_session() -> None:
    gateway = GatewayFalso()
    gateway.respostas["token?grant_type=refresh_token"] = _resposta_sessao(7200)

    nova = renovar(
        gateway,
        refresh_token="token-de-renovacao",
        chave_publica=PUBLICA,
        agora=AGORA,
    )

    assert nova.expires_at == AGORA + timedelta(hours=2)


def test_refusing_the_refresh_token_means_the_session_expired() -> None:
    gateway = GatewayFalso()
    gateway.erros["token?grant_type=refresh_token"] = CredenciaisInvalidas("nao")

    with pytest.raises(SessaoExpirada, match="entre novamente"):
        renovar(gateway, refresh_token="velho", chave_publica=PUBLICA)

    with pytest.raises(SessaoExpirada, match="sem token"):
        renovar(gateway, refresh_token="", chave_publica=PUBLICA)


def test_logout_failure_does_not_trap_the_user(caplog) -> None:
    """A sessão local é descartada de qualquer jeito — mas o erro é registrado.

    Não relançar é certo: uma sessão já expirada recusa aqui, e prender alguém
    dentro do aplicativo por causa disso seria pior. O que estava errado era o
    silêncio — este `except` escondeu por semanas que o logout nunca revogava
    nada, porque o `apikey` ia com o token do usuário.
    """

    gateway = GatewayFalso()
    gateway.erros["logout"] = AuthError("servidor fora do ar")

    with caplog.at_level("WARNING", logger="english_leaderboard.supabase_auth"):
        sair(gateway, access_token="token", chave_publica=PUBLICA)  # não levanta

    assert any("não revogou a sessão" in r.message for r in caplog.records)
    assert any(r.exc_info for r in caplog.records), "sem o traceback não há o que depurar"

    sair(gateway, access_token="", chave_publica=PUBLICA)


# ------------------------------------------------- operações administrativas

def test_account_creation_uses_the_privileged_key_and_confirms_the_email() -> None:
    gateway = GatewayFalso()
    gateway.respostas["admin/users"] = {"id": UID}

    identificador, senha = criar_conta(
        gateway,
        username="ana.silva",
        display_name="Ana Silva",
        chave_secreta=SECRETA,
        dominio=DOMINIO,
    )

    assert identificador == UID
    assert len(senha) == 16
    caminho, corpo, chave = gateway.chamadas[0]
    assert caminho == "admin/users"
    assert chave == SECRETA  # criar conta exige a privilegiada
    # Sem endereço real não há link de confirmação: a conta nasce confirmada.
    assert corpo["email_confirm"] is True
    assert corpo["email"] == f"ana.silva@{DOMINIO}"
    # Criar é POST em `admin/users`; atualizar é PUT em `admin/users/{id}`.
    assert gateway.verbos == ["POST"]


def test_password_reset_replaces_recovery_by_email() -> None:
    gateway = GatewayFalso()

    senha = redefinir_senha(gateway, user_id=UID, chave_secreta=SECRETA)

    assert len(senha) == 16
    caminho, corpo, chave = gateway.chamadas[0]
    assert caminho == f"admin/users/{UID}"
    assert corpo == {"password": senha}
    assert chave == SECRETA
    assert gateway.verbos == ["PUT"]


def test_user_changes_own_password_with_their_own_token() -> None:
    gateway = GatewayFalso()

    trocar_senha(
        gateway,
        access_token="token-do-aluno",
        nova_senha="nova",
        chave_publica=PUBLICA,
    )

    caminho, corpo, _ = gateway.chamadas[0]
    assert caminho == "user"
    assert corpo == {"password": "nova"}
    # A chave privilegiada não participa: a troca é do próprio usuário.
    apikey, autorizacao = gateway.credenciais[0]
    assert apikey == PUBLICA
    assert autorizacao == "token-do-aluno"


def test_changing_password_without_a_session_is_refused() -> None:
    gateway = GatewayFalso()

    with pytest.raises(SessaoExpirada):
        trocar_senha(
            gateway, access_token="", nova_senha="nova", chave_publica=PUBLICA
        )
    with pytest.raises(ValueError, match="nova senha"):
        trocar_senha(gateway, access_token="t", nova_senha="", chave_publica=PUBLICA)

    assert gateway.chamadas == []


def test_deactivation_revokes_at_the_auth_service() -> None:
    """Desativar só o perfil deixaria a sessão em curso válida até expirar."""

    gateway = GatewayFalso()

    desativar_conta(gateway, user_id=UID, chave_secreta=SECRETA)

    caminho, corpo, chave = gateway.chamadas[0]
    assert caminho == f"admin/users/{UID}"
    assert "ban_duration" in corpo
    assert chave == SECRETA
    # `admin/users/{id}` serve criação e atualização; só o verbo os separa.
    assert gateway.verbos == ["PUT"]


def test_the_http_gateway_really_sends_the_verb_it_promises(monkeypatch) -> None:
    """O duplo prova a intenção; só aqui o verbo vira requisição de verdade.

    `admin/users/{id}` responde a POST e a PUT no GoTrue, então um erro de
    verbo não apareceria como 405 — passaria batido. O teste olha o método na
    requisição montada, antes de ela sair.
    """

    import urllib.request

    from english_leaderboard.supabase_auth import HttpAuthGateway

    vistos: list[tuple[str, str, bytes | None]] = []

    class RespostaFalsa:
        def read(self) -> bytes:
            return b"{}"

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    def urlopen_falso(requisicao, timeout=None):
        vistos.append(
            (requisicao.get_method(), requisicao.full_url, requisicao.data)
        )
        return RespostaFalsa()

    monkeypatch.setattr(urllib.request, "urlopen", urlopen_falso)
    gateway = HttpAuthGateway("https://projeto.supabase.co")

    gateway.put(f"admin/users/{UID}", {"ban_duration": "none"}, chave=SECRETA)
    gateway.post("admin/users", {"email": "a@b.invalid"}, chave=SECRETA)
    gateway.delete(f"admin/users/{UID}", chave=SECRETA)

    assert [verbo for verbo, _, _ in vistos] == ["PUT", "POST", "DELETE"]
    assert vistos[0][1] == (
        f"https://projeto.supabase.co/auth/v1/admin/users/{UID}"
    )
    assert vistos[2][2] is None  # DELETE não leva corpo


def test_reactivation_clears_the_ban_instead_of_omitting_it() -> None:
    """`"none"` é como o GoTrue diz "sem ban".

    Omitir o campo não desfaz nada: a conta seguiria banida e o teste que só
    olhasse o caminho passaria mesmo assim.
    """

    gateway = GatewayFalso()

    reativar_conta(gateway, user_id=UID, chave_secreta=SECRETA)

    caminho, corpo, chave = gateway.chamadas[0]
    assert caminho == f"admin/users/{UID}"
    assert corpo == {"ban_duration": "none"}
    assert chave == SECRETA
    assert gateway.verbos == ["PUT"]


def test_the_apikey_header_never_carries_a_user_token(monkeypatch) -> None:
    """`apikey` é chave de API; `Authorization` é quem age. Nunca a mesma coisa.

    Enquanto os dois cabeçalhos levavam o mesmo valor, uma operação feita com o
    token do próprio usuário punha um JWT no lugar da chave de API, e o GoTrue
    recusava com `Invalid API key` **antes de olhar o token**. Duas chamadas
    caíam nisso: `trocar_senha`, que falhava à vista, e `sair`, que falhava em
    silêncio — nenhuma sessão era revogada no servidor.

    O duplo não alcança isto: é aqui que os cabeçalhos viram requisição.
    """

    import urllib.request

    from english_leaderboard.supabase_auth import HttpAuthGateway

    vistos: list[tuple[str, str, str]] = []

    class RespostaFalsa:
        def read(self) -> bytes:
            return b"{}"

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    def urlopen_falso(requisicao, timeout=None):
        vistos.append(
            (
                requisicao.full_url,
                requisicao.get_header("Apikey"),
                requisicao.get_header("Authorization"),
            )
        )
        return RespostaFalsa()

    monkeypatch.setattr(urllib.request, "urlopen", urlopen_falso)
    gateway = HttpAuthGateway("https://projeto.supabase.co")
    TOKEN = "token-do-aluno"

    # Operação do próprio usuário: chave de API na porta, token na autorização.
    trocar_senha(
        gateway, access_token=TOKEN, nova_senha="nova", chave_publica=PUBLICA
    )
    sair(gateway, access_token=TOKEN, chave_publica=PUBLICA)

    # Operação administrativa: a secret responde pelas duas coisas.
    desativar_conta(gateway, user_id=UID, chave_secreta=SECRETA)

    for url, apikey, autorizacao in vistos:
        assert apikey in (PUBLICA, SECRETA), f"{url} mandou {apikey!r} como apikey"
        assert apikey != TOKEN, f"{url} pôs o token do usuário no apikey"

    assert vistos[0][1] == PUBLICA
    assert vistos[0][2] == f"Bearer {TOKEN}"
    assert vistos[1][1] == PUBLICA
    assert vistos[1][2] == f"Bearer {TOKEN}"
    assert vistos[2][1] == SECRETA
    assert vistos[2][2] == f"Bearer {SECRETA}"


# Cada chamada ao GoTrue, com o caminho e o verbo que ela deve usar.
#
# Existe porque um verbo errado passou despercebido: `trocar_senha` ficou em
# POST quando `put` foi acrescentado ao gateway e as outras atualizações foram
# padronizadas. O teste que a cobria conferia corpo e chave, não o método — e
# o defeito só apareceu em produção, com um 405, depois que outro defeito
# parou de mascará-lo.
#
# A tabela é o inventário: uma chamada nova sem linha aqui é uma chamada cujo
# verbo ninguém conferiu.
CHAMADAS_ESPERADAS = [
    (
        "entrar",
        lambda g: entrar(
            g, username="ana", password="s3nh4", chave_publica=PUBLICA,
            dominio=DOMINIO,
        ),
        "POST",
        "token?grant_type=password",
    ),
    (
        "renovar",
        lambda g: renovar(
            g, refresh_token="tok", chave_publica=PUBLICA,
        ),
        "POST",
        "token?grant_type=refresh_token",
    ),
    (
        "sair",
        lambda g: sair(g, access_token="tok", chave_publica=PUBLICA),
        "POST",
        "logout",
    ),
    (
        "criar_conta",
        lambda g: criar_conta(
            g, username="ana", display_name="Ana", chave_secreta=SECRETA,
            dominio=DOMINIO,
        ),
        "POST",
        "admin/users",
    ),
    (
        "trocar_senha",
        lambda g: trocar_senha(
            g, access_token="tok", nova_senha="nova", chave_publica=PUBLICA,
        ),
        "PUT",
        "user",
    ),
    (
        "redefinir_senha",
        lambda g: redefinir_senha(g, user_id=UID, chave_secreta=SECRETA),
        "PUT",
        f"admin/users/{UID}",
    ),
    (
        "atualizar_username",
        lambda g: atualizar_username(
            g, user_id=UID, username="ana", chave_secreta=SECRETA,
            dominio=DOMINIO,
        ),
        "PUT",
        f"admin/users/{UID}",
    ),
    (
        "desativar_conta",
        lambda g: desativar_conta(g, user_id=UID, chave_secreta=SECRETA),
        "PUT",
        f"admin/users/{UID}",
    ),
    (
        "reativar_conta",
        lambda g: reativar_conta(g, user_id=UID, chave_secreta=SECRETA),
        "PUT",
        f"admin/users/{UID}",
    ),
    (
        "remover_conta",
        lambda g: remover_conta(g, user_id=UID, chave_secreta=SECRETA),
        "DELETE",
        f"admin/users/{UID}",
    ),
]


@pytest.mark.parametrize(
    ("rotulo", "executar", "verbo", "caminho"),
    CHAMADAS_ESPERADAS,
    ids=[c[0] for c in CHAMADAS_ESPERADAS],
)
def test_every_call_uses_the_verb_and_path_gotrue_expects(
    rotulo, executar, verbo, caminho
) -> None:
    """Verbo e caminho de cada chamada, conferidos um a um.

    `POST /user` chegou a ser roteado pelo GoTrue com JWT inválido — o
    middleware corta antes — então nem uma sonda contra o serviço real
    distinguia. Aqui a afirmação é sobre o que o código pede, que é o que
    estava errado.
    """

    gateway = GatewayFalso()
    gateway.respostas["token?grant_type=password"] = _resposta_sessao()
    gateway.respostas["token?grant_type=refresh_token"] = _resposta_sessao()
    gateway.respostas["admin/users"] = {"id": UID}

    executar(gateway)

    assert gateway.verbos == [verbo], f"{rotulo} usou {gateway.verbos}"
    assert gateway.chamadas[0][0] == caminho


def test_the_verb_table_covers_every_call_in_the_module() -> None:
    """A tabela acima precisa acompanhar o módulo, senão protege o passado.

    Uma função nova que fale com o gateway e não apareça na tabela reprova
    aqui — é o que evita descobrir o próximo verbo errado em produção.
    """

    import ast
    from pathlib import Path

    fonte = Path("english_leaderboard/supabase_auth.py").read_text()
    chamam_o_gateway = {
        fn.name
        for fn in ast.walk(ast.parse(fonte))
        if isinstance(fn, ast.FunctionDef)
        for no in ast.walk(fn)
        if isinstance(no, ast.Call)
        and isinstance(no.func, ast.Attribute)
        and no.func.attr in ("post", "put", "delete")
        and getattr(no.func.value, "id", "") == "gateway"
    }
    na_tabela = {c[0] for c in CHAMADAS_ESPERADAS}

    assert chamam_o_gateway - na_tabela == set(), (
        "funções que falam com o GoTrue e não têm verbo conferido: "
        f"{sorted(chamam_o_gateway - na_tabela)}"
    )


def test_account_removal_uses_delete() -> None:
    gateway = GatewayFalso()

    remover_conta(gateway, user_id=UID, chave_secreta=SECRETA)

    assert gateway.chamadas[0][0] == f"admin/users/{UID}"


def test_temporary_passwords_are_long_and_random() -> None:
    amostras = {gerar_senha_temporaria() for _ in range(20)}

    assert len(amostras) == 20
    assert all(len(s) == 16 for s in amostras)
    with pytest.raises(ValueError):
        gerar_senha_temporaria(8)


def test_no_password_hash_is_ever_produced_by_this_module() -> None:
    """A aplicação não guarda senha: essa autoridade é só do Supabase Auth."""

    import inspect

    from english_leaderboard import supabase_auth

    fonte = inspect.getsource(supabase_auth)
    for proibido in ("argon2", "PasswordHasher", "hash_password", "bcrypt"):
        assert proibido not in fonte
