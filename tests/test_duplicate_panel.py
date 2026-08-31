"""O painel antifraude: alcançável, e entregue por URL assinada.

São dois defeitos empilhados, e consertar um sem o outro não devolve a
comparação ao revisor.

O primeiro é a consulta. Ela filtrava por `DuplicateMatch.image_id`, coluna que
deixou de existir quando `submission_images` e os documentos viraram a tabela
única `submission_files` — o campo passou a se chamar `file_id`. Como a chamada
ficou para trás, abrir o card de qualquer submissão com imagem no modo
administrador levantava AttributeError, e a exceção nascia acima do painel: nem
a comparação nem o formulário de aprovar/rejeitar logo abaixo renderizavam.

O segundo é a entrega. Atrás da exceção, o painel montava um caminho dentro de
`settings.upload_dir` a partir da `storage_key` e perguntava por `is_file()`.
Desde a migração a chave é do Supabase Storage e o objeto nunca toca o disco do
Streamlit, que ainda por cima é efêmero: a resposta era sempre falsa e as duas
colunas diziam "Imagem atual indisponível".

Nada cobria esse caminho — os testes de serviço exercitam a *criação* de
`DuplicateMatch`, nunca a leitura feita pela interface.
"""

from __future__ import annotations

import pathlib
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from conftest import FakeDuolingoOCR, make_png
from sqlalchemy import select

import streamlit_app
from english_leaderboard.schema import (
    Activity,
    DuplicateMatch,
    Role,
    Submission,
    SubmissionFile,
    SubmissionStatus,
    User,
    new_id,
)
from english_leaderboard.services import UploadPayload, submit_evidence

RESUMO = (
    "Eu aprendi novas palavras e revisei exemplos importantes para a equipe. "
    "Também escrevi anotações em português para praticar o conteúdo depois "
    "e compartilhar o aprendizado com meus colegas durante o treinamento."
)


class GatewayEspiao:
    """Conta as assinaturas e trata `download` como regressão.

    Assinar não move bytes, mas debita o tamanho do objeto no orçamento de
    egress: contar assinatura é contar custo.
    """

    def __init__(self, real) -> None:
        self._real = real
        self.chamadas: list[str] = []

    def upload(self, key: str, data: bytes, content_type: str) -> None:
        self.chamadas.append("upload")
        self._real.upload(key, data, content_type)

    def download(self, key: str) -> bytes:
        raise AssertionError(
            "gateway.download() foi chamado ao comparar as evidências: os "
            "bytes voltaram a passar pelo servidor do Streamlit."
        )

    def remove(self, key: str) -> None:
        self._real.remove(key)

    def exists(self, key: str) -> bool:
        return self._real.exists(key)

    def signed_url(self, key: str, expires_in: int) -> str:
        self.chamadas.append("signed_url")
        return self._real.signed_url(key, expires_in)

    def assinaturas(self) -> int:
        return self.chamadas.count("signed_url")


class StFalso:
    """Superfície do Streamlit usada por `_render_duplicate_matches`."""

    def __init__(self, *, clicar: bool) -> None:
        self.session_state: dict[str, object] = {}
        self._clicar = clicar
        self.botoes: list[str] = []
        self.imagens: list[str] = []
        self.legendas: list[str] = []
        self.avisos: list[str] = []
        self.textos: list[str] = []
        self.erros: list[str] = []

    def button(self, label: str, **_: object) -> bool:
        self.botoes.append(label)
        return self._clicar

    def image(self, source, **_: object) -> None:
        self.imagens.append(source)

    def caption(self, texto: str, **_: object) -> None:
        self.legendas.append(texto)

    def warning(self, texto: str, **_: object) -> None:
        self.avisos.append(texto)

    def write(self, texto: str, **_: object) -> None:
        self.textos.append(texto)

    def markdown(self, texto: str, **_: object) -> None:
        self.textos.append(texto)

    def error(self, texto: str, **_: object) -> None:
        self.erros.append(texto)

    @contextmanager
    def container(self, **_: object):
        yield self

    def columns(self, quantidade: int, **_: object) -> list:
        return [self._coluna() for _ in range(quantidade)]

    @contextmanager
    def _coluna(self):
        yield self


@pytest.fixture
def duplicidade(session, users, settings, gateway):
    """Dois alunos, a mesma imagem: a suspeita que o revisor precisa comparar.

    Devolve a submissão do segundo aluno — a que o administrador abre — porque
    o arquivo correspondente pertence a outro dono, e é isso que torna a
    autorização da assinatura interessante.
    """

    espiao = GatewayEspiao(gateway)
    activity = session.scalar(
        select(Activity).where(Activity.code == "duolingo_beconfident")
    )
    outro = User(
        id=new_id(),
        username="outro",
        display_name="Outro",
        role=Role.STUDENT,
        active=True,
    )
    session.add(outro)
    session.commit()

    imagem = make_png(15)
    submit_evidence(
        session,
        gateway=espiao,
        actor=users[Role.STUDENT],
        activity_id=activity.id,
        uploads=[UploadPayload("original.png", imagem)],
        settings=settings,
        ocr_engine=FakeDuolingoOCR(),
    )
    session.commit()
    resultado = submit_evidence(
        session,
        gateway=espiao,
        actor=outro,
        activity_id=activity.id,
        uploads=[UploadPayload("copia.png", imagem)],
        settings=settings,
        ocr_engine=FakeDuolingoOCR(),
    )
    session.commit()

    submission = session.get(Submission, resultado.submission_id)
    assert submission.status == SubmissionStatus.REJECTED
    espiao.chamadas.clear()  # o envio já usou o Storage; interessa a leitura
    return submission, espiao


def _indisponiveis(st_falso: StFalso) -> list[str]:
    """Só os avisos de coluna vazia; o cabeçalho do painel também é um warning."""

    return [aviso for aviso in st_falso.avisos if "indisponível" in aviso]


def _comparar(monkeypatch, st_falso, espiao, session, actor, submission, settings):
    """Roda o painel com o Streamlit e o Storage substituídos por duplos.

    `Path.is_file` também é minado: qualquer sondagem ao disco durante a
    renderização é a regressão, venha ela de `upload_dir` ou de outro lugar.
    """

    def nao_sondar(self):
        raise AssertionError(
            f"o painel perguntou ao filesystem por {self!r}. O arquivo mora no "
            "Supabase Storage e é entregue por URL assinada."
        )

    monkeypatch.setattr(pathlib.Path, "is_file", nao_sondar)
    monkeypatch.setattr(streamlit_app, "st", st_falso)
    monkeypatch.setattr(streamlit_app, "_storage_gateway", lambda _s: espiao)
    streamlit_app._render_duplicate_matches(
        session,
        actor,
        submission,
        streamlit_app._duplicate_matches(session, submission),
        settings,
    )


# --- o painel precisa existir antes de entregar qualquer coisa ---------------


def test_a_consulta_do_painel_nao_usa_coluna_que_nao_existe(session, duplicidade):
    """Se a consulta voltar a filtrar por `image_id`, isto levanta
    AttributeError — exatamente como a interface levantava em produção."""

    submission, _ = duplicidade

    matches = streamlit_app._duplicate_matches(session, submission)

    assert matches, "a suspeita gravada tem de chegar ao painel"
    assert all(isinstance(match, DuplicateMatch) for match in matches)


def test_o_painel_so_recebe_as_suspeitas_da_submissao_aberta(session, duplicidade):
    """`file_id` é o lado atual da comparação; `matched_file_id` é o antigo.

    Filtrar pelo campo errado traria a suspeita do envio original para dentro
    do card do reenvio, invertendo as duas colunas.
    """

    submission, _ = duplicidade
    ids_do_envio = {arquivo.id for arquivo in submission.images}

    matches = streamlit_app._duplicate_matches(session, submission)

    assert {match.file_id for match in matches} <= ids_do_envio
    assert all(match.matched_file_id not in ids_do_envio for match in matches)


def test_submissao_sem_imagem_nao_consulta_duplicidade(
    session, users, settings, gateway
):
    """Sem imagem não há o que comparar, e a consulta nem precisa sair."""

    activity = session.scalar(select(Activity).where(Activity.code == "impact_summary"))
    resultado = submit_evidence(
        session,
        gateway=gateway,
        actor=users[Role.STUDENT],
        activity_id=activity.id,
        uploads=[UploadPayload("nota.txt", b"conteudo textual seguro")],
        settings=settings,
        title="Atividade Impact",
        summary=RESUMO,
    )
    session.commit()
    submission = session.get(Submission, resultado.submission_id)

    assert streamlit_app._duplicate_matches(session, submission) == []


# --- e entregar por URL assinada, não por caminho em disco -------------------


def test_a_comparacao_nao_monta_caminho_de_filesystem(
    duplicidade, session, users, settings, monkeypatch
):
    """A regressão central do Item 1.

    `Path.is_file` é minado durante a renderização: qualquer sondagem ao disco
    derruba o teste. E `Settings` não tem mais `upload_dir` — o diretório de
    uploads foi removido justamente porque estas duas linhas eram o último
    consumidor dele —, então nem existe de onde partir um caminho.
    """

    submission, espiao = duplicidade
    st_falso = StFalso(clicar=True)
    assert not hasattr(settings, "upload_dir"), (
        "voltou a existir um diretório de uploads em Settings; o disco do "
        "Streamlit é efêmero e não guarda arquivo de aluno"
    )

    _comparar(
        monkeypatch,
        st_falso,
        espiao,
        session,
        users[Role.ADMIN],
        submission,
        settings,
    )

    assert len(st_falso.imagens) == 2, "as duas colunas têm de mostrar imagem"
    assert all(fonte.startswith("https://") for fonte in st_falso.imagens)
    assert espiao.assinaturas() == 2
    assert _indisponiveis(st_falso) == [], "nenhuma coluna pode ficar sem imagem"


def test_a_comparacao_so_assina_quando_o_revisor_pede(
    duplicidade, session, users, settings, monkeypatch
):
    """Cada suspeita custa duas assinaturas, e o corpo do expander roda mesmo
    fechado: sem o botão, abrir a fila pagaria por todas as comparações."""

    submission, espiao = duplicidade
    st_falso = StFalso(clicar=False)

    _comparar(
        monkeypatch,
        st_falso,
        espiao,
        session,
        users[Role.ADMIN],
        submission,
        settings,
    )

    assert espiao.chamadas == [], "nada pode ir ao Storage antes do clique"
    assert st_falso.botoes, "a comparação precisa ficar atrás de um controle"
    assert st_falso.imagens == []


def test_as_urls_sao_reaproveitadas_entre_reruns(
    duplicidade, session, users, settings, monkeypatch
):
    """Duas imagens por suspeita dobram o custo de reassinar a cada rerun."""

    submission, espiao = duplicidade
    st_falso = StFalso(clicar=True)
    admin = users[Role.ADMIN]

    for _ in range(3):
        _comparar(
            monkeypatch, st_falso, espiao, session, admin, submission, settings
        )

    assert espiao.assinaturas() == 2, "as URLs válidas têm de ser reaproveitadas"
    assert len(st_falso.imagens) == 6, "a comparação segue renderizando a cada run"


def test_url_vencida_no_cache_e_reassinada(
    duplicidade, session, users, settings, monkeypatch
):
    """Reaproveitar não pode virar entregar link morto.

    Uma URL guardada que passou da validade é reassinada; a vencida não chega
    à tela.
    """

    submission, espiao = duplicidade
    st_falso = StFalso(clicar=True)
    vencida = datetime.now(UTC) - timedelta(seconds=1)
    st_falso.session_state[streamlit_app.URLS_ASSINADAS_KEY] = {
        arquivo.id: (vencida, "https://vencida.invalid/nao-usar")
        for arquivo in session.scalars(select(SubmissionFile)).all()
    }

    _comparar(
        monkeypatch,
        st_falso,
        espiao,
        session,
        users[Role.ADMIN],
        submission,
        settings,
    )

    assert espiao.assinaturas() == 2, "a URL vencida tem de ser reassinada"
    assert "https://vencida.invalid/nao-usar" not in st_falso.imagens


# --- StrEnum sobre coluna Text volta do banco como str ----------------------


def test_o_rotulo_da_suspeita_sobrevive_a_ida_ao_banco(
    duplicidade, session, users, settings, monkeypatch
):
    """`DuplicateMatch.kind` é StrEnum sobre `Text`: sem tipo enum do
    SQLAlchemy, volta como `str` e não tem `.value`.

    `expire_all` é o ponto do teste. Enquanto o objeto está fresco na sessão o
    atributo ainda é o enum e `.value` funciona — foi por isso que 238 testes
    passaram por cima disto. Em produção o card é montado a partir de uma
    consulta, e aí o tipo é outro.
    """

    submission, espiao = duplicidade
    session.expire_all()
    st_falso = StFalso(clicar=True)

    matches = streamlit_app._duplicate_matches(session, submission)
    assert isinstance(matches[0].kind, str) and not hasattr(matches[0].kind, "value")

    _comparar(
        monkeypatch,
        st_falso,
        espiao,
        session,
        users[Role.ADMIN],
        submission,
        settings,
    )

    assert any("Duplicata exata" in texto for texto in st_falso.textos)


def test_o_simbolo_da_verificacao_aceita_o_valor_vindo_do_banco(
    duplicidade, session
):
    """`RuleCheck.outcome` tem o mesmo mapeamento, e o mesmo desfecho.

    A leitura ficava em `_render_submission_cards`, acima da chamada ao painel:
    o AttributeError derrubava o card do administrador antes que a comparação
    de evidências tivesse chance de renderizar. Consertar o painel sem
    consertar isto deixaria o Item 1 correto no código e invisível no app.
    """

    submission, _ = duplicidade
    session.expire_all()

    checks = session.get(Submission, submission.id).checks
    assert checks, "a submissão precisa ter verificações para o teste valer"
    for check in checks:
        assert isinstance(check.outcome, str)
        assert not hasattr(check.outcome, "value"), (
            "o valor veio do banco como enum: o teste deixou de exercitar a "
            "fronteira onde o tipo muda"
        )
        assert streamlit_app._simbolo_da_verificacao(check) in {"✅", "⚠️", "❌"}


def test_a_autorizacao_vem_antes_da_assinatura(
    duplicidade, session, users, settings, monkeypatch
):
    """Um aluno não assina a evidência de outro para "comparar".

    O painel é administrativo, mas a barreira não pode depender da tela que o
    chama: quem não pode ver a submissão não recebe URL nenhuma.
    """

    submission, espiao = duplicidade
    st_falso = StFalso(clicar=True)
    intruso = User(
        id=new_id(),
        username="intruso",
        display_name="Intruso",
        role=Role.STUDENT,
        active=True,
    )
    session.add(intruso)
    session.commit()

    _comparar(
        monkeypatch, st_falso, espiao, session, intruso, submission, settings
    )

    assert espiao.assinaturas() == 0, "não se assina antes de autorizar"
    assert st_falso.imagens == []
    assert len(_indisponiveis(st_falso)) == 2, "as duas colunas têm de recusar"
    assert all(
        "Sem permissão" in aviso for aviso in _indisponiveis(st_falso)
    ), "a recusa tem de dizer que foi de autorização, não de armazenamento"
