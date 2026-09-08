"""Autenticação pelo Supabase Auth, com login por nome de usuário.

O Supabase Auth não tem provedor de username: ele autentica por e-mail. A
equipe decidiu manter o login por usuário, então a aplicação traduz
``ana.silva`` para ``ana.silva@{dominio}`` antes de falar com o serviço. O
aluno nunca vê esse endereço, e o ``profiles.username`` continua sendo a
identidade visível.

Consequência aceita: como nenhum endereço é real, não há recuperação de senha
por e-mail. Contas nascem confirmadas pela Admin API e a redefinição é feita
pelo administrador — o mesmo fluxo que a equipe já usava.

A aplicação não guarda hash de senha, não emite sessão própria e não escreve
token em log. O ``access_token`` circula em memória; o ``refresh_token`` só é
usado para renovar.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import string
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, runtime_checkable

LOGGER = logging.getLogger(__name__)

# Aceita o arroba porque contas migradas do modelo antigo têm um e-mail aqui.
USERNAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9._@-]{2,149}")


def normalize_username(value: str) -> str:
    """Normaliza e valida um nome de usuário de login.

    Aceita letras, dígitos e ``. _ - @`` em minúsculas. O arroba continua
    permitido para que contas migradas do antigo campo de e-mail sigam
    funcionando sem intervenção manual.
    """

    normalized = (value or "").strip().lower()
    if not USERNAME_PATTERN.fullmatch(normalized):
        raise ValueError(
            "Usuário deve ter de 3 a 150 caracteres e usar apenas letras, "
            "números, ponto, hífen, sublinhado ou arroba"
        )
    return normalized


TIMEOUT_SEGUNDOS = 20
# Margem para renovar antes de o token vencer de fato, evitando a corrida de
# usar um token que expira no meio da requisição.
MARGEM_RENOVACAO_SEGUNDOS = 60


class AuthError(RuntimeError):
    """Falha ao falar com o Supabase Auth."""


class CredenciaisInvalidas(AuthError):
    """Usuário ou senha incorretos, ou conta desativada."""


class SessaoExpirada(AuthError):
    """A sessão não é mais válida e não pôde ser renovada."""


@dataclass(frozen=True, slots=True)
class Sessao:
    user_id: str
    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    expires_at: datetime

    def __str__(self) -> str:  # pragma: no cover - proteção contra log acidental
        return f"Sessao(user_id={self.user_id}, expira={self.expires_at:%H:%M})"

    def expirada(self, agora: datetime | None = None) -> bool:
        referencia = agora or datetime.now(UTC)
        return self.expires_at <= referencia

    def precisa_renovar(self, agora: datetime | None = None) -> bool:
        referencia = agora or datetime.now(UTC)
        return self.expires_at <= referencia + timedelta(
            seconds=MARGEM_RENOVACAO_SEGUNDOS
        )


@runtime_checkable
class AuthGateway(Protocol):
    """Superfície do Supabase Auth, para os testes injetarem um duplo."""

    def post(
        self,
        caminho: str,
        corpo: dict[str, Any],
        *,
        chave: str,
        autorizacao: str | None = None,
    ) -> dict[str, Any]: ...

    def put(
        self,
        caminho: str,
        corpo: dict[str, Any],
        *,
        chave: str,
        autorizacao: str | None = None,
    ) -> dict[str, Any]: ...

    def delete(
        self, caminho: str, *, chave: str, autorizacao: str | None = None
    ) -> dict[str, Any]: ...


class HttpAuthGateway:
    def __init__(self, base_url: str) -> None:
        self._base = base_url.rstrip("/")

    def _requisitar(
        self,
        caminho: str,
        corpo: dict[str, Any] | None,
        metodo: str,
        chave: str,
        autorizacao: str | None = None,
    ) -> dict[str, Any]:
        """Monta a requisição. Os dois cabeçalhos respondem a perguntas distintas.

        ``apikey`` diz *qual projeto* está falando, e o GoTrue só aceita ali uma
        chave de API — a publishable ou a secret. ``Authorization`` diz *quem*
        age: a mesma chave, nas operações anônimas e administrativas, ou o token
        do próprio usuário, quando a operação é dele.

        Enquanto os dois carregavam o mesmo valor, passar o token de um aluno
        punha um JWT no lugar da chave de API, e o GoTrue recusava com
        ``Invalid API key`` antes sequer de olhar o token.
        """

        url = f"{self._base}/auth/v1/{caminho.lstrip('/')}"
        dados = json.dumps(corpo).encode("utf-8") if corpo is not None else None
        requisicao = urllib.request.Request(url, data=dados, method=metodo)
        requisicao.add_header("apikey", chave)
        requisicao.add_header("Authorization", f"Bearer {autorizacao or chave}")
        requisicao.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(
                requisicao, timeout=TIMEOUT_SEGUNDOS
            ) as resposta:
                bruto = resposta.read()
                return json.loads(bruto) if bruto else {}
        except urllib.error.HTTPError as erro:
            detalhe = erro.read().decode("utf-8", "replace")[:300]
            # 422 é o que o GoTrue devolve para endereço já registrado, e é
            # o caso comum ao renomear alguém para um usuário que já existe.
            # Sem ele a tela mostraria "Supabase Auth respondeu 422".
            if erro.code in (400, 401, 403, 422):
                raise CredenciaisInvalidas(_mensagem_amigavel(detalhe)) from erro
            raise AuthError(f"Supabase Auth respondeu {erro.code}") from erro
        except urllib.error.URLError as erro:  # pragma: no cover - rede indisponível
            raise AuthError("Não foi possível falar com o Supabase Auth") from erro

    def post(
        self,
        caminho: str,
        corpo: dict[str, Any],
        *,
        chave: str,
        autorizacao: str | None = None,
    ) -> dict[str, Any]:
        return self._requisitar(caminho, corpo, "POST", chave, autorizacao)

    def put(
        self,
        caminho: str,
        corpo: dict[str, Any],
        *,
        chave: str,
        autorizacao: str | None = None,
    ) -> dict[str, Any]:
        return self._requisitar(caminho, corpo, "PUT", chave, autorizacao)

    def delete(
        self, caminho: str, *, chave: str, autorizacao: str | None = None
    ) -> dict[str, Any]:
        return self._requisitar(caminho, None, "DELETE", chave, autorizacao)


def _mensagem_amigavel(detalhe: str) -> str:
    """Traduz a resposta sem repetir o corpo, que pode conter dado sensível."""

    baixo = detalhe.lower()
    if "invalid login" in baixo or "invalid_grant" in baixo:
        return "Usuário ou senha incorretos"
    if "already been registered" in baixo or "already exists" in baixo:
        return "Já existe uma conta com esse usuário"
    if "weak" in baixo or "password" in baixo and "short" in baixo:
        return "Senha recusada pelo servidor de autenticação"
    if "invalid api key" in baixo:
        # Defeito de configuração, não credencial de quem está na tela. Dizer
        # "autenticação recusada" mandava o usuário conferir a própria senha —
        # foi o que atrasou o diagnóstico deste erro.
        return (
            "Configuração de acesso ao Supabase inválida. "
            "Procure o administrador; não é a sua senha."
        )
    return "Autenticação recusada"


def username_para_email(username: str, dominio: str) -> str:
    """Traduz o usuário digitado no endereço interno do Supabase Auth."""

    normalizado = normalize_username(username)
    if "@" in normalizado:
        # Contas migradas do modelo antigo já têm um endereço no username.
        return normalizado
    limpo = (dominio or "").strip().lower().lstrip("@")
    if not limpo or "." not in limpo:
        raise AuthError("SUPABASE_USERNAME_DOMAIN inválido")
    return f"{normalizado}@{limpo}"


def _sessao_de(resposta: dict[str, Any], agora: datetime | None = None) -> Sessao:
    acesso = resposta.get("access_token")
    atualizacao = resposta.get("refresh_token")
    usuario = (resposta.get("user") or {}).get("id")
    if not acesso or not usuario:
        raise AuthError("Resposta de autenticação incompleta")
    referencia = agora or datetime.now(UTC)
    duracao = int(resposta.get("expires_in") or 3600)
    return Sessao(
        user_id=str(usuario),
        access_token=str(acesso),
        refresh_token=str(atualizacao or ""),
        expires_at=referencia + timedelta(seconds=duracao),
    )


def entrar(
    gateway: AuthGateway,
    *,
    username: str,
    password: str,
    chave_publica: str,
    dominio: str,
    agora: datetime | None = None,
) -> Sessao:
    """Autentica e devolve a sessão. Nada é persistido pela aplicação."""

    if not password:
        raise CredenciaisInvalidas("Informe a senha")
    email = username_para_email(username, dominio)
    resposta = gateway.post(
        "token?grant_type=password",
        {"email": email, "password": password},
        chave=chave_publica,
    )
    return _sessao_de(resposta, agora)


def renovar(
    gateway: AuthGateway,
    *,
    refresh_token: str,
    chave_publica: str,
    agora: datetime | None = None,
) -> Sessao:
    """Troca o refresh token por uma sessão nova."""

    if not refresh_token:
        raise SessaoExpirada("Sessão sem token de renovação")
    try:
        resposta = gateway.post(
            "token?grant_type=refresh_token",
            {"refresh_token": refresh_token},
            chave=chave_publica,
        )
    except CredenciaisInvalidas as erro:
        raise SessaoExpirada("Sessão expirada, entre novamente") from erro
    return _sessao_de(resposta, agora)


def sair(gateway: AuthGateway, *, access_token: str, chave_publica: str) -> None:
    """Revoga a sessão no servidor. Falha aqui não deixa o usuário preso."""

    if not access_token:
        return
    try:
        gateway.post("logout", {}, chave=chave_publica, autorizacao=access_token)
    except AuthError:
        # Não relançar continua certo: a sessão local é descartada de qualquer
        # forma, e insistir só impediria o logout de acontecer. Uma sessão já
        # expirada recusa aqui, e isso é esperado.
        #
        # O que estava errado era o silêncio. Este `except` escondeu por semanas
        # que a chamada nunca funcionava — o `apikey` ia com o token do usuário
        # e o GoTrue recusava tudo. Nenhuma sessão era revogada e ninguém tinha
        # como saber. Agora a falha fica no log, sem virar erro de tela.
        LOGGER.warning(
            "Logout não revogou a sessão no Supabase Auth; "
            "a sessão local foi descartada assim mesmo.",
            exc_info=True,
        )
        return


def gerar_senha_temporaria(tamanho: int = 16) -> str:
    """Senha aleatória entregue uma única vez, trocada no primeiro acesso."""

    if tamanho < 12:
        raise ValueError("Senha temporária deve ter ao menos 12 caracteres")
    alfabeto = string.ascii_letters + string.digits + "!@#$%"
    return "".join(secrets.choice(alfabeto) for _ in range(tamanho))


def criar_conta(
    gateway: AuthGateway,
    *,
    username: str,
    display_name: str,
    chave_secreta: str,
    dominio: str,
    password: str | None = None,
) -> tuple[str, str]:
    """Cria a conta já confirmada e devolve ``(user_id, senha temporária)``.

    Operação administrativa: usa a chave privilegiada de propósito, porque
    criar usuário no Auth exige isso. O chamador precisa ter verificado que o
    ator é administrador **antes** de chamar.

    Não existe cadastro público: a criação de contas passa só por aqui.
    """

    email = username_para_email(username, dominio)
    senha = password or gerar_senha_temporaria()
    resposta = gateway.post(
        "admin/users",
        {
            "email": email,
            "password": senha,
            # Sem endereço real não há como confirmar por link; a conta já
            # nasce confirmada, o que é seguro porque não há autocadastro.
            "email_confirm": True,
            "user_metadata": {"display_name": display_name},
        },
        chave=chave_secreta,
    )
    identificador = resposta.get("id")
    if not identificador:
        raise AuthError("Supabase Auth não devolveu o id da conta criada")
    return str(identificador), senha


def atualizar_username(
    gateway: AuthGateway,
    *,
    user_id: str,
    username: str,
    chave_secreta: str,
    dominio: str,
) -> str:
    """Move o endereço interno da conta para acompanhar o username.

    O login não guarda o username: ele o traduz em endereço a cada tentativa,
    por ``username_para_email``. Mudar só ``profiles.username`` deixa a conta
    acessível apenas pelo nome antigo, e a tela passa a mostrar um valor que
    não serve para entrar.

    ``email_confirm`` volta aqui pelo mesmo motivo da criação: trocar o
    endereço reabre a confirmação, e endereço interno não recebe link.

    Devolve o endereço novo, para quem chamar poder registrá-lo.
    """

    email = username_para_email(username, dominio)
    gateway.put(
        f"admin/users/{user_id}",
        {"email": email, "email_confirm": True},
        chave=chave_secreta,
    )
    return email


def redefinir_senha(
    gateway: AuthGateway,
    *,
    user_id: str,
    chave_secreta: str,
    password: str | None = None,
) -> str:
    """Define uma senha temporária nova. Substitui a recuperação por e-mail.

    Como os endereços são internos, ninguém recebe link de redefinição. O
    administrador gera a senha e a entrega pessoalmente.
    """

    senha = password or gerar_senha_temporaria()
    gateway.put(
        f"admin/users/{user_id}",
        {"password": senha},
        chave=chave_secreta,
    )
    return senha


def trocar_senha(
    gateway: AuthGateway,
    *,
    access_token: str,
    nova_senha: str,
    chave_publica: str,
) -> None:
    """Troca a senha do próprio usuário, com o token dele.

    ``chave_publica`` vai no ``apikey`` e o token no ``Authorization``: são
    coisas diferentes, e mandar o token nos dois fazia o GoTrue recusar com
    ``Invalid API key`` antes de olhar a senha. A chave privilegiada não
    participa — a troca é do próprio usuário.
    """

    if not nova_senha:
        raise ValueError("Informe a nova senha")
    if not access_token:
        raise SessaoExpirada("Sessão ausente")
    gateway.post(
        "user",
        {"password": nova_senha},
        chave=chave_publica,
        autorizacao=access_token,
    )


def desativar_conta(
    gateway: AuthGateway, *, user_id: str, chave_secreta: str
) -> None:
    """Revoga o acesso banindo a conta no Auth.

    A revogação precisa acontecer no Supabase: desativar só o perfil deixaria
    a sessão em curso continuar válida até expirar.

    ``PUT`` é o verbo documentado para atualizar conta. O GoTrue também roteia
    ``POST`` no mesmo caminho — foi assim que esta função nasceu, e ela não
    estava quebrada por causa disso —, mas manter um verbo só evita a dúvida
    de qual dos dois vale.
    """

    gateway.put(
        f"admin/users/{user_id}",
        {"ban_duration": "876000h"},
        chave=chave_secreta,
    )


def reativar_conta(
    gateway: AuthGateway, *, user_id: str, chave_secreta: str
) -> None:
    """Levanta o ban imposto por :func:`desativar_conta`.

    Marcar o perfil como ativo de novo não devolve o acesso: quem recusa o
    login é o Auth, e para ele a conta continua banida. Sem esta chamada o
    aluno reativado aparece ativo na tela e não entra mais — sintoma nenhum
    até ele tentar.

    ``"none"`` é como o GoTrue expressa "sem ban"; não é o mesmo que omitir o
    campo, que deixaria o ban de pé.
    """

    gateway.put(
        f"admin/users/{user_id}",
        {"ban_duration": "none"},
        chave=chave_secreta,
    )


def remover_conta(gateway: AuthGateway, *, user_id: str, chave_secreta: str) -> None:
    """Remove a conta do Auth. O perfil sai por cascade no PostgreSQL."""

    gateway.delete(f"admin/users/{user_id}", chave=chave_secreta)


__all__ = [
    "MARGEM_RENOVACAO_SEGUNDOS",
    "AuthError",
    "AuthGateway",
    "CredenciaisInvalidas",
    "HttpAuthGateway",
    "Sessao",
    "SessaoExpirada",
    "atualizar_username",
    "criar_conta",
    "desativar_conta",
    "entrar",
    "gerar_senha_temporaria",
    "normalize_username",
    "reativar_conta",
    "redefinir_senha",
    "remover_conta",
    "renovar",
    "sair",
    "trocar_senha",
    "username_para_email",
]
