from __future__ import annotations

from datetime import date, datetime, timezone
from enum import StrEnum
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum as SAEnum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


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


role_enum = SAEnum(Role, native_enum=False, length=16, validate_strings=True, create_constraint=True, name="role_enum")
status_enum = SAEnum(SubmissionStatus, native_enum=False, length=24, validate_strings=True, create_constraint=True, name="submission_status_enum")
check_enum = SAEnum(CheckOutcome, native_enum=False, length=12, validate_strings=True, create_constraint=True, name="check_outcome_enum")
duplicate_enum = SAEnum(DuplicateKind, native_enum=False, length=12, validate_strings=True, create_constraint=True, name="duplicate_kind_enum")
ledger_enum = SAEnum(LedgerKind, native_enum=False, length=32, validate_strings=True, create_constraint=True, name="ledger_kind_enum")


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class User(TimestampMixin, Base):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("oidc_issuer", "oidc_subject", name="uq_user_oidc_identity"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(160))
    role: Mapped[Role] = mapped_column(role_enum, default=Role.STUDENT)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    oidc_issuer: Mapped[str | None] = mapped_column(String(500))
    oidc_subject: Mapped[str | None] = mapped_column(String(500))

    submissions: Mapped[list["Submission"]] = relationship(
        back_populates="student", foreign_keys="Submission.student_id"
    )
    ledger_transactions: Mapped[list["LedgerTransaction"]] = relationship(
        back_populates="student", foreign_keys="LedgerTransaction.student_id"
    )


class Activity(TimestampMixin, Base):
    __tablename__ = "activities"
    __table_args__ = (
        CheckConstraint("points > 0", name="ck_activity_points_positive"),
        CheckConstraint("unit_threshold > 0", name="ck_activity_threshold_positive"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    code: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(180))
    points: Mapped[int] = mapped_column(Integer)
    unit_threshold: Mapped[int] = mapped_column(Integer, default=1)
    requires_images: Mapped[bool] = mapped_column(Boolean, default=True)
    requires_summary: Mapped[bool] = mapped_column(Boolean, default=False)
    requires_title_or_url: Mapped[bool] = mapped_column(Boolean, default=False)
    summary_min_chars: Mapped[int] = mapped_column(Integer, default=0)
    content_review_required: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_approvable: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    config_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class Submission(TimestampMixin, Base):
    __tablename__ = "submissions"
    __table_args__ = (
        CheckConstraint("declared_units >= 0", name="ck_submission_declared_units"),
        CheckConstraint("recognized_units >= 0", name="ck_submission_recognized_units"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_submission_confidence"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    student_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    activity_id: Mapped[str] = mapped_column(ForeignKey("activities.id"), index=True)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    status: Mapped[SubmissionStatus] = mapped_column(
        status_enum, default=SubmissionStatus.PROCESSING, index=True
    )
    title: Mapped[str | None] = mapped_column(String(500))
    url: Mapped[str | None] = mapped_column(String(2048))
    summary: Mapped[str | None] = mapped_column(Text)
    ocr_text: Mapped[str] = mapped_column(Text, default="")
    detected_platform: Mapped[str | None] = mapped_column(String(80))
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    declared_units: Mapped[int] = mapped_column(Integer, default=1)
    recognized_units: Mapped[int] = mapped_column(Integer, default=0)
    admin_reason: Mapped[str | None] = mapped_column(Text)
    points_awarded: Mapped[int] = mapped_column(Integer, default=0)
    rule_snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_by_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"))

    student: Mapped[User] = relationship(
        back_populates="submissions", foreign_keys=[student_id]
    )
    activity: Mapped[Activity] = relationship()
    images: Mapped[list["SubmissionImage"]] = relationship(
        back_populates="submission", cascade="all, delete-orphan"
    )
    checks: Mapped[list["RuleCheck"]] = relationship(
        back_populates="submission", cascade="all, delete-orphan"
    )

    __mapper_args__ = {"version_id_col": version}


class SubmissionImage(Base):
    __tablename__ = "submission_images"
    __table_args__ = (
        CheckConstraint("size_bytes > 0", name="ck_image_size_positive"),
        CheckConstraint("width > 0 AND height > 0", name="ck_image_dimensions_positive"),
        Index("ix_submission_image_sha256", "sha256"),
        Index("ix_submission_image_phash", "phash"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    submission_id: Mapped[str] = mapped_column(
        ForeignKey("submissions.id", ondelete="CASCADE"), index=True
    )
    storage_key: Mapped[str] = mapped_column(String(255), unique=True)
    client_filename: Mapped[str | None] = mapped_column(String(255))
    mime_type: Mapped[str] = mapped_column(String(80))
    image_format: Mapped[str] = mapped_column(String(16))
    size_bytes: Mapped[int] = mapped_column(Integer)
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    phash: Mapped[str] = mapped_column(String(64))
    laplacian_variance: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )

    submission: Mapped[Submission] = relationship(back_populates="images")


class RuleCheck(Base):
    __tablename__ = "rule_checks"
    __table_args__ = (
        CheckConstraint("score >= 0 AND score <= 1", name="ck_rule_check_score"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    submission_id: Mapped[str] = mapped_column(
        ForeignKey("submissions.id", ondelete="CASCADE"), index=True
    )
    image_id: Mapped[str | None] = mapped_column(
        ForeignKey("submission_images.id", ondelete="CASCADE"), index=True
    )
    rule_name: Mapped[str] = mapped_column(String(100))
    outcome: Mapped[CheckOutcome] = mapped_column(check_enum)
    required: Mapped[bool] = mapped_column(Boolean, default=True)
    score: Mapped[float] = mapped_column(Float, default=0.0)
    message: Mapped[str] = mapped_column(Text)
    details_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )

    submission: Mapped[Submission] = relationship(back_populates="checks")


class DuplicateMatch(Base):
    __tablename__ = "duplicate_matches"
    __table_args__ = (
        UniqueConstraint(
            "image_id", "matched_image_id", "kind", name="uq_duplicate_pair_kind"
        ),
        CheckConstraint(
            "image_id <> matched_image_id", name="ck_duplicate_distinct_images"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    image_id: Mapped[str] = mapped_column(
        ForeignKey("submission_images.id", ondelete="CASCADE"), index=True
    )
    matched_image_id: Mapped[str] = mapped_column(
        ForeignKey("submission_images.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[DuplicateKind] = mapped_column(duplicate_enum)
    distance: Mapped[int | None] = mapped_column(Integer)
    same_student: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class ApprovedEvidence(Base):
    """Atomic claim that an exact image may contribute to scoring only once."""

    __tablename__ = "approved_evidence"

    sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    image_id: Mapped[str] = mapped_column(
        ForeignKey("submission_images.id"), unique=True, nullable=False
    )
    submission_id: Mapped[str] = mapped_column(
        ForeignKey("submissions.id"), index=True, nullable=False
    )
    student_id: Mapped[str] = mapped_column(
        ForeignKey("users.id"), index=True, nullable=False
    )
    claimed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class LedgerTransaction(Base):
    __tablename__ = "ledger_transactions"
    __table_args__ = (
        CheckConstraint("points <> 0", name="ck_ledger_nonzero_points"),
        Index("ix_ledger_student_occurred", "student_id", "occurred_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    student_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    points: Mapped[int] = mapped_column(Integer)
    kind: Mapped[LedgerKind] = mapped_column(ledger_enum, index=True)
    source_type: Mapped[str] = mapped_column(String(80))
    source_id: Mapped[str | None] = mapped_column(String(160))
    source_key: Mapped[str] = mapped_column(String(500), unique=True, index=True)
    activity_id: Mapped[str | None] = mapped_column(ForeignKey("activities.id"))
    submission_id: Mapped[str | None] = mapped_column(ForeignKey("submissions.id"))
    description: Mapped[str | None] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    created_by_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"))

    student: Mapped[User] = relationship(
        back_populates="ledger_transactions", foreign_keys=[student_id]
    )


class LessonUnit(Base):
    __tablename__ = "lesson_units"
    __table_args__ = (
        UniqueConstraint("submission_id", "unit_index", name="uq_lesson_submission_unit"),
        CheckConstraint("unit_index > 0", name="ck_lesson_unit_index_positive"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    submission_id: Mapped[str] = mapped_column(
        ForeignKey("submissions.id"), index=True
    )
    student_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    activity_group: Mapped[str] = mapped_column(
        String(80), default="duolingo_beconfident", index=True
    )
    unit_index: Mapped[int] = mapped_column(Integer)
    approved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )


class LessonBatch(Base):
    __tablename__ = "lesson_batches"
    __table_args__ = (
        UniqueConstraint(
            "student_id", "activity_group", "sequence", name="uq_lesson_batch_sequence"
        ),
        CheckConstraint("sequence > 0", name="ck_lesson_batch_sequence_positive"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    student_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    activity_group: Mapped[str] = mapped_column(
        String(80), default="duolingo_beconfident"
    )
    sequence: Mapped[int] = mapped_column(Integer)
    ledger_transaction_id: Mapped[str] = mapped_column(
        ForeignKey("ledger_transactions.id"), unique=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class LessonBatchUnit(Base):
    __tablename__ = "lesson_batch_units"

    batch_id: Mapped[str] = mapped_column(
        ForeignKey("lesson_batches.id", ondelete="CASCADE"), primary_key=True
    )
    unit_id: Mapped[str] = mapped_column(
        ForeignKey("lesson_units.id"), primary_key=True, unique=True
    )


class Meeting(Base):
    __tablename__ = "meetings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    student_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    meeting_date: Mapped[date] = mapped_column(Date, index=True)
    description: Mapped[str] = mapped_column(Text)
    confirmed_by_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    ledger_transaction_id: Mapped[str] = mapped_column(
        ForeignKey("ledger_transactions.id"), unique=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (Index("ix_audit_entity", "entity_type", "entity_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    actor_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), index=True)
    action: Mapped[str] = mapped_column(String(100), index=True)
    entity_type: Mapped[str] = mapped_column(String(80))
    entity_id: Mapped[str] = mapped_column(String(160))
    reason: Mapped[str | None] = mapped_column(Text)
    before_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    after_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )


class ImportRun(Base):
    __tablename__ = "import_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    namespace: Mapped[str] = mapped_column(String(160), index=True)
    source_path: Mapped[str] = mapped_column(String(1000))
    source_sha256: Mapped[str] = mapped_column(String(64))
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    imported_count: Mapped[int] = mapped_column(Integer, default=0)
    skipped_count: Mapped[int] = mapped_column(Integer, default=0)
    inconsistent_count: Mapped[int] = mapped_column(Integer, default=0)
    report_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class ImportRecord(Base):
    __tablename__ = "import_records"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("import_runs.id"), index=True)
    external_key: Mapped[str] = mapped_column(String(500), unique=True, index=True)
    student_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    ledger_transaction_id: Mapped[str] = mapped_column(
        ForeignKey("ledger_transactions.id"), unique=True
    )
    source_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
