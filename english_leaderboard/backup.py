from __future__ import annotations

import hashlib
import json
import sqlite3
import tarfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse


@dataclass(frozen=True)
class BackupManifest:
    created_at: str
    database_file: str
    database_sha256: str
    uploads_file: str | None
    uploads_sha256: str | None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sqlite_path_from_url(database_url: str) -> Path:
    parsed = urlparse(database_url)
    if parsed.scheme != "sqlite":
        raise ValueError("O backup embutido suporta apenas SQLite no MVP")
    if parsed.path in {"", "/:memory:"}:
        raise ValueError("Banco SQLite em memória não pode ser copiado")
    # sqlite:////data/app.db -> /data/app.db; sqlite:///./data/app.db -> ./data/app.db
    raw_path = unquote(parsed.path)
    if database_url.startswith("sqlite:////"):
        return Path(raw_path)
    return Path(raw_path.lstrip("/"))


def create_backup(
    database_url: str,
    upload_dir: str | Path,
    destination: str | Path,
) -> tuple[BackupManifest, Path]:
    """Cria snapshot consistente do SQLite e arquivo dos uploads.

    A API de backup online do SQLite evita copiar diretamente um arquivo WAL em
    uso. O arquivo de uploads é separado para permitir inspeção/restauração.
    """

    source_db = sqlite_path_from_url(database_url).resolve()
    if not source_db.is_file():
        raise FileNotFoundError(f"Banco não encontrado: {source_db}")

    output_dir = Path(destination).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    db_output = output_dir / f"app-{stamp}.sqlite3"

    with sqlite3.connect(source_db) as source, sqlite3.connect(db_output) as target:
        source.backup(target)

    uploads_path = Path(upload_dir).resolve()
    uploads_output: Path | None = None
    if uploads_path.is_dir():
        uploads_output = output_dir / f"uploads-{stamp}.tar.gz"
        with tarfile.open(uploads_output, "w:gz") as archive:
            archive.add(uploads_path, arcname="uploads", recursive=True)

    manifest = BackupManifest(
        created_at=datetime.now(timezone.utc).isoformat(),
        database_file=db_output.name,
        database_sha256=_sha256(db_output),
        uploads_file=uploads_output.name if uploads_output else None,
        uploads_sha256=_sha256(uploads_output) if uploads_output else None,
    )
    manifest_path = output_dir / f"manifest-{stamp}.json"
    manifest_path.write_text(
        json.dumps(asdict(manifest), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return manifest, manifest_path


def verify_backup(manifest_path: str | Path) -> bool:
    path = Path(manifest_path).resolve()
    payload = json.loads(path.read_text(encoding="utf-8"))
    db_path = path.parent / payload["database_file"]
    if not db_path.is_file() or _sha256(db_path) != payload["database_sha256"]:
        return False
    uploads_name = payload.get("uploads_file")
    if uploads_name:
        uploads_path = path.parent / uploads_name
        if (
            not uploads_path.is_file()
            or _sha256(uploads_path) != payload.get("uploads_sha256")
        ):
            return False
    return True

