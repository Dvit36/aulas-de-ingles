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


def _env_with_legacy(name: str, legacy_name: str, default: str = "") -> str:
    """Lê a variável atual e aceita o nome antigo enquanto ele existir.

    O login deixou de usar e-mail, mas ambientes já implantados guardam os
    segredos com os nomes antigos. Ler o nome antigo evita que uma atualização
    da imagem derrube a aplicação por configuração ausente.
    """

    current = os.getenv(name)
    if current is not None and current.strip():
        return current
    legacy = os.getenv(legacy_name)
    if legacy is not None and legacy.strip():
        return legacy
    return default


def _csv_set(value: str | None, *, lower: bool = False) -> frozenset[str]:
    items = {item.strip() for item in (value or "").split(",") if item.strip()}
    return frozenset(item.lower() for item in items) if lower else frozenset(items)


@dataclass(frozen=True)
class Settings:
    app_env: str = "development"
    demo_auth_enabled: bool = False
    demo_student_username: str = "aluno.demo"
    demo_admin_username: str = "admin.demo"
    seed_fake_data: bool = True
    bootstrap_admin_name: str = ""
    bootstrap_admin_username: str = ""
    bootstrap_admin_password: str = ""
    login_max_attempts: int = 5
    login_lock_minutes: int = 15
    database_url: str = "sqlite:///./data/app.db"
    upload_dir: Path = Path("./data/uploads")
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
    supabase_url: str = ""
    supabase_publishable_key: str = ""
    supabase_secret_key: str = ""
    supabase_db_url: str = ""
    supabase_username_domain: str = "robonaticos7565.invalid"
    storage_bucket: str = "student-files"
    # Não existe teto de gasto no Supabase. Os limites abaixo são a única
    # barreira real contra cobrança por excedente. O padrão deixa 10x de folga
    # sobre o uso previsto (~48 MB por temporada) e fica na metade da franquia
    # gratuita, que é de 1 GB de espaço e 10 GB de egress por mês.
    storage_max_total_bytes: int = 500 * 1024 * 1024
    storage_max_monthly_egress_bytes: int = 2 * 1024 * 1024 * 1024
    google_sheets_auto_sync: bool = False
    github_backup_enabled: bool = False
    github_backup_repo: str = ""
    github_backup_token: str = ""
    github_backup_path: str = "backups/english-leaderboard.tar.gz"
    github_backup_branch: str = "main"
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
    def supabase_ready(self) -> bool:
        """Há o mínimo para autenticar e consultar dados no Supabase."""

        return bool(
            self.supabase_url
            and self.supabase_publishable_key
            and self.supabase_db_url
        )

    @property
    def contas_administraveis(self) -> bool:
        """Há credencial privilegiada para criar conta e redefinir senha.

        Existe como propriedade para quem só precisa *saber* se a operação é
        possível não ter de tocar na chave. ``admin_secret_key()`` continua
        sendo o único caminho até o valor.
        """

        return bool(self.supabase_ready and self.supabase_secret_key)

    @property
    def storage_ready(self) -> bool:
        """O Storage usa as mesmas credenciais do Supabase, mais o bucket."""

        return bool(self.supabase_ready and self.storage_bucket)

    def admin_secret_key(self) -> str:
        """Chave privilegiada do Supabase, para operação administrativa.

        É um método, e não um acesso direto ao campo, para que todo uso fique
        rastreável e explícito: ela ignora RLS e não pode aparecer em nenhum
        caminho de aluno.
        """

        if not self.supabase_secret_key:
            raise ValueError(
                "SUPABASE_SECRET_KEY não configurada: operações administrativas "
                "do Supabase Auth estão indisponíveis"
            )
        return self.supabase_secret_key

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @classmethod
    def from_env(cls, *, env_file: str | Path | None = ".env") -> Settings:
        if load_dotenv is not None and env_file:
            load_dotenv(dotenv_path=env_file, override=False)
        settings = cls(
            app_env=os.getenv("APP_ENV", "development").strip().lower(),
            demo_auth_enabled=_as_bool(os.getenv("DEMO_AUTH_ENABLED")),
            demo_student_username=_env_with_legacy(
                "DEMO_STUDENT_USERNAME", "DEMO_STUDENT_EMAIL", "aluno.demo"
            ).strip().lower(),
            demo_admin_username=_env_with_legacy(
                "DEMO_ADMIN_USERNAME", "DEMO_ADMIN_EMAIL", "admin.demo"
            ).strip().lower(),
            seed_fake_data=_as_bool(os.getenv("SEED_FAKE_DATA"), default=True),
            bootstrap_admin_name=os.getenv("BOOTSTRAP_ADMIN_NAME", "").strip(),
            bootstrap_admin_username=_env_with_legacy(
                "BOOTSTRAP_ADMIN_USERNAME", "BOOTSTRAP_ADMIN_EMAIL"
            ).strip().lower(),
            bootstrap_admin_password=os.getenv(
                "BOOTSTRAP_ADMIN_PASSWORD", ""
            ),
            login_max_attempts=int(os.getenv("LOGIN_MAX_ATTEMPTS", "5")),
            login_lock_minutes=int(os.getenv("LOGIN_LOCK_MINUTES", "15")),
            database_url=os.getenv("DATABASE_URL", "sqlite:///./data/app.db").strip(),
            upload_dir=Path(os.getenv("UPLOAD_DIR", "./data/uploads")),
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
            supabase_url=os.getenv("SUPABASE_URL", "").strip().rstrip("/"),
            supabase_publishable_key=os.getenv(
                "SUPABASE_PUBLISHABLE_KEY", ""
            ).strip(),
            supabase_secret_key=os.getenv("SUPABASE_SECRET_KEY", "").strip(),
            supabase_db_url=os.getenv("SUPABASE_DB_URL", "").strip(),
            supabase_username_domain=os.getenv(
                "SUPABASE_USERNAME_DOMAIN", "robonaticos7565.invalid"
            ).strip().lower(),
            storage_bucket=os.getenv("STORAGE_BUCKET", "student-files").strip(),
            storage_max_total_bytes=int(
                os.getenv("STORAGE_MAX_TOTAL_BYTES", str(500 * 1024 * 1024))
            ),
            storage_max_monthly_egress_bytes=int(
                os.getenv(
                    "STORAGE_MAX_MONTHLY_EGRESS_BYTES", str(2 * 1024 * 1024 * 1024)
                )
            ),
            google_sheets_auto_sync=_as_bool(
                os.getenv("GOOGLE_SHEETS_AUTO_SYNC")
            ),
            github_backup_enabled=_as_bool(os.getenv("GITHUB_BACKUP_ENABLED")),
            github_backup_repo=os.getenv("GITHUB_BACKUP_REPO", "").strip(),
            github_backup_token=os.getenv("GITHUB_BACKUP_TOKEN", "").strip(),
            github_backup_path=os.getenv(
                "GITHUB_BACKUP_PATH", "backups/english-leaderboard.tar.gz"
            ).strip(),
            github_backup_branch=os.getenv("GITHUB_BACKUP_BRANCH", "main").strip(),
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
        if self.login_max_attempts < 2 or self.login_lock_minutes <= 0:
            raise ValueError("Limites de login inválidos")
        bootstrap_values = (
            self.bootstrap_admin_name,
            self.bootstrap_admin_username,
            self.bootstrap_admin_password,
        )
        if any(bootstrap_values) and not all(bootstrap_values):
            missing = [
                label
                for label, value in (
                    ("BOOTSTRAP_ADMIN_NAME", self.bootstrap_admin_name),
                    ("BOOTSTRAP_ADMIN_USERNAME", self.bootstrap_admin_username),
                    ("BOOTSTRAP_ADMIN_PASSWORD", self.bootstrap_admin_password),
                )
                if not value
            ]
            raise ValueError(
                "BOOTSTRAP_ADMIN_NAME, BOOTSTRAP_ADMIN_USERNAME e "
                "BOOTSTRAP_ADMIN_PASSWORD devem ser definidos juntos. "
                f"Faltando: {', '.join(missing)}. "
                "BOOTSTRAP_ADMIN_USERNAME substituiu BOOTSTRAP_ADMIN_EMAIL; "
                "o nome antigo ainda é aceito."
            )
        if self.bootstrap_admin_password and len(self.bootstrap_admin_password) < 10:
            raise ValueError("BOOTSTRAP_ADMIN_PASSWORD deve ter ao menos 10 caracteres")
        if self.smtp_port <= 0 or self.smtp_port > 65535:
            raise ValueError("SMTP_PORT inválida")
        if not 0 <= self.auto_approve_confidence <= 1:
            raise ValueError("AUTO_APPROVE_CONFIDENCE deve estar entre 0 e 1")
        if self.phash_distance_threshold < 0:
            raise ValueError("PHASH_DISTANCE_THRESHOLD não pode ser negativo")
        if self.github_backup_enabled:
            raise RuntimeError(
                "GITHUB_BACKUP_ENABLED foi descontinuado: dados e arquivos de "
                "alunos não podem ser armazenados no GitHub. Use Supabase "
                "PostgreSQL para dados/metadados e Supabase Storage para binários."
            )
        if self.supabase_url and not self.supabase_url.startswith("https://"):
            raise ValueError("SUPABASE_URL deve começar com https://")
        if self.supabase_publishable_key and self.supabase_publishable_key.startswith(
            "sb_secret_"
        ):
            raise ValueError(
                "SUPABASE_PUBLISHABLE_KEY recebeu uma chave secreta. A pública "
                "tem o prefixo sb_publishable_"
            )
        if self.supabase_secret_key and self.supabase_secret_key.startswith(
            "sb_publishable_"
        ):
            raise ValueError(
                "SUPABASE_SECRET_KEY recebeu a chave pública. A privilegiada "
                "tem o prefixo sb_secret_"
            )
        if self.supabase_db_url:
            url = make_url(self.supabase_db_url)
            if not url.get_backend_name().startswith("postgresql"):
                raise ValueError("SUPABASE_DB_URL deve apontar para PostgreSQL")
            # A conexão direta na 5432 é IPv6 e o Streamlit Cloud é IPv4. O
            # Session pooler usa a mesma porta, mas com host .pooler.
            if self.is_production and url.host and "pooler" not in url.host:
                raise ValueError(
                    "Em produção use o Session pooler do Supabase: a conexão "
                    "direta é IPv6 e o Streamlit Cloud não a alcança"
                )
        if self.is_production:
            # Nomear o que falta, e não só dizer que falta algo. Sem isto o
            # deploy quebrava tarde, dentro de runtime(), e a interface
            # mostrava apenas "Configuração inválida".
            faltando = [
                nome
                for nome, valor in (
                    ("SUPABASE_URL", self.supabase_url),
                    ("SUPABASE_PUBLISHABLE_KEY", self.supabase_publishable_key),
                    ("SUPABASE_SECRET_KEY", self.supabase_secret_key),
                    ("SUPABASE_DB_URL", self.supabase_db_url),
                )
                if not valor
            ]
            if faltando:
                raise RuntimeError(
                    "Configuração de produção incompleta. Defina nos Secrets: "
                    f"{', '.join(faltando)}. A identidade, os dados e os "
                    "arquivos vivem no Supabase. SUPABASE_SECRET_KEY é a chave "
                    "privilegiada que cria a conta do primeiro administrador e "
                    "redefine senha: sem ela o startup falharia adiante, ao "
                    "semear o banco, em vez de aqui."
                )
        if "@" in self.supabase_username_domain:
            raise ValueError("SUPABASE_USERNAME_DOMAIN deve ser só o domínio")
        for rotulo, valor in (
            ("STORAGE_MAX_TOTAL_BYTES", self.storage_max_total_bytes),
            ("STORAGE_MAX_MONTHLY_EGRESS_BYTES", self.storage_max_monthly_egress_bytes),
        ):
            if valor <= 0:
                raise ValueError(f"{rotulo} deve ser positivo")
        # Franquia gratuita: 1 GB de espaço e 10 GB de egress por mês. Passar
        # disso significa aceitar cobrança, então tem de ser explícito.
        if self.storage_max_total_bytes > 1024 * 1024 * 1024:
            raise ValueError(
                "STORAGE_MAX_TOTAL_BYTES acima de 1 GB ultrapassa a franquia "
                "gratuita do Supabase Storage e geraria cobrança"
            )
        if self.storage_max_monthly_egress_bytes > 10 * 1024 * 1024 * 1024:
            raise ValueError(
                "STORAGE_MAX_MONTHLY_EGRESS_BYTES acima de 10 GB ultrapassa a "
                "franquia gratuita e geraria cobrança"
            )
        # O plano gratuito recusa arquivo individual acima de 50 MB.
        if self.max_upload_bytes > 50 * 1024 * 1024:
            raise ValueError(
                "MAX_UPLOAD_BYTES acima de 50 MB excede o limite por arquivo "
                "do Supabase Storage no plano gratuito"
            )
        if not self.storage_bucket:
            raise ValueError("STORAGE_BUCKET não pode ficar vazio")
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
