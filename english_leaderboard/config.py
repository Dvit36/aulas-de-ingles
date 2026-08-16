from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.engine import make_url

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - only useful before dependencies install
    load_dotenv = None


def _as_bool(value: str | None, *, default: bool = False) -> bool:
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off", ""}:
        return False
    raise ValueError(f"Valor booleano inválido: {value!r}")


def _csv_set(value: str | None, *, lower: bool = False) -> frozenset[str]:
    items = {item.strip() for item in (value or "").split(",") if item.strip()}
    return frozenset(item.lower() for item in items) if lower else frozenset(items)


@dataclass(frozen=True)
class Settings:
    app_env: str = "development"
    demo_auth_enabled: bool = False
    demo_student_email: str = "aluno.demo@example.local"
    demo_admin_email: str = "admin.demo@example.local"
    database_url: str = "sqlite:///./data/app.db"
    upload_dir: Path = Path("./data/uploads")
    allowed_emails: frozenset[str] = frozenset()
    admin_emails: frozenset[str] = frozenset()
    max_upload_bytes: int = 10 * 1024 * 1024
    allowed_image_formats: frozenset[str] = frozenset({"JPEG", "PNG", "WEBP"})
    min_image_width: int = 320
    min_image_height: int = 320
    min_laplacian_variance: float = 18.0
    phash_distance_threshold: int = 6
    auto_approve_confidence: float = 0.88
    summary_min_chars: int = 120
    google_sheets_auto_sync: bool = False
    google_sheets_spreadsheet_id: str = ""
    google_sheets_leaderboard_tab: str = "Leaderboard"
    google_sheets_ledger_tab: str = "Ledger"

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @classmethod
    def from_env(cls, *, env_file: str | Path | None = ".env") -> "Settings":
        if load_dotenv is not None and env_file:
            load_dotenv(dotenv_path=env_file, override=False)
        settings = cls(
            app_env=os.getenv("APP_ENV", "development").strip().lower(),
            demo_auth_enabled=_as_bool(os.getenv("DEMO_AUTH_ENABLED")),
            demo_student_email=os.getenv(
                "DEMO_STUDENT_EMAIL", "aluno.demo@example.local"
            ).strip().lower(),
            demo_admin_email=os.getenv(
                "DEMO_ADMIN_EMAIL", "admin.demo@example.local"
            ).strip().lower(),
            database_url=os.getenv("DATABASE_URL", "sqlite:///./data/app.db").strip(),
            upload_dir=Path(os.getenv("UPLOAD_DIR", "./data/uploads")),
            allowed_emails=_csv_set(os.getenv("ALLOWED_EMAILS"), lower=True),
            admin_emails=_csv_set(os.getenv("ADMIN_EMAILS"), lower=True),
            max_upload_bytes=int(os.getenv("MAX_UPLOAD_BYTES", str(10 * 1024 * 1024))),
            allowed_image_formats=frozenset(
                item.upper()
                for item in _csv_set(
                    os.getenv("ALLOWED_IMAGE_FORMATS", "JPEG,PNG,WEBP")
                )
            ),
            min_image_width=int(os.getenv("MIN_IMAGE_WIDTH", "320")),
            min_image_height=int(os.getenv("MIN_IMAGE_HEIGHT", "320")),
            min_laplacian_variance=float(
                os.getenv("MIN_LAPLACIAN_VARIANCE", "18")
            ),
            phash_distance_threshold=int(
                os.getenv("PHASH_DISTANCE_THRESHOLD", "6")
            ),
            auto_approve_confidence=float(
                os.getenv("AUTO_APPROVE_CONFIDENCE", "0.88")
            ),
            summary_min_chars=int(os.getenv("SUMMARY_MIN_CHARS", "120")),
            google_sheets_auto_sync=_as_bool(
                os.getenv("GOOGLE_SHEETS_AUTO_SYNC")
            ),
            google_sheets_spreadsheet_id=os.getenv(
                "GOOGLE_SHEETS_SPREADSHEET_ID", ""
            ).strip(),
            google_sheets_leaderboard_tab=os.getenv(
                "GOOGLE_SHEETS_LEADERBOARD_TAB", "Leaderboard"
            ).strip(),
            google_sheets_ledger_tab=os.getenv(
                "GOOGLE_SHEETS_LEDGER_TAB", "Ledger"
            ).strip(),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if self.app_env not in {"development", "test", "production"}:
            raise ValueError("APP_ENV deve ser development, test ou production")
        if self.is_production and self.demo_auth_enabled:
            raise RuntimeError(
                "DEMO_AUTH_ENABLED=true é proibido quando APP_ENV=production"
            )
        if self.is_production and not self.allowed_emails:
            raise RuntimeError("ALLOWED_EMAILS não pode ficar vazio em produção")
        if not self.admin_emails.issubset(self.allowed_emails):
            raise ValueError("ADMIN_EMAILS deve ser subconjunto de ALLOWED_EMAILS")
        if not self.database_url:
            raise ValueError("DATABASE_URL não pode ficar vazio")
        if self.max_upload_bytes <= 0:
            raise ValueError("MAX_UPLOAD_BYTES deve ser positivo")
        if not 0 <= self.auto_approve_confidence <= 1:
            raise ValueError("AUTO_APPROVE_CONFIDENCE deve estar entre 0 e 1")
        if self.phash_distance_threshold < 0:
            raise ValueError("PHASH_DISTANCE_THRESHOLD não pode ser negativo")
        if self.google_sheets_auto_sync and not self.google_sheets_spreadsheet_id:
            raise ValueError(
                "GOOGLE_SHEETS_SPREADSHEET_ID é obrigatório quando "
                "GOOGLE_SHEETS_AUTO_SYNC=true"
            )
        tab_names = (
            self.google_sheets_leaderboard_tab,
            self.google_sheets_ledger_tab,
        )
        if any(not name or len(name) > 100 for name in tab_names):
            raise ValueError("Nomes de abas Google Sheets devem ter de 1 a 100 caracteres")
        if any(any(character in name for character in "[]:*?/\\") for name in tab_names):
            raise ValueError("Nome de aba Google Sheets contém caractere inválido")
        if self.google_sheets_leaderboard_tab == self.google_sheets_ledger_tab:
            raise ValueError("As abas de leaderboard e ledger devem ter nomes diferentes")

    def ensure_directories(self) -> None:
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        url = make_url(self.database_url)
        if url.get_backend_name() == "sqlite" and url.database not in {None, "", ":memory:"}:
            Path(url.database).expanduser().parent.mkdir(parents=True, exist_ok=True)
