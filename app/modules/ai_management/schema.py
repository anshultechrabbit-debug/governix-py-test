"""Request and response bodies of the AI management API (/organizations/{id}/ai)."""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field, field_validator, model_validator

from app.modules.ai_management.model import AIProvider, KeyKind, ModelService, NotifResource, QuotaPeriod

DEFAULT_THRESHOLDS = [70, 80, 90, 95, 100]


# 1. API key vault -------------------------------------------------------------------------------------------------

def _clean_key(value: str) -> str:
    value = value.strip()
    if len(value) < 8 or any(ch.isspace() for ch in value):
        raise ValueError("Paste the whole API key, without spaces.")
    return value


class APIKeyCreate(BaseModel):
    provider: AIProvider = AIProvider.OPENAI
    # None: from the key itself (an OpenAI "sk-admin-" key is an Admin key).
    kind: KeyKind | None = None
    api_key: str = Field(min_length=8, max_length=500)
    label: str | None = Field(None, max_length=100)
    # Admin key only: count this OpenAI project's spend and tokens, not the whole OpenAI organization's.
    openai_project_id: str | None = Field(None, max_length=64, pattern=r"^proj_[A-Za-z0-9]+$")

    @field_validator("api_key")
    @classmethod
    def _key(cls, value: str) -> str:
        return _clean_key(value)

    @model_validator(mode="after")
    def _kind(self):
        detected = KeyKind.ADMIN if self.api_key.startswith("sk-admin-") else KeyKind.API
        if self.kind is None:
            self.kind = detected
        elif self.kind != detected and self.provider == AIProvider.OPENAI:
            raise ValueError("This is an OpenAI Admin key: add it as an Admin key." if detected == KeyKind.ADMIN
                             else "An OpenAI Admin key starts with sk-admin-. This one is an ordinary API key.")
        if self.kind == KeyKind.ADMIN and self.provider != AIProvider.OPENAI:
            raise ValueError("Admin keys are an OpenAI feature.")
        if self.kind != KeyKind.ADMIN:
            self.openai_project_id = None
        return self


class APIKeyRotate(BaseModel):
    api_key: str = Field(min_length=8, max_length=500)

    @field_validator("api_key")
    @classmethod
    def _key(cls, value: str) -> str:
        return _clean_key(value)


class APIKeyUpdate(BaseModel):
    label: str | None = Field(None, max_length=100)


class APIKeyRead(BaseModel):
    id: uuid.UUID
    provider: str
    kind: str
    label: str | None
    masked_key: str
    is_active: bool
    validation_status: str | None
    validation_message: str | None
    validated_at: datetime | None
    rotated_at: datetime | None
    created_at: datetime
    # API key: the models the provider lets it use (from its last check).
    available_models: list[str] = []
    # Admin key: the OpenAI project it counts, or None for the whole OpenAI organization.
    openai_project_id: str | None = None


# 2. Models --------------------------------------------------------------------------------------------------------

class ModelAssign(BaseModel):
    provider: AIProvider
    model_id: str = Field(min_length=1, max_length=200)
    service: ModelService = ModelService.ANSWERS
    display_name: str | None = Field(None, max_length=200)
    is_default: bool = False
    input_price_per_million: float | None = Field(None, ge=0)
    output_price_per_million: float | None = Field(None, ge=0)

    @field_validator("model_id")
    @classmethod
    def _model(cls, value: str) -> str:
        value = value.strip()
        if not value or any(ch.isspace() for ch in value):
            raise ValueError("Enter the provider's model name, for example gpt-4o-mini.")
        return value


class ModelUpdate(BaseModel):
    display_name: str | None = Field(None, max_length=200)
    is_default: bool | None = None
    is_enabled: bool | None = None
    input_price_per_million: float | None = Field(None, ge=0)
    output_price_per_million: float | None = Field(None, ge=0)


class ModelRead(BaseModel):
    id: uuid.UUID
    provider: str
    model_id: str
    service: str
    display_name: str | None
    is_default: bool
    is_enabled: bool
    input_price_per_million: float | None
    output_price_per_million: float | None
    # The price used for credits: the model's own, else the platform list; None: unpriced.
    price: tuple[float, float] | None
    # The organization has an active key for this provider; otherwise the platform's key is used.
    has_own_key: bool


class ActiveModel(BaseModel):
    provider: str
    model_id: str
    source: str  # "organization" (its default) | "platform" (nothing assigned)
    uses_own_key: bool


class ModelsRead(BaseModel):
    items: list[ModelRead]
    answers: ActiveModel | None  # None: models assigned, but none enabled as default (AI is blocked)
    embeddings: ActiveModel | None


# 3-5. Limits ------------------------------------------------------------------------------------------------------

class _Period(BaseModel):
    is_enabled: bool = True
    period: QuotaPeriod = QuotaPeriod.MONTHLY
    reset_days: int | None = Field(None, ge=1, le=366)

    @model_validator(mode="after")
    def _custom(self):
        if self.period == QuotaPeriod.CUSTOM and not self.reset_days:
            raise ValueError("A custom period needs the number of days after which it resets.")
        if self.period != QuotaPeriod.CUSTOM:
            self.reset_days = None
        return self


class QuotaSet(_Period):
    question_limit: int | None = Field(None, ge=1)


class TokenSet(_Period):
    input_token_limit: int | None = Field(None, ge=1)
    output_token_limit: int | None = Field(None, ge=1)
    embedding_token_limit: int | None = Field(None, ge=1)
    total_token_limit: int | None = Field(None, ge=1)


class CreditSet(BaseModel):
    is_enabled: bool = True
    budget_usd: float | None = Field(None, ge=0)
    monthly_reset: bool = True
    rollover: bool = False


class Meter(BaseModel):
    """One limit: what is allowed, used and left in the current period. limit None: unlimited."""

    limit: float | None
    used: float
    remaining: float | None
    percent: float | None
    exhausted: bool


class Window(BaseModel):
    configured: bool
    is_enabled: bool
    period: str
    reset_days: int | None
    period_start: datetime
    resets_at: datetime | None


class QuotaRead(Window):
    questions: Meter


class TokenRead(Window):
    input: Meter
    output: Meter
    embedding: Meter
    total: Meter


class ProviderAccount(BaseModel):
    """What the provider itself reports for the current credit period (OpenAI: Admin key only).
    Never the remaining credit: OpenAI has no API for that."""

    available: bool
    message: str
    scope: str | None = None  # "project proj_..." | "whole OpenAI organization"
    since: datetime | None = None
    spent_usd: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    embedding_tokens: int | None = None
    requests: int | None = None
    checked_at: datetime | None = None


class CreditRead(Window):
    budget_usd: float | None
    monthly_reset: bool
    rollover: bool
    carried_over_usd: float
    credits: Meter  # limit = allocated (budget + carried over)
    unpriced_models: list[str]
    provider_account: ProviderAccount


# 6. Notifications -------------------------------------------------------------------------------------------------

class NotifSet(BaseModel):
    is_enabled: bool = True
    thresholds: list[int] = Field(default_factory=lambda: list(DEFAULT_THRESHOLDS), max_length=10)

    @field_validator("thresholds")
    @classmethod
    def _thresholds(cls, values: list[int]) -> list[int]:
        cleaned = sorted({int(v) for v in values})
        if any(v < 1 or v > 100 for v in cleaned):
            raise ValueError("Thresholds are percentages from 1 to 100.")
        return cleaned


class NotifRead(BaseModel):
    resource: NotifResource
    is_enabled: bool
    thresholds: list[int]
    configured: bool
    # Thresholds already sent in the current period of this resource.
    sent: list[int]


# Overview ---------------------------------------------------------------------------------------------------------

class UsageDay(BaseModel):
    date: str
    questions: int
    input_tokens: int
    output_tokens: int
    embedding_tokens: int
    cost_usd: float


class CatalogModel(BaseModel):
    provider: str
    model_id: str
    service: str
    price: tuple[float, float] | None


class Catalog(BaseModel):
    providers: list[str]
    models: list[CatalogModel]


class AIOverview(BaseModel):
    organization_id: uuid.UUID
    organization_name: str
    # Master Admin: configure; Org Admin: read only (no keys or alert settings).
    can_manage: bool
    keys: list[APIKeyRead]
    models: ModelsRead
    questions: QuotaRead
    tokens: TokenRead
    credits: CreditRead
    notifications: list[NotifRead]
    daily_usage: list[UsageDay]
    catalog: Catalog | None
