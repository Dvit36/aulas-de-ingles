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
    local_auth_enabled: bool = True
    bootstrap_admin_name: str = ""
    bootstrap_admin_email: str = ""
    bootstrap_admin_password: str = ""
    session_hours: int = 12
    login_max_attempts: int = 5
    login_lock_minutes: int = 15
    database_url: str = "sqlite:///./data/app.db"
    upload_dir: Path = Path("./data/uploads")
    allowed_emails: frozenset[str] = frozenset()
    admin_emails: frozenset[str] = frozenset()
    max_upload_bytes: int = 10 * 1024 * 1024
    max_upload_files: int = 10
    max_upload_total_bytes: int = 30 * 1024 * 1024
    max_pdf_pages: int = 10
    max_document_expanded_bytes: int = 50 * 1024 * 1024
    max_pdf_render_pixels: int = 24_000_000
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
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from_email: str = ""
    smtp_from_name: str = "English Activities"
    smtp_use_tls: bool = True
    reminder_dry_run: bool = True
    reminder_scheduler_interval_seconds: int = 300

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
            local_auth_enabled=_as_bool(
                os.getenv("LOCAL_AUTH_ENABLED"), default=True
            ),
            bootstrap_admin_name=os.getenv("BOOTSTRAP_ADMIN_NAME", "").strip(),
            bootstrap_admin_email=os.getenv(
                "BOOTSTRAP_ADMIN_EMAIL", ""
            ).strip().lower(),
            bootstrap_admin_password=os.getenv(
                "BOOTSTRAP_ADMIN_PASSWORD", ""
            ),
            session_hours=int(os.getenv("SESSION_HOURS", "12")),
            login_max_attempts=int(os.getenv("LOGIN_MAX_ATTEMPTS", "5")),
            login_lock_minutes=int(os.getenv("LOGIN_LOCK_MINUTES", "15")),
            database_url=os.getenv("DATABASE_URL", "sqlite:///./data/app.db").strip(),
            upload_dir=Path(os.getenv("UPLOAD_DIR", "./data/uploads")),
            allowed_emails=_csv_set(os.getenv("ALLOWED_EMAILS"), lower=True),
            admin_emails=_csv_set(os.getenv("ADMIN_EMAILS"), lower=True),
            max_upload_bytes=int(os.getenv("MAX_UPLOAD_BYTES", str(10 * 1024 * 1024))),
            max_upload_files=int(os.getenv("MAX_UPLOAD_FILES", "10")),
            max_upload_total_bytes=int(
                os.getenv("MAX_UPLOAD_TOTAL_BYTES", str(30 * 1024 * 1024))
            ),
            max_pdf_pages=int(os.getenv("MAX_PDF_PAGES", "10")),
            max_document_expanded_bytes=int(
                os.getenv(
                    "MAX_DOCUMENT_EXPANDED_BYTES", str(50 * 1024 * 1024)
                )
            ),
            max_pdf_render_pixels=int(
                os.getenv("MAX_PDF_RENDER_PIXELS", "24000000")
            ),
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
            smtp_host=os.getenv("SMTP_HOST", "").strip(),
            smtp_port=int(os.getenv("SMTP_PORT", "587")),
            smtp_username=os.getenv("SMTP_USERNAME", "").strip(),
            smtp_password=os.getenv("SMTP_PASSWORD", ""),
            smtp_from_email=os.getenv("SMTP_FROM_EMAIL", "").strip(),
            smtp_from_name=os.getenv(
                "SMTP_FROM_NAME", "English Activities"
            ).strip(),
            smtp_use_tls=_as_bool(os.getenv("SMTP_USE_TLS"), default=True),
            reminder_dry_run=_as_bool(
                os.getenv("REMINDER_DRY_RUN"), default=True
            ),
            reminder_scheduler_interval_seconds=int(
                os.getenv("REMINDER_SCHEDULER_INTERVAL_SECONDS", "300")
            ),
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
        if self.is_production and not self.local_auth_enabled and not self.allowed_emails:
            raise RuntimeError("ALLOWED_EMAILS não pode ficar vazio em produção")
        if not self.admin_emails.issubset(self.allowed_emails):
            raise ValueError("ADMIN_EMAILS deve ser subconjunto de ALLOWED_EMAILS")
        if not self.database_url:
            raise ValueError("DATABASE_URL não pode ficar vazio")
        if self.max_upload_bytes <= 0:
            raise ValueError("MAX_UPLOAD_BYTES deve ser positivo")
        if self.max_upload_files <= 0:
            raise ValueError("MAX_UPLOAD_FILES deve ser positivo")
        if self.max_upload_total_bytes < self.max_upload_bytes:
            raise ValueError(
                "MAX_UPLOAD_TOTAL_BYTES deve ser maior ou igual a MAX_UPLOAD_BYTES"
            )
        if self.max_pdf_pages <= 0:
            raise ValueError("MAX_PDF_PAGES deve ser positivo")
        if self.max_document_expanded_bytes < self.max_upload_bytes:
            raise ValueError(
                "MAX_DOCUMENT_EXPANDED_BYTES deve ser maior ou igual a "
                "MAX_UPLOAD_BYTES"
            )
        if self.max_pdf_render_pixels <= 0:
            raise ValueError("MAX_PDF_RENDER_PIXELS deve ser positivo")
        if self.session_hours <= 0:
            raise ValueError("SESSION_HOURS deve ser positivo")
        if self.login_max_attempts < 2 or self.login_lock_minutes <= 0:
            raise ValueError("Limites de login inválidos")
        bootstrap_values = (
            self.bootstrap_admin_name,
            self.bootstrap_admin_email,
            self.bootstrap_admin_password,
        )
        if any(bootstrap_values) and not all(bootstrap_values):
            raise ValueError(
                "BOOTSTRAP_ADMIN_NAME, BOOTSTRAP_ADMIN_EMAIL e "
                "BOOTSTRAP_ADMIN_PASSWORD devem ser definidos juntos"
            )
        if self.bootstrap_admin_password and len(self.bootstrap_admin_password) < 10:
            raise ValueError("BOOTSTRAP_ADMIN_PASSWORD deve ter ao menos 10 caracteres")
        if self.smtp_port <= 0 or self.smtp_port > 65535:
            raise ValueError("SMTP_PORT inválida")
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
