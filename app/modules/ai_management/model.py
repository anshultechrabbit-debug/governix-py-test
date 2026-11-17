"""Organization-wise AI management: one independent configuration per organization.

Six modules, all scoped to one organization and configured by the Master Admin only:
  1. OrgAPIKey          API key vault (Fernet-encrypted, shown masked)
  2. OrgAIModel         models assigned to the organization, one default per service
  3. OrgQuotaConfig     question limit shared by every user of the organization
  4. OrgTokenConfig     input / output / embedding / total token limits
  5. OrgCreditConfig    monetary budget (USD), monthly reset and optional rollover
  6. OrgAINotifConfig   alert thresholds per resource (defaults when no row exists)

OrgAIUsage is the ledger every AI call of the organization writes (reserved before the call, settled with the
provider's actual usage after it); OrgAINotifSent remembers which alerts were sent in which quota period.
"""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy import text as sql
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, TimestampMixin, UUIDPrimaryKeyMixin


class AIProvider(StrEnum):
    """The providers Governix already integrates (app/infrastructure/ai)."""

    OPENAI = "openai"
    OPENAI_COMPATIBLE = "openai_compatible"
    OPENCODE = "opencode"
    OLLAMA = "ollama"
    LOCAL = "local"


class KeyKind(StrEnum):
    API = "api"  # makes the AI calls
    ADMIN = "admin"  # OpenAI organization Admin key: reads spend and token usage only, never makes calls


class ModelService(StrEnum):
    ANSWERS = "answers"
    EMBEDDINGS = "embeddings"


class QuotaPeriod(StrEnum):
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    CUSTOM = "custom"  # every `reset_days` days from the configured start


class UsageStatus(StrEnum):
    RESERVED = "reserved"  # the call is running: its estimate counts against the limits
    SETTLED = "settled"  # the provider's actual usage
    FAILED = "failed"  # the call failed: it counts for nothing


class NotifResource(StrEnum):
    QUESTIONS = "questions"
    INPUT_TOKENS = "input_tokens"
    OUTPUT_TOKENS = "output_tokens"
    EMBEDDING_TOKENS = "embedding_tokens"
    TOTAL_TOKENS = "total_tokens"
    CREDITS = "credits"


class OrgAPIKey(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A provider API key of one organization. Only the ciphertext and a display hint are stored."""

    __tablename__ = "org_api_keys"

    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"))
    provider: Mapped[str] = mapped_column(String(40))
    kind: Mapped[str] = mapped_column(String(10), default=KeyKind.API)
    label: Mapped[str | None] = mapped_column(String(100))
    key_encrypted: Mapped[str] = mapped_column(Text)
    # What the page shows: "sk-p••••3f9a". Never enough of the key to use it.
    key_hint: Mapped[str] = mapped_column(String(40))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # "valid" | "invalid" | "unreachable" | None (not checked yet)
    validation_status: Mapped[str | None] = mapped_column(String(20))
    validation_message: Mapped[str | None] = mapped_column(String(300))
    validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # What the provider told us when the key was checked: {"models": [...]} for an API key,
    # {"project_id": "proj_..."} for an Admin key that should count one project only.
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))

    __table_args__ = (
        # One active key per organization, provider and kind (a partial unique index).
        Index("uq_org_api_keys_active_provider", "organization_id", "provider", "kind", unique=True,
              postgresql_where=sql("is_active")),
        Index("ix_org_api_keys_org", "organization_id"),
    )


class OrgAIModel(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A model the organization may use. At most one default per (organization, service)."""

    __tablename__ = "org_ai_models"

    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"))
    provider: Mapped[str] = mapped_column(String(40))
    model_id: Mapped[str] = mapped_column(String(200))
    service: Mapped[str] = mapped_column(String(20), default=ModelService.ANSWERS)
    display_name: Mapped[str | None] = mapped_column(String(200))
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # USD per 1M tokens. None: the platform's price list (ai_usage.service.PRICES, OPENAI_PRICES).
    input_price_per_million: Mapped[float | None] = mapped_column(Numeric(12, 6))
    output_price_per_million: Mapped[float | None] = mapped_column(Numeric(12, 6))

    __table_args__ = (
        UniqueConstraint("organization_id", "service", "provider", "model_id", name="uq_org_ai_models_org_model"),
        Index("uq_org_ai_models_default", "organization_id", "service", unique=True, postgresql_where=sql("is_default")),
        Index("ix_org_ai_models_org", "organization_id"),
    )


class OrgQuotaConfig(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Questions every user of the organization shares, per period."""

    __tablename__ = "org_quota_configs"

    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), unique=True)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    period: Mapped[str] = mapped_column(String(20), default=QuotaPeriod.MONTHLY)
    reset_days: Mapped[int | None] = mapped_column(Integer)  # CUSTOM only
    question_limit: Mapped[int | None] = mapped_column(BigInteger)
    # Where CUSTOM periods count from; the other periods follow the calendar (UTC).
    period_anchor: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=sql("now()"))


class OrgTokenConfig(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Token limits every user of the organization shares, per period. None: no limit of that kind."""

    __tablename__ = "org_token_configs"

    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), unique=True)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    period: Mapped[str] = mapped_column(String(20), default=QuotaPeriod.MONTHLY)
    reset_days: Mapped[int | None] = mapped_column(Integer)
    input_token_limit: Mapped[int | None] = mapped_column(BigInteger)
    output_token_limit: Mapped[int | None] = mapped_column(BigInteger)
    embedding_token_limit: Mapped[int | None] = mapped_column(BigInteger)
    total_token_limit: Mapped[int | None] = mapped_column(BigInteger)
    period_anchor: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=sql("now()"))


class OrgCreditConfig(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """The organization's internal AI budget (USD). Separate from the provider account's real balance."""

    __tablename__ = "org_credit_configs"

    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), unique=True)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    budget_usd: Mapped[float | None] = mapped_column(Numeric(14, 6))
    # True: a new budget every calendar month (UTC). False: one budget from period_anchor on.
    monthly_reset: Mapped[bool] = mapped_column(Boolean, default=True)
    # With monthly_reset: what the previous month left unspent is added to this month.
    rollover: Mapped[bool] = mapped_column(Boolean, default=False)
    carried_over_usd: Mapped[float] = mapped_column(Numeric(14, 6), default=0)
    # The month `carried_over_usd` belongs to; a new month computes its own carry once.
    carried_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    period_anchor: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=sql("now()"))


class OrgAINotifConfig(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Alert thresholds (percent used) for one resource. No row: enabled at the default thresholds."""

    __tablename__ = "org_ai_notif_configs"

    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"))
    resource: Mapped[str] = mapped_column(String(30))
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    thresholds: Mapped[list[int]] = mapped_column(JSONB, default=list)

    __table_args__ = (
        UniqueConstraint("organization_id", "resource", name="uq_org_ai_notif_configs_org_resource"),
    )


class OrgAIUsage(UUIDPrimaryKeyMixin, Base):
    """One AI call (or one question) of an organization, with its tokens and cost."""

    __tablename__ = "org_ai_usage"

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=sql("now()"))
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"))
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    # question | answers | embeddings
    service: Mapped[str] = mapped_column(String(40))
    provider: Mapped[str] = mapped_column(String(40))
    model: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(12), default=UsageStatus.SETTLED)
    input_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    output_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    embedding_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    cost_usd: Mapped[float] = mapped_column(Numeric(14, 8), default=0)
    error: Mapped[str | None] = mapped_column(String(80))
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    __table_args__ = (Index("ix_org_ai_usage_org_created", "organization_id", "created_at"),)


class OrgAINotifSent(UUIDPrimaryKeyMixin, Base):
    """An alert already sent: (organization, resource, threshold, period). Inserting it claims the alert."""

    __tablename__ = "org_ai_notif_sent"

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=sql("now()"))
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"))
    resource: Mapped[str] = mapped_column(String(30))
    threshold: Mapped[int] = mapped_column(Integer)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("organization_id", "resource", "threshold", "period_start", name="uq_org_ai_notif_sent"),
    )
