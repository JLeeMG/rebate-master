"""Database schema of the rebate master.

Every row is keyed by identity - entity code, customer group code, brand code,
NetSuite internal id - never by position (spec §2.4).

Changing this file needs a matching Alembic migration in migrations/versions.
tests/test_migrations.py fails if the two disagree.
"""

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    LargeBinary,
    MetaData,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from mgrm.auth.roles import Role

NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def text_enum(enum_cls: type[StrEnum], name: str) -> Enum:
    """Store an enum as its text value, with a CHECK constraint, not a native PG type."""
    return Enum(
        enum_cls,
        name=name,
        native_enum=False,
        create_constraint=True,
        length=32,
        values_callable=lambda cls: [member.value for member in cls],
    )


def first_of_month(column: str) -> CheckConstraint:
    return CheckConstraint(f"extract(day from {column}) = 1", name=f"{column}_is_first_of_month")


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)


# ---------------------------------------------------------------- registers (this platform is their master)


class EntityRow(Base):
    __tablename__ = "entity"

    code: Mapped[str] = mapped_column(String(8), primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    netsuite_subsidiary_id: Mapped[int] = mapped_column(Integer, unique=True)
    functional_currency: Mapped[str] = mapped_column(String(3))


class ForecastTier(StrEnum):
    STRATEGIC = "strategic"
    TAIL = "tail"
    PRE_REVENUE = "pre_revenue"


class Brand(Base):
    """Spec §4.2. One row per NetSuite class; not every class is a brand."""

    __tablename__ = "brand"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(100))
    netsuite_class_id: Mapped[int | None] = mapped_column(Integer, unique=True)
    is_brand: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    forecast_tier: Mapped[ForecastTier | None] = mapped_column(text_enum(ForecastTier, "forecast_tier"))
    notes: Mapped[str] = mapped_column(Text, default="", server_default="")
    is_inactive: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")


class CustomerGroup(Base):
    """Spec §4.3. Rebates, debtor days and concentration all work at this level."""

    __tablename__ = "customer_group"

    code: Mapped[str] = mapped_column(String(16), primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    is_intercompany: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")


class Customer(Base):
    """A NetSuite customer record. An unmapped customer has no group and is reported, never bucketed."""

    __tablename__ = "customer"

    id: Mapped[int] = mapped_column(primary_key=True)
    netsuite_customer_id: Mapped[int] = mapped_column(Integer, unique=True)
    name: Mapped[str] = mapped_column(String(200))
    entity_code: Mapped[str] = mapped_column(ForeignKey("entity.code"))
    customer_group_code: Mapped[str | None] = mapped_column(ForeignKey("customer_group.code"))
    netsuite_entity_id: Mapped[str] = mapped_column(String(100), default="", server_default="")
    category: Mapped[str] = mapped_column(String(100), default="", server_default="")
    parent_name: Mapped[str] = mapped_column(String(200), default="", server_default="")
    terms: Mapped[str] = mapped_column(String(50), default="", server_default="")
    is_inactive: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")


# ---------------------------------------------------------------- people


class AuthMethod(StrEnum):
    LOCAL = "local"
    MICROSOFT = "microsoft"


class AppUser(Base):
    __tablename__ = "app_user"
    __table_args__ = (
        CheckConstraint("auth_method <> 'local' OR password_hash IS NOT NULL", name="local_user_has_password"),
        CheckConstraint("email = lower(email)", name="email_is_lower_case"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(254), unique=True)
    display_name: Mapped[str] = mapped_column(String(100))
    role: Mapped[Role] = mapped_column(text_enum(Role, "role"))
    auth_method: Mapped[AuthMethod] = mapped_column(text_enum(AuthMethod, "auth_method"))
    password_hash: Mapped[str | None] = mapped_column(String(200))
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("app_user.id"))
    last_sign_in_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditEvent(Base):
    """Who did what, when, and why. Append-only: the database refuses updates and deletions."""

    __tablename__ = "audit_event"

    id: Mapped[int] = mapped_column(primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("app_user.id"))
    action: Mapped[str] = mapped_column(String(64))
    subject: Mapped[str] = mapped_column(String(200))
    detail: Mapped[dict] = mapped_column(JSON, default=dict)


# ---------------------------------------------------------------- loads


class LoadKind(StrEnum):
    CUSTOMERS = "customers"
    CLASSES = "classes"
    REBATE_WORKBOOK = "rebate_workbook"
    FORECASTING_IMPORT = "forecasting_import"  # the one-time move from the forecasting platform, 8 Oct 2026


class LoadBatch(Base):
    """One file loaded into the platform."""

    __tablename__ = "load_batch"
    __table_args__ = (UniqueConstraint("kind", "entity_code", "file_sha256", name="uq_load_batch_kind_entity_file"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[LoadKind] = mapped_column(text_enum(LoadKind, "load_kind"))
    entity_code: Mapped[str | None] = mapped_column(ForeignKey("entity.code"))
    source: Mapped[str] = mapped_column(String(200))
    file_name: Mapped[str] = mapped_column(String(300))
    file_sha256: Mapped[str] = mapped_column(String(64))
    row_count: Mapped[int] = mapped_column(Integer)
    loaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    loaded_by_id: Mapped[int | None] = mapped_column(ForeignKey("app_user.id"))
    summary: Mapped[dict] = mapped_column(JSON, default=dict)


# ---------------------------------------------------------------- rebate master (spec §4.5, §5.3)


class RateType(StrEnum):
    REBATE = "rebate"
    MDF = "mdf"
    DAMAGE = "damage"
    SETTLEMENT = "settlement"
    CO_OP = "co_op"


class RebateBasis(StrEnum):
    REBATE_ELIGIBLE_SALES = "rebate_eligible_sales"
    GROSS_REVENUE = "gross_revenue"


class AccrualMode(StrEnum):
    ACCRUE = "accrue"
    CHECK_ONLY = "check_only"  # e.g. the Honor tabs marked "DO NOT ACCRUE PURELY FOR CHECK"


class ReviewStatus(StrEnum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"


class RebateAgreement(Base):
    """One agreement: entity x customer group x brand (or product scope) x rate type.

    Stacked agreements on the same sales base are separate rows (spec §4.5.1).
    The rate lives in rebate_rate, never here, so its history is kept.
    """

    __tablename__ = "rebate_agreement"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(80), unique=True)
    entity_code: Mapped[str] = mapped_column(ForeignKey("entity.code"))
    customer_label: Mapped[str] = mapped_column(String(200))
    customer_group_code: Mapped[str | None] = mapped_column(ForeignKey("customer_group.code"))
    brand_code: Mapped[str | None] = mapped_column(String(64))
    product_scope: Mapped[str] = mapped_column(String(300))
    rate_type: Mapped[RateType] = mapped_column(text_enum(RateType, "rate_type"))
    basis: Mapped[RebateBasis] = mapped_column(text_enum(RebateBasis, "rebate_basis"))
    accrual_mode: Mapped[AccrualMode] = mapped_column(text_enum(AccrualMode, "accrual_mode"), default=AccrualMode.ACCRUE)
    source_reference: Mapped[str] = mapped_column(Text)
    agreed_by: Mapped[str] = mapped_column(String(200), default="", server_default="")
    notes: Mapped[str] = mapped_column(Text, default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("app_user.id"))


class RebateRate(Base):
    """A rate for a period. Nothing is overwritten: a change closes one row and opens another.

    Four-eyes (spec §4.5.2): a row starts proposed; only someone other than the
    person who entered it may approve it. A reason is always required. The
    database enforces these and the immutability of a reviewed row.
    """

    __tablename__ = "rebate_rate"
    __table_args__ = (
        CheckConstraint("rate >= 0 AND rate < 1", name="rate_is_a_fraction"),
        CheckConstraint("effective_to IS NULL OR effective_to >= effective_from", name="dates_in_order"),
        CheckConstraint("reviewed_by_id IS NULL OR reviewed_by_id <> entered_by_id", name="four_eyes"),
        CheckConstraint(
            "status = 'proposed' OR (reviewed_by_id IS NOT NULL AND reviewed_at IS NOT NULL)",
            name="decision_records_reviewer",
        ),
        CheckConstraint("band_from IS NULL OR band_to IS NULL OR band_to > band_from", name="band_in_order"),
        CheckConstraint("length(trim(source_reference)) > 0", name="source_stated"),
        CheckConstraint("length(trim(reason)) > 0", name="reason_stated"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    agreement_id: Mapped[int] = mapped_column(ForeignKey("rebate_agreement.id"))
    rate: Mapped[Decimal] = mapped_column(Numeric(9, 6))
    effective_from: Mapped[date] = mapped_column(Date)
    effective_to: Mapped[date | None] = mapped_column(Date)
    band_from: Mapped[Decimal | None] = mapped_column(Numeric(20, 2))
    band_to: Mapped[Decimal | None] = mapped_column(Numeric(20, 2))
    status: Mapped[ReviewStatus] = mapped_column(text_enum(ReviewStatus, "review_status"), default=ReviewStatus.PROPOSED)
    source_reference: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text)
    load_flags: Mapped[list] = mapped_column(JSON, default=list)
    entered_by_id: Mapped[int] = mapped_column(ForeignKey("app_user.id"))
    entered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    reviewed_by_id: Mapped[int | None] = mapped_column(ForeignKey("app_user.id"))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_note: Mapped[str] = mapped_column(Text, default="", server_default="")


class EvidenceFile(Base):
    """A document supporting a rate change or an agreement change, e.g. the retailer's email saved as PDF.

    Stored in the database so it is backed up with the rates and cannot be
    separated from them. The SHA-256 is taken on upload; the database refuses
    any change or deletion afterwards (migration 0001).
    """

    __tablename__ = "evidence_file"
    __table_args__ = (CheckConstraint("size_bytes > 0", name="not_empty"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    agreement_id: Mapped[int] = mapped_column(ForeignKey("rebate_agreement.id"))
    rate_id: Mapped[int | None] = mapped_column(ForeignKey("rebate_rate.id"))
    file_name: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(100))
    size_bytes: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    content: Mapped[bytes] = mapped_column(LargeBinary)
    description: Mapped[str] = mapped_column(Text, default="", server_default="")
    uploaded_by_id: Mapped[int] = mapped_column(ForeignKey("app_user.id"))
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RebateTrueUp(Base):
    """A lumpy one-off amount, e.g. an Officeworks LTI write-back (spec §5.3). Not a rate."""

    __tablename__ = "rebate_true_up"
    __table_args__ = (
        first_of_month("period"),
        CheckConstraint("reviewed_by_id IS NULL OR reviewed_by_id <> entered_by_id", name="four_eyes"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    agreement_id: Mapped[int] = mapped_column(ForeignKey("rebate_agreement.id"))
    period: Mapped[date] = mapped_column(Date)
    amount: Mapped[Decimal] = mapped_column(Numeric(20, 2))
    description: Mapped[str] = mapped_column(Text)
    source_reference: Mapped[str] = mapped_column(Text)
    status: Mapped[ReviewStatus] = mapped_column(text_enum(ReviewStatus, "review_status"), default=ReviewStatus.PROPOSED)
    entered_by_id: Mapped[int] = mapped_column(ForeignKey("app_user.id"))
    entered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    reviewed_by_id: Mapped[int | None] = mapped_column(ForeignKey("app_user.id"))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RebateApproverScope(Base):
    """Who may approve rate changes for a brand. A brand with no rows: any rebate reviewer may."""

    __tablename__ = "rebate_approver_scope"
    __table_args__ = (UniqueConstraint("user_id", "brand_code", name="uq_rebate_approver_scope_user_brand"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("app_user.id"))
    brand_code: Mapped[str] = mapped_column(String(64))


class RebateWorkbookMonth(Base):
    """What the legacy reconciliation workbook recorded for one agreement line in one month."""

    __tablename__ = "rebate_workbook_month"
    __table_args__ = (
        first_of_month("period"),
        UniqueConstraint("agreement_id", "period", name="uq_rebate_workbook_month_agreement_period"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("load_batch.id"))
    agreement_id: Mapped[int] = mapped_column(ForeignKey("rebate_agreement.id"))
    period: Mapped[date] = mapped_column(Date)
    sales: Mapped[Decimal] = mapped_column(Numeric(20, 2))
    rebate_due: Mapped[Decimal] = mapped_column(Numeric(20, 2))


# ---------------------------------------------------------------- the feed


class ApiToken(Base):
    """A key another system uses to read the feed (read-only). Only its hash is stored."""

    __tablename__ = "api_token"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("app_user.id"))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
