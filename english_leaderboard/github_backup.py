"""Cópia de segurança do banco e dos uploads em um repositório privado.

O Streamlit Community Cloud recria o contêiner a partir do git, então o disco
local não sobrevive a um rebuild nem a um despertar após hibernação. Este
módulo empacota o SQLite e os uploads e guarda o pacote em um repositório
privado do GitHub, restaurando-o quando a aplicação sobe com banco vazio.

Sem dependência de rede no import: a camada HTTP fica atrás de um protocolo,
e os testes injetam um gateway falso.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import io
import json
import shutil
import tarfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from .backup import create_backup, sqlite_path_from_url

GITHUB_API_ROOT = "https://api.github.com"
GITHUB_API_TIMEOUT_SECONDS = 30
# A API de conteúdo aceita até 100 MB, mas o pacote da equipe fica na casa dos
# poucos MB. O limite conservador evita transformar um erro de configuração em
# uma requisição gigante.
MAX_BACKUP_BYTES = 40 * 1024 * 1024


class GitHubBackupError(RuntimeError):
    """Falha ao guardar ou recuperar a cópia de segurança."""


class GitHubBackupConfigurationError(ValueError):
    """Repositório, token ou caminho ausentes ou malformados."""


@dataclass(frozen=True, slots=True)
class BackupUploadResult:
    path: str
    size_bytes: int
    created: bool
    commit_sha: str | None


@dataclass(frozen=True, slots=True)
class BackupRestoreResult:
    restored: bool
    reason: str
    size_bytes: int = 0


@runtime_checkable
class GitHubGateway(Protocol):
    def get_file(self, repo: str, path: str, ref: str) -> tuple[bytes, str] | None:
        """Devolve ``(conteúdo, sha)`` ou ``None`` quando o arquivo não existe."""

    def put_file(
        self,
        repo: str,
        path: str,
        branch: str,
        content: bytes,
        message: str,
        sha: str | None,
    ) -> str | None:
        """Cria ou atualiza o arquivo e devolve o sha do commit."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalize_repo(repo: str) -> str:
    candidate = (repo or "").strip().strip("/")
    candidate = candidate.removeprefix("https://github.com/")
    candidate = candidate.removesuffix(".git")
    partes = [part for part in candidate.split("/") if part]
    if len(partes) != 2:
        raise GitHubBackupConfigurationError(
            "GITHUB_BACKUP_REPO deve estar no formato dono/repositorio"
        )
    return "/".join(partes)


def _normalize_path(path: str) -> str:
    candidate = (path or "").strip().lstrip("/")
    if not candidate:
        raise GitHubBackupConfigurationError("GITHUB_BACKUP_PATH não pode ficar vazio")
    if ".." in Path(candidate).parts:
        raise GitHubBackupConfigurationError("GITHUB_BACKUP_PATH não pode subir diretórios")
    return candidate


def current_checkout_repo(start: Path | None = None) -> str | None:
    """Descobre `dono/repo` do checkout onde a aplicação está rodando.

    Melhor esforço: devolve ``None`` quando não há `.git` legível, como em uma
    imagem Docker sem o repositório.
    """

    base = (start or Path.cwd()).resolve()
    for diretorio in [base, *base.parents]:
        config = diretorio / ".git" / "config"
        if not config.is_file():
            continue
        try:
            texto = config.read_text(encoding="utf-8", errors="replace")
        except OSError:  # pragma: no cover - permissão negada
            return None
        for linha in texto.splitlines():
            linha = linha.strip()
            if not linha.startswith("url = ") or "github.com" not in linha:
                continue
            url = linha[len("url = ") :].strip()
            _, _, caminho = url.partition("github.com")
            try:
                return _normalize_repo(caminho.lstrip(":/"))
            except GitHubBackupConfigurationError:
                continue
        return None
    return None


def ensure_separate_repository(repo: str, *, checkout: Path | None = None) -> None:
    """Impede que a cópia vá para o repositório da própria aplicação.

    No Streamlit Cloud, gravar no repositório do app dispara um rebuild, que
    recria o disco e apaga os dados — que então são restaurados e regravados,
    disparando outro rebuild. O laço destrói exatamente o que deveria proteger.
    """

    atual = current_checkout_repo(checkout)
    if atual and atual.casefold() == _normalize_repo(repo).casefold():
        raise GitHubBackupConfigurationError(
            "GITHUB_BACKUP_REPO não pode ser o mesmo repositório da aplicação "
            f"({atual}): gravar nele dispara um rebuild que apaga os dados. "
            "Crie um repositório privado separado só para as cópias."
        )


class GitHubApiGateway:
    """Gateway HTTP mínimo sobre a API de conteúdo do GitHub."""

    def __init__(self, token: str, *, api_root: str = GITHUB_API_ROOT) -> None:
        if not (token or "").strip():
            raise GitHubBackupConfigurationError("GITHUB_BACKUP_TOKEN não configurado")
        self._token = token.strip()
        self._api_root = api_root.rstrip("/")

    def _request(
        self,
        method: str,
        url: str,
        *,
        accept: str,
        payload: dict[str, Any] | None = None,
    ) -> tuple[int, bytes]:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Authorization", f"Bearer {self._token}")
        request.add_header("Accept", accept)
        request.add_header("X-GitHub-Api-Version", "2022-11-28")
        request.add_header("User-Agent", "english-leaderboard-backup")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(
                request, timeout=GITHUB_API_TIMEOUT_SECONDS
            ) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()
        except urllib.error.URLError as error:  # pragma: no cover - rede indisponível
            raise GitHubBackupError(f"Falha de rede ao falar com o GitHub: {error}") from error

    def get_file(self, repo: str, path: str, ref: str) -> tuple[bytes, str] | None:
        url = f"{self._api_root}/repos/{repo}/contents/{path}?ref={ref}"
        # Acima de 1 MB a API só devolve o conteúdo com o media type .raw, e o
        # pacote de backup passa disso rapidamente.
        status, body = self._request("GET", url, accept="application/vnd.github.raw")
        if status == 404:
            return None
        if status >= 400:
            raise GitHubBackupError(f"GitHub respondeu {status} ao ler {path}: {body[:200]!r}")
        sha_status, sha_body = self._request(
            "GET", url, accept="application/vnd.github.object+json"
        )
        sha = ""
        if sha_status < 400:
            try:
                sha = json.loads(sha_body).get("sha", "")
            except (ValueError, AttributeError):
                sha = ""
        return body, sha

    def put_file(
        self,
        repo: str,
        path: str,
        branch: str,
        content: bytes,
        message: str,
        sha: str | None,
    ) -> str | None:
        url = f"{self._api_root}/repos/{repo}/contents/{path}"
        payload: dict[str, Any] = {
            "message": message,
            "content": base64.b64encode(content).decode("ascii"),
            "branch": branch,
        }
        if sha:
            payload["sha"] = sha
        status, body = self._request(
            "PUT", url, accept="application/vnd.github+json", payload=payload
        )
        if status >= 400:
            raise GitHubBackupError(
                f"GitHub respondeu {status} ao gravar {path}: {body[:200]!r}"
            )
        try:
            return json.loads(body).get("commit", {}).get("sha")
        except (ValueError, AttributeError):  # pragma: no cover - resposta inesperada
            return None


def _deterministic_info(name: str, size: int) -> tarfile.TarInfo:
    """Metadados fixos para o pacote sair byte a byte igual entre execuções."""

    info = tarfile.TarInfo(name)
    info.size = size
    info.mtime = 0
    info.mode = 0o600
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    return info


def build_archive(
    database_url: str, upload_dir: str | Path, workdir: str | Path
) -> Path:
    """Empacota um snapshot consistente do banco e os uploads em um .tar.gz.

    O pacote é determinístico: gzip sem timestamp e metadados fixos. Assim, um
    conteúdo inalterado gera bytes idênticos e não vira commit novo no
    repositório a cada aprovação.
    """

    staging = Path(workdir) / "staging"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    manifest, _ = create_backup(database_url, upload_dir, staging)
    banco = staging / manifest.database_file

    origem_uploads = Path(upload_dir)
    arquivos: list[tuple[str, Path]] = []
    if origem_uploads.is_dir():
        for caminho in sorted(origem_uploads.rglob("*")):
            if caminho.is_file():
                relativo = caminho.relative_to(origem_uploads).as_posix()
                arquivos.append((f"uploads/{relativo}", caminho))

    inventario = {
        "database_sha256": manifest.database_sha256,
        "uploads": [
            {"path": nome, "sha256": _sha256(caminho)} for nome, caminho in arquivos
        ],
    }
    inventario_bytes = json.dumps(
        inventario, ensure_ascii=False, indent=2, sort_keys=True
    ).encode("utf-8")

    archive = Path(workdir) / "english-leaderboard-backup.tar.gz"
    if archive.exists():
        archive.unlink()
    # mtime=0 remove o carimbo de tempo que o gzip grava por padrão.
    with (
        archive.open("wb") as bruto,
        gzip.GzipFile(filename="", mode="wb", fileobj=bruto, mtime=0) as compactado,
        tarfile.open(fileobj=compactado, mode="w") as tar,
    ):
            dados = banco.read_bytes()
            tar.addfile(_deterministic_info("app.db", len(dados)), io.BytesIO(dados))
            tar.addfile(
                _deterministic_info("manifest.json", len(inventario_bytes)),
                io.BytesIO(inventario_bytes),
            )
            for nome, caminho in arquivos:
                conteudo = caminho.read_bytes()
                tar.addfile(
                    _deterministic_info(nome, len(conteudo)), io.BytesIO(conteudo)
                )
    shutil.rmtree(staging, ignore_errors=True)
    return archive


def _is_safe_member(member: tarfile.TarInfo) -> bool:
    """Recusa link e caminho que escape do diretório de destino."""

    if member.issym() or member.islnk():
        return False
    nome = Path(member.name)
    return not (nome.is_absolute() or ".." in nome.parts)


def extract_archive(
    archive: str | Path, database_url: str, upload_dir: str | Path
) -> None:
    """Recoloca banco e uploads no lugar a partir do pacote."""

    destino_db = sqlite_path_from_url(database_url)
    destino_db.parent.mkdir(parents=True, exist_ok=True)
    destino_uploads = Path(upload_dir)
    destino_uploads.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:gz") as tar:
        membros = tar.getmembers()
        por_nome = {m.name: m for m in membros}
        if "app.db" not in por_nome:
            raise GitHubBackupError("Pacote de backup não contém app.db")
        # O arquivo restaurado já é um snapshot completo; restos do WAL
        # anterior descreveriam transações que não existem mais.
        for sufixo in ("-wal", "-shm"):
            Path(str(destino_db) + sufixo).unlink(missing_ok=True)
        origem = tar.extractfile(por_nome["app.db"])
        if origem is None:  # pragma: no cover - pacote corrompido
            raise GitHubBackupError("Não foi possível ler app.db do pacote")
        with destino_db.open("wb") as saida:
            shutil.copyfileobj(origem, saida)
        for membro in membros:
            if not membro.name.startswith("uploads/") or not _is_safe_member(membro):
                continue
            relativo = membro.name[len("uploads/") :]
            if not relativo:
                continue
            alvo = destino_uploads / relativo
            alvo.parent.mkdir(parents=True, exist_ok=True)
            conteudo = tar.extractfile(membro)
            if conteudo is None:  # pragma: no cover - entrada de diretório
                continue
            with alvo.open("wb") as saida:
                shutil.copyfileobj(conteudo, saida)
            alvo.chmod(0o600)


def push_backup(
    *,
    gateway: GitHubGateway,
    repo: str,
    path: str,
    branch: str,
    database_url: str,
    upload_dir: str | Path,
    workdir: str | Path,
    message: str = "Atualizar cópia de segurança",
) -> BackupUploadResult:
    """Gera o pacote e o grava no repositório privado."""

    repo_norm = _normalize_repo(repo)
    ensure_separate_repository(repo_norm)
    path_norm = _normalize_path(path)
    archive = build_archive(database_url, upload_dir, workdir)
    content = archive.read_bytes()
    if len(content) > MAX_BACKUP_BYTES:
        raise GitHubBackupError(
            f"Pacote de {len(content)} bytes excede o limite de {MAX_BACKUP_BYTES}"
        )
    atual = gateway.get_file(repo_norm, path_norm, branch)
    sha = atual[1] if atual else None
    if atual is not None and atual[0] == content:
        return BackupUploadResult(path_norm, len(content), created=False, commit_sha=None)
    commit = gateway.put_file(
        repo_norm, path_norm, branch, content, message, sha
    )
    return BackupUploadResult(path_norm, len(content), created=sha is None, commit_sha=commit)


def restore_backup(
    *,
    gateway: GitHubGateway,
    repo: str,
    path: str,
    branch: str,
    database_url: str,
    upload_dir: str | Path,
    workdir: str | Path,
) -> BackupRestoreResult:
    """Baixa o pacote e recoloca banco e uploads no disco."""

    repo_norm = _normalize_repo(repo)
    path_norm = _normalize_path(path)
    encontrado = gateway.get_file(repo_norm, path_norm, branch)
    if encontrado is None:
        return BackupRestoreResult(False, "nenhuma cópia encontrada no repositório")
    content, _ = encontrado
    destino = Path(workdir) / "download.tar.gz"
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_bytes(content)
    extract_archive(destino, database_url, upload_dir)
    return BackupRestoreResult(True, "cópia restaurada", len(content))


__all__ = [
    "MAX_BACKUP_BYTES",
    "BackupRestoreResult",
    "BackupUploadResult",
    "GitHubApiGateway",
    "GitHubBackupConfigurationError",
    "GitHubBackupError",
    "GitHubGateway",
    "build_archive",
    "current_checkout_repo",
    "ensure_separate_repository",
    "extract_archive",
    "push_backup",
    "restore_backup",
]
