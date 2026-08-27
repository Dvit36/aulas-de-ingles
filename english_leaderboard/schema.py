"""Modelos ORM da arquitetura oficial, espelhando o schema do PostgreSQL.

Fonte única do modelo de dados. ``models.py`` descreve o SQLite legado e
importa daqui os enums, para os dois lados compararem o *mesmo* objeto; assim
o legado sai por remoção pura, sem tocar em quem já migrou.

Três diferenças estruturais em relação ao legado:

* ``profiles`` substitui ``users``. O id é o UUID de ``auth.users``; não há
  coluna de senha, sessão ou tentativa de login — isso é do Supabase Auth.
* ``submission_files`` unifica o que eram ``submission_images`` e
  ``submission_files``, e guarda a referência ao objeto no Storage em vez do
  caminho local.
* Os enums são nativos do PostgreSQL e guardam o **valor** minúsculo. O
  SQLAlchemy grava o *nome* por padrão, o que produziria ``APPROVED_AUTO`` em
  uma coluna que só aceita ``approved_auto``; ``values_callable`` corrige.

Os carimbos de tempo têm padrão dos dois lados de propósito. O ``server_default``
cobre quem insere por SQL direto; o ``default=utcnow`` do Python garante
resolução de microssegundo — o ``CURRENT_TIMESTAMP`` do SQLite só tem segundo,
e dois registros criados no mesmo segundo empatariam, tornando indefinida
qualquer ordenação por data (a trilha de auditoria depende disso).
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import Uuid


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return str(uuid4())


class Role(StrEnum):
    STUDENT = "student"
    ADMIN = "admin"


class SubmissionStatus(StrEnum):
    PROCESSING = "processing"
    APPROVED_AUTO = "approved_auto"
    NEEDS_REVIEW = "needs_review"
    APPROVED_MANUAL = "approved_manual"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class CheckOutcome(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    REVIEW = "review"


class DuplicateKind(StrEnum):
    EXACT = "exact"
    SIMILAR = "similar"


class LedgerKind(StrEnum):
    DIRECT_ACTIVITY = "direct_activity"
    LESSON_BATCH = "lesson_batch"
    MEETING = "meeting"
    INITIAL_BALANCE = "initial_balance"
    IMPORTED_DAILY_SCORE = "imported_daily_score"
    ADJUSTMENT = "adjustment"


class EmailAttemptStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"
    SKIPPED = "skipped"


class SchemaBase(DeclarativeBase):
    """Registro das tabelas da arquitetura oficial."""


def _uuid():
    """UUID portátil, com a mesma representação em texto nos dois bancos.

    O tipo ``Uuid`` do SQLAlchemy guarda os 32 dígitos **sem hífen** no SQLite,
    enquanto o Python carrega a forma com hífen. Quem escreve pelo ORM e quem
    escreve por SQL direto — o serviço de Storage faz isso — passariam a gravar
    valores diferentes para a mesma chave, e as estrangeiras não fechariam.
    Fixar ``String(36)`` no SQLite mantém as duas escritas idênticas; no
    PostgreSQL o tipo nativo continua valendo.
    """

    return Uuid(as_uuid=False).with_variant(String(36), "sqlite")


def _json():
    """JSON portátil: JSONB no PostgreSQL, JSON genérico no SQLite dos testes.

    A suíte roda em SQLite em memória. Sem a variante, o mesmo modelo não
    poderia ser exercitado pelos testes e pelo banco de produção.
    """

    return JSON().with_variant(JSONB(), "postgresql")


def _enum(tipo, nome: str):
    """Enum nativo do PostgreSQL guardando o valor, não o nome do membro."""

    return SAEnum(
        tipo,
        name=nome,
        create_type=False,
        values_callable=lambda membros: [membro.value for membro in membros],
    )


class Profile(SchemaBase):
    """Perfil do usuário. A identidade vive em ``auth.users``."""

    __tablename__ = "profiles"

    id: Mapped[str] = mapped_column(_uuid(), primary_key=True)
    username: Mapped[str] = mapped_column(Text)
    display_name: Mapped[str] = mapped_column(Text)
    role: Mapped[Role] = mapped_column(Text, default=Role.STUDENT.value)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reminders_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow,
        server_default=func.now()
    )

    submissions: Mapped[list[Submission]] = relationship(
        back_populates="student", foreign_keys="Submission.student_id"
    )


class Activity(SchemaBase):
    __tablename__ = "activities"

    id: Mapped[str] = mapped_column(_uuid(), primary_key=True, default=new_id)
    code: Mapped[str] = mapped_column(Text, unique=True)
    name: Mapped[str] = mapped_column(Text)
    points: Mapped[int] = mapped_column(Integer)
    unit_threshold: Mapped[int] = mapped_column(Integer, default=1)
    requires_images: Mapped[bool] = mapped_column(Boolean, default=True)
    requires_summary: Mapped[bool] = mapped_column(Boolean, default=False)
    requires_title_or_url: Mapped[bool] = mapped_column(Boolean, default=False)
    summary_min_chars: Mapped[int] = mapped_column(Integer, default=0)
    # Revisão humana de conteúdo é exceção pedida pelo catálogo, não regra:
    # o padrão `true` impediria qualquer aprovação automática.
    content_review_required: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_approvable: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    config_json: Mapped[dict[str, Any]] = mapped_column("config_json", _json(), default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow,
        server_default=func.now()
    )


class Resource(SchemaBase):
    __tablename__ = "resources"

    id: Mapped[str] = mapped_column(_uuid(), primary_key=True, default=new_id)
    title: Mapped[str] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text, default="")
    position: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow,
        server_default=func.now()
    )


class GoalConfiguration(SchemaBase):
    __tablename__ = "goal_configuration"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    weekly_lesson_goal: Mapped[int] = mapped_column(Integer, default=5)
    updated_by_id: Mapped[str | None] = mapped_column(
        _uuid(), ForeignKey("profiles.id", ondelete="SET NULL")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow,
        server_default=func.now()
    )


class Submission(SchemaBase):
    __tablename__ = "submissions"

    id: Mapped[str] = mapped_column(_uuid(), primary_key=True, default=new_id)
    student_id: Mapped[str] = mapped_column(
        _uuid(), ForeignKey("profiles.id", ondelete="CASCADE"), index=True
    )
    activity_id: Mapped[str] = mapped_column(
        _uuid(), ForeignKey("activities.id")
    )
    status: Mapped[SubmissionStatus] = mapped_column(
        _enum(SubmissionStatus, "submission_status"),
        default=SubmissionStatus.PROCESSING,
    )
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_by_id: Mapped[str | None] = mapped_column(
        _uuid(), ForeignKey("profiles.id", ondelete="SET NULL")
    )
    title: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    ocr_text: Mapped[str | None] = mapped_column(Text)
    detected_platform: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    declared_units: Mapped[int] = mapped_column(Integer, default=0)
    recognized_units: Mapped[int] = mapped_column(Integer, default=0)
    points_awarded: Mapped[int] = mapped_column(Integer, default=0)
    # Nomes de atributo preservados do legado para os serviços não mudarem.
    rule_snapshot_json: Mapped[dict[str, Any]] = mapped_column(
        "rule_snapshot", _json(), default=dict
    )
    admin_reason: Mapped[str | None] = mapped_column("review_note", Text)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow,
        server_default=func.now()
    )

    student: Mapped[Profile] = relationship(
        back_populates="submissions", foreign_keys=[student_id]
    )
    activity: Mapped[Activity] = relationship()
    files: Mapped[list[SubmissionFile]] = relationship(
        back_populates="submission",
        cascade="all, delete-orphan",
        order_by="SubmissionFile.position",
    )
    checks: Mapped[list[RuleCheck]] = relationship(
        back_populates="submission", cascade="all, delete-orphan"
    )

    __mapper_args__ = {"version_id_col": version}

    @property
    def images(self) -> list[SubmissionFile]:
        """Compatibilidade: o legado separava imagem de documento."""

        return [item for item in self.files if item.content_type.startswith("image/")]


class SubmissionFile(SchemaBase):
    """Metadados do arquivo. O binário fica no Supabase Storage."""

    __tablename__ = "submission_files"
    __table_args__ = (
        UniqueConstraint(
            "storage_provider", "storage_bucket", "storage_key",
            name="submission_files_storage_provider_storage_bucket_storage_key_key",
        ),
        Index("submission_files_checksum", "checksum_sha256"),
    )

    id: Mapped[str] = mapped_column(_uuid(), primary_key=True, default=new_id)
    submission_id: Mapped[str] = mapped_column(
        _uuid(), ForeignKey("submissions.id", ondelete="CASCADE"), index=True
    )
    student_id: Mapped[str] = mapped_column(
        _uuid(), ForeignKey("profiles.id", ondelete="CASCADE"), index=True
    )
    filename: Mapped[str] = mapped_column(Text)
    storage_provider: Mapped[str] = mapped_column(
        Text, default="supabase", server_default=text("'supabase'")
    )
    storage_bucket: Mapped[str] = mapped_column(
        Text, default="student-files", server_default=text("'student-files'")
    )
    storage_key: Mapped[str] = mapped_column(Text)
    content_type: Mapped[str] = mapped_column(Text)
    file_size: Mapped[int] = mapped_column(Integer)
    checksum_sha256: Mapped[str] = mapped_column(String(64))
    phash: Mapped[str | None] = mapped_column(Text)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    page_count: Mapped[int | None] = mapped_column(Integer)
    ocr_text: Mapped[str | None] = mapped_column(Text)
    position: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now()
    )

    submission: Mapped[Submission] = relationship(back_populates="files")

    @property
    def file_kind(self) -> str:
        """Classificação derivada do tipo MIME.

        Deixou de ser coluna: guardar a categoria ao lado do ``content_type``
        permitiria os dois discordarem. Aqui ela é sempre consistente.
        """

        tipo = (self.content_type or "").lower()
        if tipo.startswith("image/"):
            return "image"
        if tipo == "application/pdf":
            return "pdf"
        if "wordprocessingml" in tipo or tipo == "application/msword":
            return "docx"
        if tipo.startswith("text/"):
            return "txt"
        return "file"

    # Nomes do legado, para reduzir a mudança nos serviços.
    @property
    def client_filename(self) -> str:
        return self.filename

    @property
    def sha256(self) -> str:
        return self.checksum_sha256

    @property
    def mime_type(self) -> str:
        return self.content_type

    @property
    def size_bytes(self) -> int:
        return self.file_size


class RuleCheck(SchemaBase):
    __tablename__ = "rule_checks"

    id: Mapped[str] = mapped_column(_uuid(), primary_key=True, default=new_id)
    submission_id: Mapped[str] = mapped_column(
        _uuid(), ForeignKey("submissions.id", ondelete="CASCADE"), index=True
    )
    rule_name: Mapped[str] = mapped_column("name", Text)
    outcome: Mapped[CheckOutcome] = mapped_column(Text)
    required: Mapped[bool] = mapped_column(Boolean, default=True)
    score: Mapped[float] = mapped_column(Float, default=0.0)
    message: Mapped[str | None] = mapped_column(Text)
    details_json: Mapped[dict[str, Any]] = mapped_column("details", _json(), default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now()
    )

    submission: Mapped[Submission] = relationship(back_populates="checks")


class DuplicateMatch(SchemaBase):
    __tablename__ = "duplicate_matches"

    id: Mapped[str] = mapped_column(_uuid(), primary_key=True, default=new_id)
    submission_id: Mapped[str] = mapped_column(
        _uuid(), ForeignKey("submissions.id", ondelete="CASCADE"), index=True
    )
    file_id: Mapped[str | None] = mapped_column(
        _uuid(), ForeignKey("submission_files.id", ondelete="SET NULL")
    )
    matched_file_id: Mapped[str | None] = mapped_column(
        _uuid(), ForeignKey("submission_files.id", ondelete="SET NULL")
    )
    kind: Mapped[DuplicateKind] = mapped_column(Text)
    distance: Mapped[int | None] = mapped_column(Integer)
    same_student: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now()
    )


class ApprovedEvidence(SchemaBase):
    """Claim único por checksum: fecha corrida de reenvio idêntico."""

    __tablename__ = "approved_evidence"

    checksum_sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    submission_id: Mapped[str] = mapped_column(
        _uuid(), ForeignKey("submissions.id", ondelete="CASCADE")
    )
    student_id: Mapped[str] = mapped_column(
        _uuid(), ForeignKey("profiles.id", ondelete="CASCADE")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now()
    )


class LessonUnit(SchemaBase):
    __tablename__ = "lesson_units"
    __table_args__ = (
        UniqueConstraint("submission_id", "unit_index"),
        CheckConstraint("unit_index > 0", name="lesson_units_unit_index_check"),
    )

    id: Mapped[str] = mapped_column(_uuid(), primary_key=True, default=new_id)
    submission_id: Mapped[str] = mapped_column(
        _uuid(), ForeignKey("submissions.id", ondelete="CASCADE")
    )
    student_id: Mapped[str] = mapped_column(
        _uuid(), ForeignKey("profiles.id", ondelete="CASCADE"), index=True
    )
    activity_group: Mapped[str] = mapped_column(Text)
    unit_index: Mapped[int] = mapped_column(Integer)
    approved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class LessonBatch(SchemaBase):
    __tablename__ = "lesson_batches"
    __table_args__ = (UniqueConstraint("student_id", "activity_group", "sequence"),)

    id: Mapped[str] = mapped_column(_uuid(), primary_key=True, default=new_id)
    student_id: Mapped[str] = mapped_column(
        _uuid(), ForeignKey("profiles.id", ondelete="CASCADE")
    )
    activity_group: Mapped[str] = mapped_column(Text)
    sequence: Mapped[int] = mapped_column(Integer)
    # Rastreabilidade: qual lançamento pagou este lote.
    ledger_transaction_id: Mapped[str | None] = mapped_column(
        _uuid(),
        ForeignKey("ledger_transactions.id", ondelete="RESTRICT"),
        unique=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now()
    )


class LessonBatchUnit(SchemaBase):
    __tablename__ = "lesson_batch_units"

    unit_id: Mapped[str] = mapped_column(
        _uuid(),
        ForeignKey("lesson_units.id", ondelete="CASCADE"),
        primary_key=True,
    )
    batch_id: Mapped[str] = mapped_column(
        _uuid(), ForeignKey("lesson_batches.id", ondelete="CASCADE")
    )


class LedgerTransaction(SchemaBase):
    """Imutável por gatilho no banco: correção é lançamento compensatório."""

    __tablename__ = "ledger_transactions"

    id: Mapped[str] = mapped_column(_uuid(), primary_key=True, default=new_id)
    student_id: Mapped[str] = mapped_column(
        _uuid(), ForeignKey("profiles.id"), index=True
    )
    points: Mapped[int] = mapped_column(Integer)
    kind: Mapped[LedgerKind] = mapped_column(_enum(LedgerKind, "ledger_kind"))
    source_type: Mapped[str] = mapped_column(Text)
    source_id: Mapped[str | None] = mapped_column(_uuid())
    source_key: Mapped[str] = mapped_column(Text, unique=True)
    activity_id: Mapped[str | None] = mapped_column(
        _uuid(), ForeignKey("activities.id", ondelete="SET NULL")
    )
    submission_id: Mapped[str | None] = mapped_column(
        _uuid(), ForeignKey("submissions.id", ondelete="SET NULL")
    )
    description: Mapped[str | None] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    created_by_id: Mapped[str | None] = mapped_column(
        _uuid(), ForeignKey("profiles.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now()
    )


class AuditLog(SchemaBase):
    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(_uuid(), primary_key=True, default=new_id)
    actor_id: Mapped[str | None] = mapped_column(
        _uuid(), ForeignKey("profiles.id", ondelete="SET NULL")
    )
    action: Mapped[str] = mapped_column(Text)
    entity_type: Mapped[str] = mapped_column(Text)
    entity_id: Mapped[str | None] = mapped_column(Text)
    before_json: Mapped[dict[str, Any] | None] = mapped_column(_json())
    after_json: Mapped[dict[str, Any] | None] = mapped_column(_json())
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now()
    )


class StorageOrphan(SchemaBase):
    __tablename__ = "storage_orphans"

    id: Mapped[str] = mapped_column(_uuid(), primary_key=True, default=new_id)
    storage_provider: Mapped[str] = mapped_column(
        Text, default="supabase", server_default=text("'supabase'")
    )
    storage_bucket: Mapped[str] = mapped_column(
        Text, default="student-files", server_default=text("'student-files'")
    )
    storage_key: Mapped[str] = mapped_column(Text)
    student_id: Mapped[str | None] = mapped_column(
        _uuid(), ForeignKey("profiles.id", ondelete="SET NULL")
    )
    reason: Mapped[str] = mapped_column(Text)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now()
    )


class StorageUsage(SchemaBase):
    __tablename__ = "storage_usage"

    period: Mapped[str] = mapped_column(Text, primary_key=True)
    # server_default além do default do ORM: os contadores são atualizados por
    # SQL direto (upsert), que não passa pelo Python e precisa do padrão no
    # próprio banco.
    egress_bytes: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    upload_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    download_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow,
        server_default=func.now()
    )


class ReminderConfiguration(SchemaBase):
    """Linha única: a configuração dos lembretes é global."""

    __tablename__ = "reminder_configuration"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    frequency: Mapped[str] = mapped_column(Text, default="weekly")
    weekday: Mapped[int] = mapped_column(Integer, default=1)
    send_hour: Mapped[int] = mapped_column(Integer, default=9)
    timezone_name: Mapped[str] = mapped_column(Text, default="America/Sao_Paulo")
    inactive_days: Mapped[int] = mapped_column(Integer, default=7)
    subject_template: Mapped[str] = mapped_column(
        Text, default="Lembrete de atividades de inglês"
    )
    body_template: Mapped[str] = mapped_column(
        Text,
        default=(
            "Olá, {name}! Sentimos sua falta nas atividades de inglês. "
            "Acesse a plataforma para enviar uma nova atividade."
        ),
    )
    audience: Mapped[str] = mapped_column(Text, default="inactive_students")
    updated_by_id: Mapped[str | None] = mapped_column(
        _uuid(), ForeignKey("profiles.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow,
        server_default=func.now()
    )


class EmailAttempt(SchemaBase):
    """Tentativa de envio. ``dedupe_key`` impede reenvio do mesmo lembrete."""

    __tablename__ = "email_attempts"

    id: Mapped[str] = mapped_column(_uuid(), primary_key=True, default=new_id)
    user_id: Mapped[str | None] = mapped_column(
        _uuid(), ForeignKey("profiles.id", ondelete="SET NULL"), index=True
    )
    recipient_email: Mapped[str] = mapped_column(Text, default="")
    subject: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)
    status: Mapped[EmailAttemptStatus] = mapped_column(
        _enum(EmailAttemptStatus, "email_attempt_status"),
        default=EmailAttemptStatus.PENDING,
    )
    dedupe_key: Mapped[str] = mapped_column(Text, unique=True)
    dry_run: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)
    scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now()
    )


class ImportRun(SchemaBase):
    __tablename__ = "import_runs"

    id: Mapped[str] = mapped_column(_uuid(), primary_key=True, default=new_id)
    namespace: Mapped[str] = mapped_column(Text, index=True)
    source_path: Mapped[str] = mapped_column(Text)
    source_sha256: Mapped[str] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    imported_count: Mapped[int] = mapped_column(Integer, default=0)
    skipped_count: Mapped[int] = mapped_column(Integer, default=0)
    inconsistent_count: Mapped[int] = mapped_column(Integer, default=0)
    report_json: Mapped[dict[str, Any]] = mapped_column(_json(), default=dict)


class ImportRecord(SchemaBase):
    """``external_key`` único é o que torna a reimportação idempotente."""

    __tablename__ = "import_records"

    id: Mapped[str] = mapped_column(_uuid(), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(
        _uuid(), ForeignKey("import_runs.id", ondelete="CASCADE"), index=True
    )
    external_key: Mapped[str] = mapped_column(Text, unique=True, index=True)
    student_id: Mapped[str] = mapped_column(
        _uuid(), ForeignKey("profiles.id", ondelete="CASCADE"), index=True
    )
    ledger_transaction_id: Mapped[str] = mapped_column(
        _uuid(),
        ForeignKey("ledger_transactions.id", ondelete="RESTRICT"),
        unique=True,
    )
    source_json: Mapped[dict[str, Any]] = mapped_column(_json(), default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now()
    )


# Nome legado, para os serviços continuarem lendo ``User`` durante a troca.
User = Profile

__all__ = [
    "Activity",
    "ApprovedEvidence",
    "AuditLog",
    "CheckOutcome",
    "DuplicateKind",
    "DuplicateMatch",
    "EmailAttempt",
    "EmailAttemptStatus",
    "GoalConfiguration",
    "ImportRecord",
    "ImportRun",
    "LedgerKind",
    "LedgerTransaction",
    "LessonBatch",
    "LessonBatchUnit",
    "LessonUnit",
    "Profile",
    "ReminderConfiguration",
    "Resource",
    "Role",
    "RuleCheck",
    "SchemaBase",
    "StorageOrphan",
    "StorageUsage",
    "Submission",
    "SubmissionFile",
    "SubmissionStatus",
    "User",
    "new_id",
    "utcnow",
]
