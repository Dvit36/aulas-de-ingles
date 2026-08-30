"""A tela de arquivos entrega URL assinada, e só quando o arquivo é pedido.

Antes, `_render_submission_files` chamava `get_submission_file_for_user`, que
baixa os bytes inteiros pelo servidor do Streamlit. O laço que a invoca roda
dentro de um `st.expander(expanded=False)`, e o Streamlit executa o corpo do
expander mesmo fechado: abrir o histórico baixava todos os arquivos de todas as
submissões da página, a cada rerun, debitando o egress de cada um.

São duas garantias distintas e as duas precisam de teste próprio, porque
consertar uma sem a outra só muda a forma do custo:

* os bytes não passam mais pelo servidor — `gateway.download()` não é chamado;
* a URL só é emitida quando alguém pede o arquivo, e é reaproveitada enquanto
  vale — assinar já debita o tamanho do objeto no orçamento de egress.

E a autorização continua vindo antes da assinatura: não se assina URL para
arquivo que o usuário não pode ver.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

import streamlit_app
from english_leaderboard.authz import AuthorizationError
from english_leaderboard.schema import (
    Activity,
    Role,
    Submission,
    SubmissionFile,
    User,
    new_id,
)
from english_leaderboard.services import (
    UploadPayload,
    get_submission_file_url_for_user,
    submit_evidence,
)

RESUMO = (
    "Eu aprendi novas palavras e revisei exemplos importantes para a equipe. "
    "Também escrevi anotações em português para praticar o conteúdo depois "
    "e compartilhar o aprendizado com meus colegas durante o treinamento."
)


class GatewayEspiao:
    """Registra as chamadas e trata `download` como a regressão sob teste."""

    def __init__(self, real) -> None:
        self._real = real
        self.chamadas: list[str] = []

    def upload(self, key: str, data: bytes, content_type: str) -> None:
        self.chamadas.append("upload")
        self._real.upload(key, data, content_type)

    def download(self, key: str) -> bytes:
        self.chamadas.append("download")
        raise AssertionError(
            "gateway.download() foi chamado ao renderizar os arquivos: os "
            "bytes voltaram a passar pelo servidor do Streamlit. A entrega "
            "tem de ser por URL assinada (url_temporaria)."
        )

    def remove(self, key: str) -> None:
        self.chamadas.append("remove")
        self._real.remove(key)

    def exists(self, key: str) -> bool:
        return self._real.exists(key)

    def signed_url(self, key: str, expires_in: int) -> str:
        self.chamadas.append("signed_url")
        return self._real.signed_url(key, expires_in)

    def assinaturas(self) -> int:
        return self.chamadas.count("signed_url")


class StFalso:
    """Superfície mínima do Streamlit usada por `_render_submission_files`."""

    def __init__(self, *, clicar: bool) -> None:
        self.session_state: dict[str, object] = {}
        self._clicar = clicar
        self.botoes: list[str] = []
        self.imagens: list[str] = []
        self.links: list[tuple[str, str]] = []
        self.legendas: list[str] = []
        self.avisos: list[str] = []
        self.erros: list[str] = []

    def button(self, label: str, **_: object) -> bool:
        self.botoes.append(label)
        return self._clicar

    def image(self, source, **_: object) -> None:
        self.imagens.append(source)

    def link_button(self, label: str, url: str, **_: object) -> None:
        self.links.append((label, url))

    def caption(self, texto: str, **_: object) -> None:
        self.legendas.append(texto)

    def warning(self, texto: str, **_: object) -> None:
        self.avisos.append(texto)

    def error(self, texto: str, **_: object) -> None:
        self.erros.append(texto)


@pytest.fixture
def enviado(session, users, settings, gateway):
    """Uma submissão com um arquivo, e o espião que observa o Storage."""

    espiao = GatewayEspiao(gateway)
    activity = session.scalar(
        select(Activity).where(Activity.code == "impact_summary")
    )
    resultado = submit_evidence(
        session,
        gateway=espiao,
        actor=users[Role.STUDENT],
        activity_id=activity.id,
        uploads=[UploadPayload("evidencia.txt", b"conteudo textual seguro")],
        settings=settings,
        title="Atividade Impact",
        summary=RESUMO,
    )
    session.commit()
    submission = session.get(Submission, resultado.submission_id)
    espiao.chamadas.clear()  # o envio já usou o gateway; o que interessa é a leitura
    return submission, espiao


def _renderizar(monkeypatch, st_falso, espiao, session, actor, submission, settings):
    monkeypatch.setattr(streamlit_app, "st", st_falso)
    monkeypatch.setattr(streamlit_app, "_storage_gateway", lambda _s: espiao)
    streamlit_app._render_submission_files(session, actor, submission, settings)


def test_renderizar_arquivos_nao_baixa_os_bytes_pelo_servidor(
    enviado, session, users, settings, monkeypatch
) -> None:
    """A regressão que este módulo existe para impedir.

    Se `_render_submission_files` voltar a chamar `get_submission_file_for_user`
    — ou qualquer caminho que use `gateway.download()` —, o espião levanta
    AssertionError e este teste falha.
    """

    submission, espiao = enviado
    st_falso = StFalso(clicar=True)

    _renderizar(
        monkeypatch,
        st_falso,
        espiao,
        session,
        users[Role.STUDENT],
        submission,
        settings,
    )

    assert "download" not in espiao.chamadas
    assert espiao.assinaturas() == 1
    assert len(st_falso.links) == 1
    _, url = st_falso.links[0]
    assert url.startswith("https://")


def test_a_url_so_e_emitida_quando_o_arquivo_e_pedido(
    enviado, session, users, settings, monkeypatch
) -> None:
    """O corpo do expander roda mesmo fechado; o botão é o que gasta.

    Sem o botão, abrir o histórico assinaria URL para todos os arquivos de
    todas as submissões da página — e assinar já debita o tamanho do objeto no
    orçamento de egress.
    """

    submission, espiao = enviado
    st_falso = StFalso(clicar=False)

    _renderizar(
        monkeypatch,
        st_falso,
        espiao,
        session,
        users[Role.STUDENT],
        submission,
        settings,
    )

    assert espiao.chamadas == [], "nada pode ir ao Storage antes do clique"
    assert st_falso.botoes, "o arquivo precisa ficar atrás de um controle explícito"
    assert st_falso.links == []


def test_a_url_e_reaproveitada_entre_reruns(
    enviado, session, users, settings, monkeypatch
) -> None:
    """Cada assinatura debita o objeto no egress: repetir por rerun é o mesmo custo."""

    submission, espiao = enviado
    st_falso = StFalso(clicar=True)
    aluno = users[Role.STUDENT]

    for _ in range(3):
        _renderizar(
            monkeypatch, st_falso, espiao, session, aluno, submission, settings
        )

    assert espiao.assinaturas() == 1, "a URL válida tem de ser reaproveitada"
    assert len(st_falso.links) == 3, "o link continua sendo renderizado a cada run"


def test_a_autorizacao_vem_antes_da_assinatura(
    enviado, session, users, settings, gateway
) -> None:
    """Não se assina URL para arquivo de outro aluno.

    A recusa vem de `require_submission_access`, na camada de serviço, e o
    Storage não chega a ser tocado. Abaixo dela, `url_temporaria` ainda passa
    por `resolver_arquivo_autorizado`, que aplica `ensure_owned_key` sobre a
    chave física — as duas barreiras continuam no caminho.
    """

    _, espiao = enviado
    stored_file = session.scalar(select(SubmissionFile))
    intruso = User(
        id=new_id(),
        username="outro-aluno",
        display_name="Outro",
        role=Role.STUDENT,
        active=True,
    )
    session.add(intruso)
    session.commit()

    with pytest.raises(AuthorizationError):
        get_submission_file_url_for_user(
            session,
            actor=intruso,
            file_id=stored_file.id,
            settings=settings,
            gateway=espiao,
        )

    assert espiao.assinaturas() == 0, "a URL não pode ser assinada antes de autorizar"


def test_ensure_owned_key_continua_barrando_chave_de_outro_dono(
    enviado, session, users, settings
) -> None:
    """A chave gravada é conferida contra o dono, não aceita como veio.

    Se a linha apontasse para a pasta de outro aluno — chave adulterada no
    banco —, `ensure_owned_key` recusa antes de qualquer assinatura.
    """

    from english_leaderboard.storage import StorageKeyError, ensure_owned_key
    from english_leaderboard.storage_service import (
        ArquivoNaoAutorizado,
        resolver_arquivo_autorizado,
    )

    _, espiao = enviado
    stored_file = session.scalar(select(SubmissionFile))
    aluno = users[Role.STUDENT]

    # A chave do próprio dono passa; a de um estranho, não.
    assert ensure_owned_key(stored_file.storage_key, aluno.id)
    with pytest.raises(StorageKeyError):
        ensure_owned_key(stored_file.storage_key, new_id())

    with pytest.raises(ArquivoNaoAutorizado):
        resolver_arquivo_autorizado(
            session.connection(),
            file_id=stored_file.id,
            student_id=new_id(),
        )
    assert espiao.assinaturas() == 0
