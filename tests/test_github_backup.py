from __future__ import annotations

import shutil
import tarfile
from pathlib import Path

import pytest
from sqlalchemy import func, select

from english_leaderboard.database import (
    create_database_engine,
    create_session_factory,
    initialize_database,
)
from english_leaderboard.github_backup import (
    MAX_BACKUP_BYTES,
    GitHubBackupConfigurationError,
    GitHubBackupError,
    build_archive,
    push_backup,
    restore_backup,
)
from english_leaderboard.models import LedgerTransaction, Role, Submission, User


class FakeGitHubGateway:
    """Guarda um arquivo por caminho, como a API de conteúdo do GitHub."""

    def __init__(self) -> None:
        self.arquivos: dict[str, bytes] = {}
        self.puts: list[dict[str, object]] = []
        self._sha = 0

    def get_file(self, repo: str, path: str, ref: str) -> tuple[bytes, str] | None:
        assert repo and ref
        conteudo = self.arquivos.get(path)
        if conteudo is None:
            return None
        return conteudo, f"sha-{len(conteudo)}"

    def put_file(
        self,
        repo: str,
        path: str,
        branch: str,
        content: bytes,
        message: str,
        sha: str | None,
    ) -> str | None:
        assert repo and branch and message
        self.puts.append({"path": path, "bytes": len(content), "sha": sha})
        self.arquivos[path] = content
        self._sha += 1
        return f"commit-{self._sha}"


def _montar_dados(database_url: str, upload_dir: Path) -> dict[str, int]:
    engine = create_database_engine(database_url)
    initialize_database(engine)
    sessao = create_session_factory(engine)()
    aluno = User(username="ana.silva", display_name="Ana Silva", role=Role.STUDENT)
    sessao.add(aluno)
    sessao.flush()
    sessao.add(
        LedgerTransaction(
            student_id=aluno.id,
            points=30,
            kind="DIRECT_ACTIVITY",
            source_type="submission",
            source_key="chave-unica",
            description="Reunião em inglês",
        )
    )
    sessao.commit()
    upload_dir.mkdir(parents=True, exist_ok=True)
    (upload_dir / "comprovante.png").write_bytes(b"conteudo-binario-do-print")
    contagem = {
        "usuarios": sessao.scalar(select(func.count(User.id))),
        "lancamentos": sessao.scalar(select(func.count(LedgerTransaction.id))),
    }
    sessao.close()
    engine.dispose()
    return contagem


def test_backup_survives_a_full_disk_wipe(tmp_path: Path) -> None:
    """O ciclo que o Streamlit Cloud provoca: disco recriado do zero."""

    dados = tmp_path / "data"
    dados.mkdir()
    database_url = f"sqlite:///{dados / 'app.db'}"
    uploads = dados / "uploads"
    antes = _montar_dados(database_url, uploads)
    assert antes == {"usuarios": 1, "lancamentos": 1}

    gateway = FakeGitHubGateway()
    envio = push_backup(
        gateway=gateway,
        repo="equipe/backups-privado",
        path="backups/english-leaderboard.tar.gz",
        branch="main",
        database_url=database_url,
        upload_dir=uploads,
        workdir=tmp_path / "trabalho",
    )
    assert envio.created is True
    assert envio.size_bytes > 0

    # O rebuild do Cloud: nada do disco sobrevive.
    shutil.rmtree(dados)
    assert not Path(dados / "app.db").exists()

    resultado = restore_backup(
        gateway=gateway,
        repo="equipe/backups-privado",
        path="backups/english-leaderboard.tar.gz",
        branch="main",
        database_url=database_url,
        upload_dir=uploads,
        workdir=tmp_path / "trabalho2",
    )
    assert resultado.restored is True

    engine = create_database_engine(database_url)
    sessao = create_session_factory(engine)()
    assert sessao.scalar(select(func.count(User.id))) == 1
    assert sessao.scalar(select(User.username)) == "ana.silva"
    lancamento = sessao.scalar(select(LedgerTransaction))
    assert lancamento.points == 30
    assert lancamento.source_key == "chave-unica"
    assert sessao.scalar(select(func.count(Submission.id))) == 0
    sessao.close()
    engine.dispose()

    # O comprovante volta byte a byte.
    assert (uploads / "comprovante.png").read_bytes() == b"conteudo-binario-do-print"


def test_restore_reports_when_there_is_nothing_stored(tmp_path: Path) -> None:
    dados = tmp_path / "data"
    dados.mkdir()
    resultado = restore_backup(
        gateway=FakeGitHubGateway(),
        repo="equipe/backups-privado",
        path="backups/english-leaderboard.tar.gz",
        branch="main",
        database_url=f"sqlite:///{dados / 'app.db'}",
        upload_dir=dados / "uploads",
        workdir=tmp_path / "trabalho",
    )
    assert resultado.restored is False
    assert "nenhuma cópia" in resultado.reason


def test_identical_content_is_not_pushed_again(tmp_path: Path) -> None:
    """Reenviar conteúdo igual não cria commit novo no repositório."""

    dados = tmp_path / "data"
    dados.mkdir()
    database_url = f"sqlite:///{dados / 'app.db'}"
    uploads = dados / "uploads"
    _montar_dados(database_url, uploads)
    gateway = FakeGitHubGateway()
    comum = {
        "gateway": gateway,
        "repo": "equipe/backups-privado",
        "path": "backups/english-leaderboard.tar.gz",
        "branch": "main",
        "database_url": database_url,
        "upload_dir": uploads,
    }
    push_backup(**comum, workdir=tmp_path / "w1")
    assert len(gateway.puts) == 1
    # O mesmo pacote, byte a byte, é reconhecido e ignorado.
    gateway.arquivos[comum["path"]] = gateway.arquivos[comum["path"]]
    conteudo = gateway.arquivos[comum["path"]]

    class GatewayEstavel(FakeGitHubGateway):
        def get_file(self, repo: str, path: str, ref: str):
            return conteudo, "sha-fixo"

    estavel = GatewayEstavel()
    resultado = push_backup(**{**comum, "gateway": estavel}, workdir=tmp_path / "w2")
    assert estavel.puts == []
    assert resultado.commit_sha is None


def test_repository_and_path_are_validated(tmp_path: Path) -> None:
    dados = tmp_path / "data"
    dados.mkdir()
    database_url = f"sqlite:///{dados / 'app.db'}"
    uploads = dados / "uploads"
    _montar_dados(database_url, uploads)
    comum = {
        "gateway": FakeGitHubGateway(),
        "branch": "main",
        "database_url": database_url,
        "upload_dir": uploads,
        "workdir": tmp_path / "w",
    }
    for repo in ("", "somente-nome", "a/b/c"):
        with pytest.raises(GitHubBackupConfigurationError):
            push_backup(**comum, repo=repo, path="backups/x.tar.gz")
    for path in ("", "../fora.tar.gz"):
        with pytest.raises(GitHubBackupConfigurationError):
            push_backup(**comum, repo="equipe/repo", path=path)


def test_oversized_package_is_refused(tmp_path: Path, monkeypatch) -> None:
    dados = tmp_path / "data"
    dados.mkdir()
    database_url = f"sqlite:///{dados / 'app.db'}"
    uploads = dados / "uploads"
    _montar_dados(database_url, uploads)
    monkeypatch.setattr("english_leaderboard.github_backup.MAX_BACKUP_BYTES", 10)
    with pytest.raises(GitHubBackupError):
        push_backup(
            gateway=FakeGitHubGateway(),
            repo="equipe/repo",
            path="backups/x.tar.gz",
            branch="main",
            database_url=database_url,
            upload_dir=uploads,
            workdir=tmp_path / "w",
        )
    assert MAX_BACKUP_BYTES > 1024


def test_archive_holds_the_database_and_the_uploads(tmp_path: Path) -> None:
    dados = tmp_path / "data"
    dados.mkdir()
    database_url = f"sqlite:///{dados / 'app.db'}"
    uploads = dados / "uploads"
    _montar_dados(database_url, uploads)

    pacote = build_archive(database_url, uploads, tmp_path / "w")

    with tarfile.open(pacote, "r:gz") as tar:
        nomes = sorted(tar.getnames())
    assert nomes == ["app.db", "manifest.json", "uploads/comprovante.png"]

    # Determinístico: reconstruir sem mudar nada gera bytes idênticos, então
    # uma aprovação sem efeito não vira commit novo no repositório.
    segundo = build_archive(database_url, uploads, tmp_path / "w2")
    assert segundo.read_bytes() == pacote.read_bytes()


def test_backing_up_into_the_app_repository_is_refused(tmp_path: Path) -> None:
    """Gravar no repositório do app dispararia rebuild, wipe e novo backup."""

    from english_leaderboard.github_backup import (
        current_checkout_repo,
        ensure_separate_repository,
    )

    checkout = tmp_path / "app"
    (checkout / ".git").mkdir(parents=True)
    (checkout / ".git" / "config").write_text(
        '[remote "origin"]\n\turl = https://github.com/Dvit36/aulas-de-ingles.git\n',
        encoding="utf-8",
    )

    assert current_checkout_repo(checkout) == "Dvit36/aulas-de-ingles"

    with pytest.raises(GitHubBackupConfigurationError) as erro:
        ensure_separate_repository("Dvit36/aulas-de-ingles", checkout=checkout)
    assert "mesmo repositório" in str(erro.value)

    # Um repositório separado passa sem reclamação.
    ensure_separate_repository("Dvit36/backups-ingles", checkout=checkout)

    # Sem .git legível, o guarda não bloqueia nada.
    assert current_checkout_repo(tmp_path / "sem-git") is None
