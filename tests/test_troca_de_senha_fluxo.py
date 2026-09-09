"""O caminho que o aluno percorre no primeiro acesso, do clique ao Auth.

Os testes da trava (`test_troca_obrigatoria_senha.py`) provam que a rota é
desviada, que a barra some e que quem já existia navega. **Nenhum deles chega
a submeter o formulário** — medido com um espião: a suíte inteira chamava
`trocar_senha` zero vezes, em 291 testes.

Foi essa lacuna que deixou passar dois defeitos seguidos nesta tela: o `apikey`
levando o token do usuário, e depois um erro de assinatura na própria chamada.
Os dois estavam a um clique de distância da cobertura.

Aqui o duplo de `st` **clica de verdade**, e o duplo do gateway confere o que
saiu — inclusive os cabeçalhos, que é onde o primeiro defeito morava.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest

import streamlit_app
from english_leaderboard.schema import Role, User
from english_leaderboard.supabase_auth import AuthError, Sessao

TOKEN = "token-de-acesso-do-aluno"
PUBLICA = "sb_publishable_de_teste"


class GatewayEspiao:
    """Guarda caminho, corpo e **as duas credenciais** de cada requisição."""

    def __init__(self, erro: Exception | None = None) -> None:
        self.erro = erro
        self.chamadas: list[dict] = []

    def _registrar(self, verbo, caminho, corpo, chave, autorizacao):
        self.chamadas.append(
            {
                "verbo": verbo,
                "caminho": caminho,
                "corpo": corpo,
                "apikey": chave,
                "autorizacao": autorizacao or chave,
            }
        )
        if self.erro is not None:
            raise self.erro
        return {}

    def post(self, caminho, corpo, *, chave, autorizacao=None):
        return self._registrar("POST", caminho, corpo, chave, autorizacao)

    def put(self, caminho, corpo, *, chave, autorizacao=None):
        return self._registrar("PUT", caminho, corpo, chave, autorizacao)

    def delete(self, caminho, *, chave, autorizacao=None):
        return self._registrar("DELETE", caminho, {}, chave, autorizacao)


class StQueClica:
    """`st` que preenche as senhas e aperta o botão pedido."""

    def __init__(self, *, senhas: dict[str, str], clicar: str | None) -> None:
        self.senhas = senhas
        self.clicar = clicar
        self.session_state: dict = {}
        self.erros: list[str] = []
        self.sucessos: list[str] = []

    def error(self, texto, **_):
        self.erros.append(str(texto))

    def success(self, texto, **_):
        self.sucessos.append(str(texto))

    def warning(self, *_a, **_k):
        pass

    def header(self, *_a, **_k):
        pass

    def text_input(self, rotulo, **_):
        # "Confirmar nova senha" contém "nova senha": a confirmação vem antes,
        # senão os dois campos devolvem o mesmo valor e o teste de senhas
        # divergentes nunca divergiria.
        baixo = rotulo.lower()
        if "confirmar" in baixo:
            return self.senhas["confirmacao"]
        if "nova senha" in baixo:
            return self.senhas["nova"]
        return ""

    def form_submit_button(self, rotulo, **_):
        return rotulo == self.clicar

    def button(self, rotulo, **_):
        return rotulo == self.clicar

    def checkbox(self, *_a, **_k):
        return False

    def selectbox(self, _rotulo, opcoes=(), **_):
        opcoes = list(opcoes)
        return opcoes[0] if opcoes else None

    def columns(self, quantidade, **_):
        n = quantidade if isinstance(quantidade, int) else len(quantidade)
        return [self for _ in range(n)]

    def write(self, *_a, **_k):
        pass

    @contextmanager
    def _bloco(self, *_a, **_k):
        yield self

    form = expander = container = _bloco

    def __getattr__(self, _nome):
        return lambda *a, **k: None


class SettingsFalso:
    supabase_url = "https://projeto.supabase.co"
    supabase_publishable_key = PUBLICA


@pytest.fixture
def tela(monkeypatch):
    """Monta a tela com um gateway espião e um `st` que clica."""

    def montar(*, senhas, clicar, erro_do_auth=None):
        espiao = GatewayEspiao(erro=erro_do_auth)
        falso = StQueClica(senhas=senhas, clicar=clicar)
        falso.session_state[streamlit_app.SESSAO_KEY] = Sessao(
            user_id="11111111-1111-1111-1111-111111111111",
            access_token=TOKEN,
            refresh_token="refresh",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        monkeypatch.setattr(streamlit_app, "st", falso)
        monkeypatch.setattr(streamlit_app, "_auth_gateway", lambda _url: espiao)
        return falso, espiao

    return montar


def _aluno_marcado(session, users) -> User:
    alvo = users[Role.STUDENT]
    alvo.must_change_password = True
    session.flush()
    return alvo


# ------------------------------------------------- 1. a chamada acontece

def test_the_locked_screen_actually_calls_the_auth_service(
    session, users, tela
) -> None:
    """O teste que faltava: submeter e ver a requisição sair.

    Um erro de assinatura em `trocar_senha` — argumento a mais, a menos ou
    renomeado — estoura aqui. Antes disto, nada na suíte chegava a executar
    esta linha, e o erro só aparecia com um aluno na frente da tela.
    """

    alvo = _aluno_marcado(session, users)
    falso, espiao = tela(
        senhas={"nova": "SenhaNova!123", "confirmacao": "SenhaNova!123"},
        clicar="Trocar senha e continuar",
    )

    streamlit_app._troca_obrigatoria_view(session, SettingsFalso(), alvo)

    assert len(espiao.chamadas) == 1, "o formulário não chegou a falar com o Auth"
    assert espiao.chamadas[0]["caminho"] == "user"
    assert espiao.chamadas[0]["corpo"] == {"password": "SenhaNova!123"}
    assert falso.erros == []


# --------------------------------------- 2. os cabeçalhos, nesta tela

def test_the_locked_screen_separates_the_api_key_from_the_user_token(
    session, users, tela
) -> None:
    """O defeito do `apikey` chegava até aqui, e a camada HTTP sozinha não bastou.

    O teste de `test_supabase_auth.py` cobre `trocar_senha` chamada à mão. Este
    cobre a tela chamando: se alguém trocar a ordem dos argumentos ou passar o
    token no lugar da chave, é aqui que aparece.
    """

    alvo = _aluno_marcado(session, users)
    _, espiao = tela(
        senhas={"nova": "SenhaNova!123", "confirmacao": "SenhaNova!123"},
        clicar="Trocar senha e continuar",
    )

    streamlit_app._troca_obrigatoria_view(session, SettingsFalso(), alvo)

    chamada = espiao.chamadas[0]
    assert chamada["apikey"] == PUBLICA
    assert chamada["autorizacao"] == TOKEN
    assert chamada["apikey"] != TOKEN, "o token do aluno não é chave de API"


# ------------------------ 3. a marca só cai depois de o Auth confirmar

def test_a_refused_change_leaves_the_lock_in_place(session, users, tela) -> None:
    """Se o Auth recusa, a senha temporária ainda vale — e a trava tem de ficar.

    A ordem inversa liberaria a navegação para quem continua com a senha que o
    administrador entregou em mãos.
    """

    alvo = _aluno_marcado(session, users)
    falso, espiao = tela(
        senhas={"nova": "SenhaNova!123", "confirmacao": "SenhaNova!123"},
        clicar="Trocar senha e continuar",
        erro_do_auth=AuthError("Supabase Auth respondeu 500"),
    )

    streamlit_app._troca_obrigatoria_view(session, SettingsFalso(), alvo)

    assert espiao.chamadas, "tentou falar com o Auth"
    assert falso.erros, "e disse ao aluno que não deu certo"
    assert session.get(User, alvo.id).must_change_password is True
    assert "password_changed_notice" not in falso.session_state


def test_a_confirmed_change_clears_the_mark(session, users, tela) -> None:
    alvo = _aluno_marcado(session, users)
    falso, _ = tela(
        senhas={"nova": "SenhaNova!123", "confirmacao": "SenhaNova!123"},
        clicar="Trocar senha e continuar",
    )

    streamlit_app._troca_obrigatoria_view(session, SettingsFalso(), alvo)
    session.commit()

    assert session.get(User, alvo.id).must_change_password is False
    assert falso.session_state["password_changed_notice"]


# ---------------- 3b. as falhas passageiras dizem que a senha não mudou

@pytest.mark.parametrize(
    ("codigo", "trecho"),
    [
        (429, "Muitas tentativas"),
        (503, "instável"),
    ],
)
def test_a_transient_failure_says_the_password_was_not_changed(
    session, users, tela, codigo, trecho
) -> None:
    """Numa troca obrigatória, não saber com qual senha entrar é o pior estado.

    `Supabase Auth respondeu 429` é verdade e não serve para ninguém: o aluno
    fica sem saber se a senha mudou, e a tela que deveria destravá-lo é a
    mesma que o deixa sem caminho. A mensagem precisa dizer, em primeiro
    lugar, que a senha atual continua valendo.

    429 acontece com uso normal — vários alunos entrando e trocando a senha no
    mesmo dia batem no limite do GoTrue —, e 5xx é instabilidade do serviço.
    Nenhum dos dois é erro de quem está na tela.
    """

    alvo = _aluno_marcado(session, users)
    falso, espiao = tela(
        senhas={"nova": "SenhaNova!123", "confirmacao": "SenhaNova!123"},
        clicar="Trocar senha e continuar",
        erro_do_auth=AuthError(f"resposta {codigo}", codigo=codigo),
    )

    streamlit_app._troca_obrigatoria_view(session, SettingsFalso(), alvo)

    assert espiao.chamadas, "chegou a tentar"
    assert len(falso.erros) == 1
    mensagem = falso.erros[0]
    assert "NÃO foi alterada" in mensagem, mensagem
    assert trecho in mensagem, mensagem
    # E o código cru não vaza para a tela do aluno.
    assert str(codigo) not in mensagem, mensagem

    # A trava continua de pé: a senha temporária ainda é a que vale.
    assert session.get(User, alvo.id).must_change_password is True
    assert "password_changed_notice" not in falso.session_state


# ------------------------------ 4. senha divergente não fala com o Auth

def test_mismatched_passwords_never_reach_the_auth_service(
    session, users, tela
) -> None:
    """Erro local não vira requisição — e a trava continua de pé."""

    alvo = _aluno_marcado(session, users)
    falso, espiao = tela(
        senhas={"nova": "SenhaNova!123", "confirmacao": "outra-coisa"},
        clicar="Trocar senha e continuar",
    )

    streamlit_app._troca_obrigatoria_view(session, SettingsFalso(), alvo)

    assert espiao.chamadas == []
    assert any("não coincidem" in e for e in falso.erros)
    assert session.get(User, alvo.id).must_change_password is True


# --------------------------------- 5. o account_view, o mesmo formulário

def test_the_account_view_change_password_also_reaches_the_auth_service(
    session, users, tela
) -> None:
    """Mesmo formulário, mesma função, e igualmente sem cobertura até agora.

    Lá a troca é voluntária e termina deslogando, o que é decisão deliberada —
    mas a requisição precisa sair igual.
    """

    _, espiao = tela(
        senhas={"nova": "SenhaNova!123", "confirmacao": "SenhaNova!123"},
        clicar="Alterar senha",
    )

    streamlit_app.account_view(session, users[Role.ADMIN], SettingsFalso())

    assert len(espiao.chamadas) == 1, "o account_view não falou com o Auth"
    chamada = espiao.chamadas[0]
    assert chamada["caminho"] == "user"
    assert chamada["apikey"] == PUBLICA
    assert chamada["autorizacao"] == TOKEN
