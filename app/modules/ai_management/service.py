"""Organization-wise AI management: configuration (Master Admin) and the usage every AI call is checked against.

Limits are shared by every user of an organization and measured in its current period, which is computed from
the clock (a new day, week, month or custom interval starts by itself; nothing has to run for a reset). Usage is
the org_ai_usage ledger: a call is reserved with an upper estimate before it runs (so concurrent requests can
never spend the same remaining balance) and settled with the provider's actual usage afterwards. Reservations a
crashed process never settled stop counting after RESERVATION_TTL.
"""

from __future__ import annotations

import hashlib
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.exceptions import AppError, ConflictError, NotFoundError, ValidationError
from app.infrastructure.ai.embeddings.base import EmbeddingUnavailableError
from app.infrastructure.ai.llm.base import LLMUnavailableError
from app.modules.ai_management.model import (
    AIProvider, ModelService, NotifResource, OrgAIModel, OrgAINotifConfig, OrgAINotifSent, OrgAIUsage, OrgAPIKey,
    KeyKind, OrgCreditConfig, OrgQuotaConfig, OrgTokenConfig, QuotaPeriod, UsageStatus,
)
from app.modules.ai_management.schema import (
    DEFAULT_THRESHOLDS, ActiveModel, AIOverview, APIKeyCreate, APIKeyRead, Catalog, CatalogModel, CreditRead,
    CatalogProvider, CreditSet, Meter, ModelAssign, ModelRead, ModelsRead, ModelUpdate, NotifRead, NotifSet, ProviderAccount,
    QuotaRead, QuotaSet, TokenRead, TokenSet, UsageDay,
)
from app.modules.ai_usage.service import PRICES, price_of
from app.modules.auth import mfa
from app.modules.notifications.model import NotificationType
from app.modules.notifications.service import notify

logger = logging.getLogger(__name__)

# A reservation older than this belongs to a call that never finished (a crashed worker): it stops counting.
RESERVATION_TTL = timedelta(minutes=15)
# Providers that cost nothing per token.
FREE_PROVIDERS = frozenset({AIProvider.LOCAL, AIProvider.OLLAMA})
PROVIDER_ACCOUNT_CACHE_SECONDS = 600
_account_cache: dict[str, tuple[float, ProviderAccount]] = {}
# Every provider the admin panel can offer (the local stand-in is for tests); AI_PANEL_PROVIDERS picks which it does.
ENABLED_PROVIDERS = (AIProvider.OPENAI, AIProvider.OPENAI_COMPATIBLE, AIProvider.OPENCODE, AIProvider.OLLAMA)
PROVIDER_FORMS = {
    AIProvider.OPENAI_COMPATIBLE: ("Custom API (any compatible LLM)", True, True, "https://api.groq.com/openai/v1",
                                   "Any provider with an OpenAI-compatible API: Google Gemini, Groq, Mistral, DeepSeek, "
                                   "Together, Fireworks, OpenRouter, Azure OpenAI, or your own vLLM / LM Studio server."),
    AIProvider.OPENAI: ("OpenAI", True, False, "https://api.openai.com/v1", "OpenAI's own API."),
    AIProvider.OPENCODE: ("OpenCode Zen", True, False, "https://opencode.ai/zen/v1",
                          "OpenCode's gateway to many models with one key."),
    AIProvider.OLLAMA: ("Ollama (self-hosted)", False, False, "http://localhost:11434",
                        "Models running on your own Ollama server. No key, no per-token cost."),
}
OPENAI_ADMIN_API = "https://api.openai.com/v1/organization"
# Model families worth offering from an OpenAI key's model list (not audio, image, moderation or legacy models).
_OPENAI_ANSWER_FAMILIES = ("gpt-4", "gpt-5", "o1", "o3", "o4", "chatgpt-")
_OPENAI_SKIP = ("audio", "realtime", "transcribe", "tts", "image", "search", "instruct", "codex", "computer-use")

RESOURCE_LABELS = {
    NotifResource.QUESTIONS: "question limit",
    NotifResource.INPUT_TOKENS: "input token limit",
    NotifResource.OUTPUT_TOKENS: "output token limit",
    NotifResource.EMBEDDING_TOKENS: "embedding token limit",
    NotifResource.TOTAL_TOKENS: "total token limit",
    NotifResource.CREDITS: "AI credit budget",
}

_ALERT_HEADINGS = {
    NotifResource.QUESTIONS: "AI Question Usage",
    NotifResource.INPUT_TOKENS: "AI Input Token Usage",
    NotifResource.OUTPUT_TOKENS: "AI Output Token Usage",
    NotifResource.EMBEDDING_TOKENS: "AI Embedding Token Usage",
    NotifResource.TOTAL_TOKENS: "AI Token Usage",
    NotifResource.CREDITS: "AI Budget Usage",
}


def _big(value: float) -> str:
    """5,400,000 -> "5.4 million"; 12,500 -> "12,500"."""
    if value >= 1_000_000:
        return f"{value / 1_000_000:,.1f}".rstrip("0").rstrip(".") + " million"
    return f"{value:,.0f}"


def _amounts(resource: str, meter) -> str:
    """"5.4 million of 6 million tokens", "$42.50 of $100.00", "7,450 of 10,000 questions"."""
    if resource == NotifResource.CREDITS:
        return f"${meter.used:,.2f} of ${meter.limit:,.2f}"
    unit = "questions" if resource == NotifResource.QUESTIONS else "tokens"
    return f"{_big(meter.used)} of {_big(meter.limit)} {unit}"


def _severity(threshold: int) -> str:
    """70: info, 80: warning, 90: high, 95: critical, 100: limit reached."""
    return ("limit_reached" if threshold >= 100 else "critical" if threshold >= 95 else "high" if threshold >= 90
            else "warning" if threshold >= 80 else "info")


# --- errors ---------------------------------------------------------------------------------------------------------

class AIBlocked(AppError, LLMUnavailableError):
    """The organization may not use AI now. An AppError for the API (shown as is), and an LLMUnavailableError so
    every caller that already copes with an unavailable model (summaries, naming, triage) degrades gracefully."""

    status_code = 429
    code = "AI_LIMIT_REACHED"


class AIModelNotConfigured(AIBlocked):
    status_code = 409
    code = "AI_MODEL_NOT_CONFIGURED"


class AIEmbeddingBlocked(AppError, EmbeddingUnavailableError):
    """The same for embeddings: a search then runs on its keyword lanes, an upload stops with this message."""

    status_code = 429
    code = "AI_LIMIT_REACHED"


# --- small helpers --------------------------------------------------------------------------------------------------

def _http_get(url: str, headers: dict[str, str], *, params: dict[str, Any] | None = None,
              timeout: float = 8) -> tuple[int, Any]:
    """(status, JSON body or None) of a GET. Standard library only; raises OSError when unreachable."""
    import json
    import urllib.error
    import urllib.parse
    import urllib.request

    if params:
        url = f"{url}?{urllib.parse.urlencode(params, doseq=True)}"  # lists repeat: project_ids=a&project_ids=b
    request = urllib.request.Request(url, headers={**headers, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 (fixed provider URLs)
            raw = response.read()
            status = response.status
    except urllib.error.HTTPError as exc:
        return exc.code, None
    try:
        return status, json.loads(raw or b"null")
    except ValueError:
        return status, None

def key_hint(raw: str) -> str:
    """Enough of a key to recognise it, never enough to use it: "sk-p••••3f9a"."""
    if len(raw) >= 20:
        return f"{raw[:4]}••••{raw[-4:]}"
    if len(raw) >= 12:
        return f"{raw[:2]}••••{raw[-2:]}"
    return f"••••{raw[-2:]}"


def _usable_models(provider: str, body: Any) -> list[str]:
    """The chat and embedding models in a provider's /models answer (Ollama: /api/tags), newest names first."""
    ids = [str(m.get("id")) for m in (body or {}).get("data", []) if isinstance(m, dict) and m.get("id")] \
        if isinstance(body, dict) else []
    if provider == AIProvider.OLLAMA and isinstance(body, dict):
        ids = [str(m.get("name") or m.get("model")) for m in body.get("models", []) if isinstance(m, dict)]
    if provider != AIProvider.OPENAI:
        return sorted({i.removeprefix("models/") for i in ids if i})[:200]
    keep = [i for i in ids if (i.startswith(_OPENAI_ANSWER_FAMILIES) or i.startswith("text-embedding-3"))
            and not any(word in i for word in _OPENAI_SKIP)]
    return sorted(keep, key=lambda i: (i.startswith("text-embedding"), i))[:200]


def window(period: str, reset_days: int | None, anchor: datetime | None, now: datetime) -> tuple[datetime, datetime]:
    """The current period [start, end) in UTC. Calendar periods follow the calendar; a custom one counts
    `reset_days` from its anchor."""
    today = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    if period == QuotaPeriod.DAILY:
        return today, today + timedelta(days=1)
    if period == QuotaPeriod.WEEKLY:
        start = today - timedelta(days=today.weekday())
        return start, start + timedelta(days=7)
    if period == QuotaPeriod.CUSTOM and reset_days:
        step = timedelta(days=reset_days)
        start = (anchor or today).astimezone(UTC)
        if start > now:
            start = today
        start += step * int((now - start) / step)
        return start, start + step
    start = today.replace(day=1)
    end = (start + timedelta(days=32)).replace(day=1)
    return start, end


def _meter(limit: float | None, used: float, *, money: bool = False) -> Meter:
    places = 6 if money else 0
    used = round(used, places) if money else int(used)
    if limit is None:
        return Meter(limit=None, used=used, remaining=None, percent=None, exhausted=False)
    remaining = max(0.0, limit - used)
    return Meter(
        limit=round(limit, places) if money else int(limit), used=used,
        remaining=round(remaining, places) if money else int(remaining),
        percent=round(min(100.0, used * 100.0 / limit), 1) if limit > 0 else 100.0,
        exhausted=used >= limit,
    )


@dataclass
class Usage:
    questions: int = 0
    input: int = 0
    output: int = 0
    embedding: int = 0
    cost: float = 0.0

    @property
    def total(self) -> int:
        return self.input + self.output + self.embedding


@dataclass
class Headroom:
    """What one AI call may still spend: tokens of each kind and USD (None: unlimited)."""

    input: float | None
    output: float | None
    embedding: float | None
    total: float | None
    credit: float | None


class AIManagementService:
    def __init__(self, db: Session, settings: Settings) -> None:
        self.db = db
        self.settings = settings

    # --- lookups ------------------------------------------------------------------------------------------------

    def organization(self, organization_id: uuid.UUID):
        from app.modules.organizations.model import Organization

        org = self.db.get(Organization, organization_id)
        if org is None or org.deleted_at is not None:
            raise NotFoundError("Organization not found.")
        return org

    def _quota(self, organization_id: uuid.UUID) -> OrgQuotaConfig | None:
        return self.db.scalar(select(OrgQuotaConfig).where(OrgQuotaConfig.organization_id == organization_id))

    def _tokens(self, organization_id: uuid.UUID) -> OrgTokenConfig | None:
        return self.db.scalar(select(OrgTokenConfig).where(OrgTokenConfig.organization_id == organization_id))

    def _credit(self, organization_id: uuid.UUID) -> OrgCreditConfig | None:
        return self.db.scalar(select(OrgCreditConfig).where(OrgCreditConfig.organization_id == organization_id))

    def _models(self, organization_id: uuid.UUID) -> list[OrgAIModel]:
        return list(self.db.scalars(select(OrgAIModel).where(OrgAIModel.organization_id == organization_id)
                                    .order_by(OrgAIModel.service, OrgAIModel.is_default.desc(), OrgAIModel.created_at)))

    def _keys(self, organization_id: uuid.UUID) -> list[OrgAPIKey]:
        return list(self.db.scalars(select(OrgAPIKey).where(OrgAPIKey.organization_id == organization_id)
                                    .order_by(OrgAPIKey.is_active.desc(), OrgAPIKey.created_at.desc())))

    def active_key(self, organization_id: uuid.UUID, provider: str, kind: str = KeyKind.API) -> OrgAPIKey | None:
        return self.db.scalar(select(OrgAPIKey).where(
            OrgAPIKey.organization_id == organization_id, OrgAPIKey.provider == provider, OrgAPIKey.kind == kind,
            OrgAPIKey.is_active,
        ))

    def decrypted_key(self, organization_id: uuid.UUID, provider: str) -> str | None:
        """The organization's own key for the provider, or None."""
        row = self.active_key(organization_id, provider)
        if row is None:
            return None
        raw = mfa.decrypt(self.settings, row.key_encrypted)
        if raw is None:
            raise AIModelNotConfigured(
                "This organization's API key can no longer be read (the server's encryption key changed). "
                "The Master Admin must rotate it.")
        return raw

    # --- 1. API key vault ---------------------------------------------------------------------------------------

    def key_read(self, key: OrgAPIKey) -> APIKeyRead:
        return APIKeyRead(
            id=key.id, provider=key.provider, label=key.label, masked_key=key.key_hint, is_active=key.is_active,
            validation_status=key.validation_status, validation_message=key.validation_message,
            validated_at=key.validated_at, rotated_at=key.rotated_at, created_at=key.created_at, kind=key.kind,
            available_models=list((key.details or {}).get("models") or []),
            openai_project_id=(key.details or {}).get("project_id"),
            base_url=(key.details or {}).get("base_url"),
        )

    def _key(self, organization_id: uuid.UUID, key_id: uuid.UUID) -> OrgAPIKey:
        key = self.db.get(OrgAPIKey, key_id)
        if key is None or key.organization_id != organization_id:  # never another organization's key
            raise NotFoundError("API key not found.")
        return key

    def _deactivate_others(self, key: OrgAPIKey) -> None:
        self.db.execute(update(OrgAPIKey).where(
            OrgAPIKey.organization_id == key.organization_id, OrgAPIKey.provider == key.provider,
            OrgAPIKey.kind == key.kind, OrgAPIKey.is_active, OrgAPIKey.id != key.id,
        ).values(is_active=False))

    def add_key(self, organization_id: uuid.UUID, body: APIKeyCreate, actor_id: uuid.UUID | None) -> OrgAPIKey:
        """A new active key of its kind for the provider; the previous active one (if any) is disabled and kept."""
        self.organization(organization_id)
        details = {"project_id": body.openai_project_id} if body.openai_project_id else {}
        if body.base_url:
            details["base_url"] = body.base_url
        key = OrgAPIKey(organization_id=organization_id, provider=body.provider, kind=body.kind, label=body.label,
                        key_encrypted=mfa.encrypt(self.settings, body.api_key), key_hint=key_hint(body.api_key),
                        is_active=True, created_by_id=actor_id, details=details)
        self.db.execute(update(OrgAPIKey).where(
            OrgAPIKey.organization_id == organization_id, OrgAPIKey.provider == body.provider,
            OrgAPIKey.kind == body.kind, OrgAPIKey.is_active,
        ).values(is_active=False))
        self.db.add(key)
        self.db.flush()
        self._validate(key, body.api_key)
        return key

    def rotate_key(self, organization_id: uuid.UUID, key_id: uuid.UUID, raw: str) -> OrgAPIKey:
        """Replace the secret, keeping the key's place, label and history."""
        key = self._key(organization_id, key_id)
        if key.provider == AIProvider.OPENAI and (raw.startswith("sk-admin-")) != (key.kind == KeyKind.ADMIN):
            raise ValidationError("An Admin key can only be replaced by an Admin key (sk-admin-...), and an API key "
                                  "by an API key.", code="KEY_KIND_MISMATCH")
        key.key_encrypted = mfa.encrypt(self.settings, raw)
        key.key_hint = key_hint(raw)
        key.rotated_at = datetime.now(UTC)
        self._validate(key, raw)
        return key

    def set_key_active(self, organization_id: uuid.UUID, key_id: uuid.UUID, active: bool) -> OrgAPIKey:
        key = self._key(organization_id, key_id)
        if active:
            self._deactivate_others(key)
            self.db.flush()
        key.is_active = active
        return key

    def relabel_key(self, organization_id: uuid.UUID, key_id: uuid.UUID, label: str | None,
                    base_url: str | None = None, *, set_base_url: bool = False) -> OrgAPIKey:
        """A new label, and with `set_base_url` a new API address (the key is checked again against it)."""
        key = self._key(organization_id, key_id)
        key.label = (label or "").strip() or None
        if set_base_url and base_url != (key.details or {}).get("base_url"):
            details = {k: v for k, v in (key.details or {}).items() if k not in ("base_url", "models")}
            if base_url:
                details["base_url"] = base_url
            key.details = details
            raw = mfa.decrypt(self.settings, key.key_encrypted)
            if raw is not None:
                self._validate(key, raw)
        return key

    def connection(self, organization_id: uuid.UUID, provider: str) -> tuple[str | None, str | None]:
        """(key, API address) of the organization's active connection to the provider; (None, None) without one.
        The address is None when the provider's usual one applies."""
        row = self.active_key(organization_id, provider)
        if row is None:
            return None, None
        return self.decrypted_key(organization_id, provider), (row.details or {}).get("base_url")

    def validate_key(self, organization_id: uuid.UUID, key_id: uuid.UUID) -> OrgAPIKey:
        key = self._key(organization_id, key_id)
        raw = mfa.decrypt(self.settings, key.key_encrypted)
        if raw is None:
            key.validation_status, key.validation_message = "invalid", "The stored key can no longer be decrypted; rotate it."
            key.validated_at = datetime.now(UTC)
            return key
        self._validate(key, raw)
        _account_cache.clear()  # the provider's figures are read again with the checked key
        return key

    def _base_url(self, provider: str, key: OrgAPIKey | None = None) -> str | None:
        """Where the provider's API is: the key's own address, else the platform's setting, else the usual one."""
        own = (key.details or {}).get("base_url") if key is not None else None
        if own:
            return own
        s = self.settings
        return {
            AIProvider.OPENAI: s.OPENAI_BASE_URL or "https://api.openai.com/v1",
            AIProvider.OPENCODE: s.OPENCODE_BASE_URL,
            AIProvider.OPENAI_COMPATIBLE: s.LLM_BASE_URL,
            AIProvider.OLLAMA: s.LLM_BASE_URL or "http://localhost:11434",
        }.get(provider)

    def _validate(self, key: OrgAPIKey, raw: str) -> None:
        """Ask the provider whether the key works (it lists its models). Never stores or logs the key."""
        key.validated_at = datetime.now(UTC)
        if key.provider == AIProvider.LOCAL:
            key.validation_status, key.validation_message = "valid", "The local model needs no key."
            return
        base = self._base_url(key.provider, key)
        if not base:
            key.validation_status = "unreachable"
            key.validation_message = "No API address is set for this provider: add it to the key."
            return
        if key.kind == KeyKind.ADMIN:
            # An Admin key reads the organization's costs; it cannot list models or make calls.
            url, params = f"{OPENAI_ADMIN_API}/costs", {"start_time": int(time.time()) - 86_400, "limit": 1}
        elif key.provider == AIProvider.OLLAMA:
            root = base.rstrip("/").removesuffix("/v1")
            url, params = f"{root}/api/tags", None
        else:
            url, params = f"{base.rstrip('/')}/models", None
        try:
            status, body = _http_get(url, {"Authorization": f"Bearer {raw}"}, params=params)
        except (OSError, ValueError) as exc:
            key.validation_status = "unreachable"
            key.validation_message = f"The provider could not be reached ({type(exc).__name__})."
            return
        if status == 200:
            key.validation_status = "valid"
            if key.kind == KeyKind.ADMIN:
                scope = (key.details or {}).get("project_id")
                key.validation_message = ("OpenAI accepted the Admin key: spend and token usage are read from it"
                                          + (f" for project {scope}." if scope else " for the whole OpenAI organization."))
            else:
                models = _usable_models(key.provider, body)
                key.details = {**(key.details or {}), "models": models}
                key.validation_message = (f"The provider accepted the key: {len(models)} usable models."
                                          if models else "The provider accepted the key.")
        elif status in (401, 403):
            key.validation_status, key.validation_message = "invalid", "The provider rejected the key."
        elif status == 404:
            key.validation_status = "unreachable"
            key.validation_message = "Nothing answered at this API address (HTTP 404): check the address."
        else:
            key.validation_status = "unreachable"
            key.validation_message = f"The provider answered HTTP {status}."

    # --- 2. Models ----------------------------------------------------------------------------------------------

    def price(self, model: str, provider: str, assigned: list[OrgAIModel] | None = None) -> tuple[float, float] | None:
        """USD per 1M tokens (input, output): the organization's price for the model, else the platform list."""
        if provider in FREE_PROVIDERS or model.startswith("local-"):
            return (0.0, 0.0)
        for row in assigned or []:
            if row.input_price_per_million is not None and (model == row.model_id or model.startswith(f"{row.model_id}-")):
                return float(row.input_price_per_million), float(row.output_price_per_million or 0)
        return price_of(model, self.settings.OPENAI_PRICES)

    def model_read(self, row: OrgAIModel, assigned: list[OrgAIModel], keys: set[str]) -> ModelRead:
        return ModelRead(
            id=row.id, provider=row.provider, model_id=row.model_id, service=row.service,
            display_name=row.display_name, is_default=row.is_default, is_enabled=row.is_enabled,
            input_price_per_million=float(row.input_price_per_million) if row.input_price_per_million is not None else None,
            output_price_per_million=float(row.output_price_per_million) if row.output_price_per_million is not None else None,
            price=self.price(row.model_id, row.provider, assigned), has_own_key=row.provider in keys,
        )

    def active_model(self, organization_id: uuid.UUID, service: str) -> tuple[str, str, OrgAIModel | None]:
        """(provider, model_id, assignment) the organization uses for a service.

        Nothing assigned for the service: the platform's model. Models assigned: only those, the default first;
        none of them enabled: AI is blocked for it (unassigned models are never used)."""
        rows = [m for m in self._models(organization_id) if m.service == service]
        if not rows:
            if service == ModelService.EMBEDDINGS:
                return self.settings.EMBEDDING_PROVIDER.lower(), self.settings.EMBEDDING_MODEL, None
            return self.settings.LLM_PROVIDER.lower(), self.settings.LLM_MODEL, None
        enabled = [m for m in rows if m.is_enabled]
        if not enabled:
            raise AIModelNotConfigured(
                "No AI model is enabled for your organization. Ask the platform administrator to enable one.")
        chosen = next((m for m in enabled if m.is_default), enabled[0])
        return chosen.provider, chosen.model_id, chosen

    def models_read(self, organization_id: uuid.UUID) -> ModelsRead:
        assigned = self._models(organization_id)
        keys = {k.provider for k in self._keys(organization_id) if k.is_active and k.kind == KeyKind.API}
        active: dict[str, ActiveModel | None] = {}
        for service in (ModelService.ANSWERS, ModelService.EMBEDDINGS):
            try:
                provider, model_id, row = self.active_model(organization_id, service)
                if row is None and service == ModelService.ANSWERS and self.settings.AI_REQUIRE_ORG_CONFIG:
                    active[service] = None  # the platform's model is never used for an organization
                    continue
                active[service] = ActiveModel(provider=provider, model_id=model_id,
                                              source="organization" if row else "platform",
                                              uses_own_key=provider in keys)
            except AIModelNotConfigured:
                active[service] = None
        return ModelsRead(items=[self.model_read(m, assigned, keys) for m in assigned],
                          answers=active[ModelService.ANSWERS], embeddings=active[ModelService.EMBEDDINGS])

    def _model(self, organization_id: uuid.UUID, model_id: uuid.UUID) -> OrgAIModel:
        row = self.db.get(OrgAIModel, model_id)
        if row is None or row.organization_id != organization_id:
            raise NotFoundError("Model not found.")
        return row

    def _make_default(self, row: OrgAIModel) -> None:
        self.db.execute(update(OrgAIModel).where(
            OrgAIModel.organization_id == row.organization_id, OrgAIModel.service == row.service,
            OrgAIModel.is_default, OrgAIModel.id != row.id,
        ).values(is_default=False))
        self.db.flush()
        row.is_default = True
        row.is_enabled = True

    def assign_model(self, organization_id: uuid.UUID, body: ModelAssign) -> OrgAIModel:
        self.organization(organization_id)
        exists = self.db.scalar(select(OrgAIModel.id).where(
            OrgAIModel.organization_id == organization_id, OrgAIModel.service == body.service,
            OrgAIModel.provider == body.provider, OrgAIModel.model_id == body.model_id,
        ))
        if exists:
            raise ConflictError("This model is already assigned to the organization.", code="MODEL_ALREADY_ASSIGNED")
        first = not self.db.scalar(select(OrgAIModel.id).where(
            OrgAIModel.organization_id == organization_id, OrgAIModel.service == body.service).limit(1))
        row = OrgAIModel(organization_id=organization_id, provider=body.provider, model_id=body.model_id,
                         service=body.service, display_name=body.display_name, is_default=False, is_enabled=True,
                         input_price_per_million=body.input_price_per_million,
                         output_price_per_million=body.output_price_per_million)
        self.db.add(row)
        self.db.flush()
        if body.is_default or first:  # the first model of a service is its default
            self._make_default(row)
        return row

    def update_model(self, organization_id: uuid.UUID, model_id: uuid.UUID, body: ModelUpdate) -> OrgAIModel:
        row = self._model(organization_id, model_id)
        fields = body.model_fields_set
        if "display_name" in fields:
            row.display_name = (body.display_name or "").strip() or None
        if "input_price_per_million" in fields:
            row.input_price_per_million = body.input_price_per_million
        if "output_price_per_million" in fields:
            row.output_price_per_million = body.output_price_per_million
        if body.is_default:
            self._make_default(row)
        elif body.is_default is False:
            row.is_default = False
        if body.is_enabled is not None:
            row.is_enabled = body.is_enabled
            if not body.is_enabled and row.is_default:
                # A disabled model cannot be the default: the next enabled one of the service takes over.
                row.is_default = False
                self.db.flush()
                successor = self.db.scalar(select(OrgAIModel).where(
                    OrgAIModel.organization_id == organization_id, OrgAIModel.service == row.service,
                    OrgAIModel.is_enabled, OrgAIModel.id != row.id).order_by(OrgAIModel.created_at))
                if successor is not None:
                    self._make_default(successor)
        return row

    def remove_model(self, organization_id: uuid.UUID, model_id: uuid.UUID) -> None:
        row = self._model(organization_id, model_id)
        was_default, service = row.is_default, row.service
        self.db.delete(row)
        self.db.flush()
        if was_default:
            successor = self.db.scalar(select(OrgAIModel).where(
                OrgAIModel.organization_id == organization_id, OrgAIModel.service == service, OrgAIModel.is_enabled,
            ).order_by(OrgAIModel.created_at))
            if successor is not None:
                self._make_default(successor)

    # --- usage ledger -------------------------------------------------------------------------------------------

    def usage_since(self, organization_id: uuid.UUID, since: datetime, until: datetime | None = None) -> Usage:
        """What the organization used from `since`: settled calls plus calls still running."""
        live = or_(OrgAIUsage.status == UsageStatus.SETTLED, and_(
            OrgAIUsage.status == UsageStatus.RESERVED, OrgAIUsage.created_at > datetime.now(UTC) - RESERVATION_TTL))
        conditions = [OrgAIUsage.organization_id == organization_id, OrgAIUsage.created_at >= since, live]
        if until is not None:
            conditions.append(OrgAIUsage.created_at < until)
        row = self.db.execute(select(
            func.count().filter(OrgAIUsage.service == "question"),
            func.coalesce(func.sum(OrgAIUsage.input_tokens), 0),
            func.coalesce(func.sum(OrgAIUsage.output_tokens), 0),
            func.coalesce(func.sum(OrgAIUsage.embedding_tokens), 0),
            func.coalesce(func.sum(OrgAIUsage.cost_usd), 0),
        ).where(*conditions)).one()
        return Usage(int(row[0]), int(row[1]), int(row[2]), int(row[3]), float(row[4]))

    def lock(self, organization_id: uuid.UUID) -> None:
        """Serialize the organization's reservations until this transaction ends (a few milliseconds)."""
        self.db.execute(select(func.pg_advisory_xact_lock(func.hashtext(f"governix-ai:{organization_id}"))))

    # --- 3-5. Limits --------------------------------------------------------------------------------------------

    def _now(self) -> datetime:
        return datetime.now(UTC)

    def quota_read(self, organization_id: uuid.UUID) -> QuotaRead:
        cfg, now = self._quota(organization_id), self._now()
        period = cfg.period if cfg else QuotaPeriod.MONTHLY
        start, end = window(period, cfg.reset_days if cfg else None, cfg.period_anchor if cfg else None, now)
        used = self.usage_since(organization_id, start).questions
        enabled = bool(cfg and cfg.is_enabled)
        limit = cfg.question_limit if enabled and cfg else None
        return QuotaRead(configured=cfg is not None, is_enabled=enabled, period=period,
                         reset_days=cfg.reset_days if cfg else None, period_start=start, resets_at=end,
                         questions=_meter(limit, used))

    def set_quota(self, organization_id: uuid.UUID, body: QuotaSet) -> OrgQuotaConfig:
        self.organization(organization_id)
        cfg = self._quota(organization_id) or OrgQuotaConfig(organization_id=organization_id)
        if cfg.period != body.period or cfg.reset_days != body.reset_days or cfg.id is None:
            cfg.period_anchor = self._now()  # a new custom period counts from now
        cfg.is_enabled, cfg.period, cfg.reset_days, cfg.question_limit = (
            body.is_enabled, body.period, body.reset_days, body.question_limit)
        self.db.add(cfg)
        return cfg

    def token_read(self, organization_id: uuid.UUID) -> TokenRead:
        cfg, now = self._tokens(organization_id), self._now()
        period = cfg.period if cfg else QuotaPeriod.MONTHLY
        start, end = window(period, cfg.reset_days if cfg else None, cfg.period_anchor if cfg else None, now)
        used = self.usage_since(organization_id, start)
        enabled = bool(cfg and cfg.is_enabled)

        def limit(name: str) -> int | None:
            return getattr(cfg, name) if enabled and cfg else None

        return TokenRead(
            configured=cfg is not None, is_enabled=enabled, period=period, reset_days=cfg.reset_days if cfg else None,
            period_start=start, resets_at=end,
            input=_meter(limit("input_token_limit"), used.input),
            output=_meter(limit("output_token_limit"), used.output),
            embedding=_meter(limit("embedding_token_limit"), used.embedding),
            total=_meter(limit("total_token_limit"), used.total),
        )

    def set_tokens(self, organization_id: uuid.UUID, body: TokenSet) -> OrgTokenConfig:
        self.organization(organization_id)
        cfg = self._tokens(organization_id) or OrgTokenConfig(organization_id=organization_id)
        if cfg.period != body.period or cfg.reset_days != body.reset_days or cfg.id is None:
            cfg.period_anchor = self._now()
        cfg.is_enabled, cfg.period, cfg.reset_days = body.is_enabled, body.period, body.reset_days
        cfg.input_token_limit, cfg.output_token_limit = body.input_token_limit, body.output_token_limit
        cfg.embedding_token_limit, cfg.total_token_limit = body.embedding_token_limit, body.total_token_limit
        self.db.add(cfg)
        return cfg

    def _credit_window(self, cfg: OrgCreditConfig | None, now: datetime) -> tuple[datetime, datetime | None]:
        if cfg is None or cfg.monthly_reset:
            return window(QuotaPeriod.MONTHLY, None, None, now)
        return cfg.period_anchor, None

    def _carry(self, organization_id: uuid.UUID, cfg: OrgCreditConfig, start: datetime, *, save: bool) -> float:
        """Unspent budget of the previous month added to this one (rollover). Computed once per month and kept,
        so it stays the same all month whatever happens to old usage rows."""
        if not (cfg.monthly_reset and cfg.rollover and cfg.budget_usd is not None):
            return 0.0
        if cfg.carried_for is not None and cfg.carried_for == start:
            return float(cfg.carried_over_usd or 0)
        previous_start = (start - timedelta(days=1)).replace(day=1)
        if cfg.created_at and cfg.created_at.astimezone(UTC) >= start:
            carry = 0.0  # the budget did not exist last month
        else:
            previous_allocated = float(cfg.budget_usd) + (
                float(cfg.carried_over_usd or 0) if cfg.carried_for == previous_start else 0.0)
            spent = self.usage_since(organization_id, previous_start, start).cost
            carry = max(0.0, round(previous_allocated - spent, 6))
        if save:
            cfg.carried_over_usd, cfg.carried_for = carry, start
        return carry

    def credit_read(self, organization_id: uuid.UUID, *, with_balance: bool = True) -> CreditRead:
        cfg, now = self._credit(organization_id), self._now()
        start, end = self._credit_window(cfg, now)
        used = self.usage_since(organization_id, start)
        enabled = bool(cfg and cfg.is_enabled and cfg.budget_usd is not None)
        carry = self._carry(organization_id, cfg, start, save=False) if cfg else 0.0
        allocated = float(cfg.budget_usd) + carry if enabled and cfg else None
        assigned = self._models(organization_id)
        unpriced = sorted({m.model_id for m in assigned if m.is_enabled and self.price(m.model_id, m.provider, assigned) is None})
        return CreditRead(
            configured=cfg is not None, is_enabled=enabled, period="monthly" if not cfg or cfg.monthly_reset else "total",
            reset_days=None, period_start=start, resets_at=end,
            budget_usd=float(cfg.budget_usd) if cfg and cfg.budget_usd is not None else None,
            monthly_reset=cfg.monthly_reset if cfg else True, rollover=cfg.rollover if cfg else False,
            carried_over_usd=carry, credits=_meter(allocated, used.cost, money=True), unpriced_models=unpriced,
            provider_account=self.provider_account(organization_id, start) if with_balance else ProviderAccount(
                available=False, message=""),
        )

    def set_credits(self, organization_id: uuid.UUID, body: CreditSet) -> OrgCreditConfig:
        self.organization(organization_id)
        cfg = self._credit(organization_id) or OrgCreditConfig(organization_id=organization_id, carried_over_usd=0)
        if cfg.id is None or cfg.monthly_reset != body.monthly_reset:
            cfg.period_anchor = self._now()
        if not body.rollover:
            cfg.carried_over_usd, cfg.carried_for = 0, None
        cfg.is_enabled, cfg.budget_usd = body.is_enabled, body.budget_usd
        cfg.monthly_reset, cfg.rollover = body.monthly_reset, body.rollover and body.monthly_reset
        self.db.add(cfg)
        return cfg

    def provider_account(self, organization_id: uuid.UUID, since: datetime, *, refresh: bool = False) -> ProviderAccount:
        """What OpenAI itself reports since `since`: spend (Costs API) and tokens (Usage API), read with the
        organization's Admin key, for its OpenAI project if one is set. OpenAI has no API for the remaining credit,
        so that is never shown; the budget Governix enforces is its own. Cached for 10 minutes."""
        row = self.active_key(organization_id, AIProvider.OPENAI, KeyKind.ADMIN)
        raw = mfa.decrypt(self.settings, row.key_encrypted) if row else None
        if not raw:
            if self.active_key(organization_id, AIProvider.OPENAI) is None:
                return ProviderAccount(available=False, message=(
                    "AI providers don't report the remaining credit of an account, so the budget enforced here is "
                    "the one you set. Spend is computed from the tokens each call used and the model's price."))
            return ProviderAccount(available=False, message=(
                "The provider doesn't show spend to ordinary API keys. Add a usage reader key (an OpenAI Admin key, "
                "sk-admin-...) to see what it reports as spent and used. No provider reports the remaining credit, "
                "so the budget is set by you."))
        project = (row.details or {}).get("project_id")
        cache_key = hashlib.sha256(f"{raw}:{project}:{since.isoformat()}".encode()).hexdigest()
        if not refresh and (cached := _account_cache.get(cache_key)) and cached[0] > time.monotonic():
            return cached[1]
        headers = {"Authorization": f"Bearer {raw}"}
        base = {"start_time": int(since.timestamp()), "bucket_width": "1d", **({"project_ids": [project]} if project else {})}

        def pages(path: str, limit: int):
            page = None
            for _ in range(24):
                status, body = _http_get(f"{OPENAI_ADMIN_API}/{path}", headers,
                                         params={**base, "limit": limit, **({"page": page} if page else {})})
                if status != 200 or not isinstance(body, dict):
                    raise ValueError(f"{path}: HTTP {status}")
                yield from (r for bucket in body.get("data", []) for r in bucket.get("results", []))
                if not body.get("has_more"):
                    return
                page = body.get("next_page")

        try:
            spent = sum(float((r.get("amount") or {}).get("value") or 0) for r in pages("costs", 180))
            completions = list(pages("usage/completions", 31))
            embeddings = list(pages("usage/embeddings", 31))
        except Exception as exc:  # never fail the page over the provider's figures
            logger.warning("OpenAI usage unavailable for organization %s: %s", organization_id, exc)
            return ProviderAccount(available=False, message=(
                "OpenAI didn't return the usage just now (the Admin key may lack the usage permission). "
                "Check the key, or try again later."))
        result = ProviderAccount(
            available=True, since=since, checked_at=datetime.now(UTC),
            scope=f"project {project}" if project else "whole OpenAI organization",
            message=("What OpenAI reports for " + (f"project {project}" if project else "the whole OpenAI organization")
                     + " since this period started. OpenAI does not report the remaining credit."),
            spent_usd=round(spent, 4),
            input_tokens=sum(int(r.get("input_tokens") or 0) for r in completions),
            output_tokens=sum(int(r.get("output_tokens") or 0) for r in completions),
            embedding_tokens=sum(int(r.get("input_tokens") or 0) for r in embeddings),
            requests=sum(int(r.get("num_model_requests") or 0) for r in completions + embeddings),
        )
        _account_cache[cache_key] = (time.monotonic() + PROVIDER_ACCOUNT_CACHE_SECONDS, result)
        return result

    # --- enforcement --------------------------------------------------------------------------------------------

    def headroom(self, organization_id: uuid.UUID, *, save: bool = False) -> Headroom:
        """What the organization may still spend in each limit's current period (None: unlimited)."""
        now = self._now()
        tokens = self._tokens(organization_id)
        caps: dict[str, float | None] = {"input": None, "output": None, "embedding": None, "total": None}
        if tokens and tokens.is_enabled:
            start, _ = window(tokens.period, tokens.reset_days, tokens.period_anchor, now)
            used = self.usage_since(organization_id, start)
            for name, limit, spent in (("input", tokens.input_token_limit, used.input),
                                       ("output", tokens.output_token_limit, used.output),
                                       ("embedding", tokens.embedding_token_limit, used.embedding),
                                       ("total", tokens.total_token_limit, used.total)):
                if limit is not None:
                    caps[name] = limit - spent
        credit = None
        cfg = self._credit(organization_id)
        if cfg and cfg.is_enabled and cfg.budget_usd is not None:
            start, _ = self._credit_window(cfg, now)
            credit = float(cfg.budget_usd) + self._carry(organization_id, cfg, start, save=save) \
                - self.usage_since(organization_id, start).cost
        return Headroom(caps["input"], caps["output"], caps["embedding"], caps["total"], credit)

    def check_question(self, organization_id: uuid.UUID) -> None:
        """Refuse a new question when any shared limit is used up. Called under the organization's lock."""
        cfg = self._quota(organization_id)
        if cfg and cfg.is_enabled and cfg.question_limit is not None:
            start, end = window(cfg.period, cfg.reset_days, cfg.period_anchor, self._now())
            if self.usage_since(organization_id, start).questions >= cfg.question_limit:
                raise AIBlocked(
                    f"Your organization has used all {cfg.question_limit} AI questions for this period. "
                    f"Questions are available again from {end:%d %b %Y %H:%M} UTC.", code="QUESTION_LIMIT_REACHED")
        room = self.headroom(organization_id, save=True)
        if any(v is not None and v <= 0 for v in (room.input, room.output, room.total)):
            raise AIBlocked("Your organization has used all of its AI tokens for this period.",
                            code="TOKEN_LIMIT_REACHED")
        if room.credit is not None and room.credit <= 0:
            raise AIBlocked("Your organization has used all of its AI credit for this period.",
                            code="CREDIT_LIMIT_REACHED")
        self.active_model(organization_id, ModelService.ANSWERS)  # a model the organization may use

    def _check_switched_on(self, organization_id: uuid.UUID, *, embeddings: bool = False) -> None:
        """A suspension (or deactivation) stops even the calls already past the request's own check."""
        if not self.settings.AI_REQUIRE_ORG_CONFIG:
            return
        from app.modules.ai_management.model import OrgAISettings
        from app.modules.ai_management.status import USER_MESSAGE

        row = self.db.scalar(select(OrgAISettings).where(OrgAISettings.organization_id == organization_id))
        if row is not None and row.is_active:
            return
        suspended = row is not None and row.suspended_at is not None
        exc = (AIEmbeddingBlocked if embeddings else AIBlocked)(
            USER_MESSAGE, code="AI_SUSPENDED" if suspended else "AI_CONFIGURATION_INCOMPLETE")
        exc.status_code = 403 if suspended else 409
        raise exc

    def _origin(self, user_id: uuid.UUID | None) -> dict[str, Any]:
        """The branch of the user making the call and the request it belongs to, for the usage row."""
        from app.core.request_context import get_request_id
        from app.modules.users.model import User

        branch_id = self.db.scalar(select(User.branch_id).where(User.id == user_id)) if user_id else None
        return {"branch_id": branch_id, "request_id": (get_request_id() or None)}

    def reserve(self, organization_id: uuid.UUID, *, service: str, provider: str, model: str,
                input_tokens: int, output_tokens: int, embedding_tokens: int,
                user_id: uuid.UUID | None, meta: dict | None = None, feature: str | None = None) -> OrgAIUsage:
        """Charge the estimate of one call, under the organization's lock, or refuse it."""
        self._check_switched_on(organization_id, embeddings=service == "embeddings")
        room = self.headroom(organization_id, save=True)
        total = input_tokens + output_tokens + embedding_tokens
        for name, need, cap in (("input", input_tokens, room.input), ("output", output_tokens, room.output),
                                ("embedding", embedding_tokens, room.embedding), ("total", total, room.total)):
            if need and cap is not None and need > cap:
                blocked = AIEmbeddingBlocked if service == "embeddings" else AIBlocked
                raise blocked(f"Your organization has reached its {name} token limit for this period.",
                              code="TOKEN_LIMIT_REACHED")
        error = AIEmbeddingBlocked if service == "embeddings" else AIBlocked
        cost = 0.0
        price = self.price(model, provider, self._models(organization_id))
        if price is None and room.credit is not None:
            raise error(f"The model {model} has no price set, so the organization's AI budget cannot be applied. "
                        "The platform administrator must set its price.", code="AI_PRICE_UNAVAILABLE")
        if price is not None:
            cost = ((input_tokens + embedding_tokens) * price[0] + output_tokens * price[1]) / 1_000_000
        if room.credit is not None and cost > room.credit:
            raise error("Your organization has used all of its AI credit for this period.",
                        code="CREDIT_LIMIT_REACHED")
        row = OrgAIUsage(organization_id=organization_id, user_id=user_id, service=service, provider=provider,
                         model=model, status=UsageStatus.RESERVED, input_tokens=input_tokens,
                         output_tokens=output_tokens, embedding_tokens=embedding_tokens, cost_usd=round(cost, 8),
                         meta=meta or {}, feature=feature, estimated=False, **self._origin(user_id))
        self.db.add(row)
        self.db.flush()
        return row

    def _price_meta(self, model: str, provider: str, organization_id: uuid.UUID) -> tuple[tuple[float, float], dict]:
        """The price a cost is computed with, and where it came from (kept on the row: later price changes never
        rewrite history)."""
        assigned = self._models(organization_id)
        own = any(r.input_price_per_million is not None and (model == r.model_id or model.startswith(f"{r.model_id}-"))
                  for r in assigned)
        price = self.price(model, provider, assigned)
        source = "free" if price == (0.0, 0.0) else "organization" if own else "platform" if price else "unpriced"
        price = price or (0.0, 0.0)
        return price, {"price": [price[0], price[1]], "price_source": source}

    def settle(self, usage_id: uuid.UUID, *, model: str, input_tokens: int = 0, output_tokens: int = 0,
               embedding_tokens: int = 0, error: str | None = None, estimated: bool = False) -> None:
        """The provider's actual usage replaces the estimate (zero when the call failed before the provider
        charged anything). `estimated`: the provider reported no usage, so these figures are Governix's own."""
        row = self.db.get(OrgAIUsage, usage_id)
        if row is None:
            return
        if error is not None:
            row.status, row.error = UsageStatus.FAILED, error[:80]
            row.input_tokens = row.output_tokens = row.embedding_tokens = 0
            row.cost_usd = 0
            return
        price, meta = self._price_meta(model, row.provider, row.organization_id)
        row.status, row.model, row.estimated = UsageStatus.SETTLED, model[:200], estimated
        row.input_tokens, row.output_tokens, row.embedding_tokens = input_tokens, output_tokens, embedding_tokens
        row.cost_usd = round(((input_tokens + embedding_tokens) * price[0] + output_tokens * price[1]) / 1_000_000, 8)
        row.meta = {**(row.meta or {}), **meta}

    def settle_estimate(self, usage_id: uuid.UUID, error: str | None = None) -> None:
        """The actual usage is unknown but the provider was (or may have been) paid: the reader left mid-stream,
        or the call failed after the provider had started answering. The reserved estimate is what counts,
        labelled as an estimate."""
        row = self.db.get(OrgAIUsage, usage_id)
        if row is not None and row.status == UsageStatus.RESERVED:
            _, meta = self._price_meta(row.model, row.provider, row.organization_id)
            row.status, row.estimated = UsageStatus.SETTLED, True
            row.meta = {**(row.meta or {}), **meta, "estimated": True}
            if error:
                row.error = error[:80]

    def record_question(self, organization_id: uuid.UUID, user_id: uuid.UUID | None) -> None:
        self.db.add(OrgAIUsage(organization_id=organization_id, user_id=user_id, service="question",
                               provider="governix", model="question", status=UsageStatus.SETTLED,
                               input_tokens=0, output_tokens=0, embedding_tokens=0, cost_usd=0, meta={},
                               feature="qa", estimated=False, **self._origin(user_id)))

    # --- 6. Notifications ---------------------------------------------------------------------------------------

    def _notif_configs(self, organization_id: uuid.UUID) -> dict[str, OrgAINotifConfig]:
        return {c.resource: c for c in self.db.scalars(
            select(OrgAINotifConfig).where(OrgAINotifConfig.organization_id == organization_id))}

    def set_notifications(self, organization_id: uuid.UUID, resource: NotifResource, body: NotifSet) -> OrgAINotifConfig:
        self.organization(organization_id)
        cfg = self._notif_configs(organization_id).get(resource) or OrgAINotifConfig(
            organization_id=organization_id, resource=resource)
        cfg.is_enabled, cfg.thresholds = body.is_enabled, body.thresholds
        self.db.add(cfg)
        return cfg

    def _levels(self, organization_id: uuid.UUID) -> dict[str, tuple[float, datetime, datetime | None]]:
        """Percent used and period of every resource with a limit."""
        return {r: (m.percent, start, end) for r, (m, start, end) in self._meters(organization_id).items()}

    def _meters(self, organization_id: uuid.UUID) -> dict[str, tuple[Meter, datetime, datetime | None]]:
        """The meter and period of every resource with a limit."""
        out: dict[str, tuple[Meter, datetime, datetime | None]] = {}
        quota = self.quota_read(organization_id)
        if quota.questions.percent is not None:
            out[NotifResource.QUESTIONS] = (quota.questions, quota.period_start, quota.resets_at)
        tokens = self.token_read(organization_id)
        for resource, meter in ((NotifResource.INPUT_TOKENS, tokens.input), (NotifResource.OUTPUT_TOKENS, tokens.output),
                                (NotifResource.EMBEDDING_TOKENS, tokens.embedding),
                                (NotifResource.TOTAL_TOKENS, tokens.total)):
            if meter.percent is not None:
                out[resource] = (meter, tokens.period_start, tokens.resets_at)
        credits = self.credit_read(organization_id, with_balance=False)
        if credits.credits.percent is not None:
            out[NotifResource.CREDITS] = (credits.credits, credits.period_start, credits.resets_at)
        return out

    def rearm_alerts(self, organization_id: uuid.UUID) -> int:
        """After a limit changed: forget the alerts of this period above what is now used, so crossing them
        again (against the new limit) alerts again. Returns how many were re-armed."""
        removed = 0
        meters = self._meters(organization_id)
        for resource in NotifResource:
            meter, start, _ = meters.get(resource, (None, None, None))
            conditions = [OrgAINotifSent.organization_id == organization_id, OrgAINotifSent.resource == resource]
            if meter is not None:
                conditions += [OrgAINotifSent.period_start == start, OrgAINotifSent.threshold > (meter.percent or 0)]
            removed += self.db.query(OrgAINotifSent).filter(*conditions).delete(synchronize_session=False)
        return removed

    def notifications_read(self, organization_id: uuid.UUID) -> list[NotifRead]:
        configs = self._notif_configs(organization_id)
        levels = self._levels(organization_id)
        sent = self.db.execute(select(OrgAINotifSent.resource, OrgAINotifSent.threshold, OrgAINotifSent.period_start)
                               .where(OrgAINotifSent.organization_id == organization_id)).all()
        result = []
        for resource in NotifResource:
            cfg = configs.get(resource)
            start = levels.get(resource, (0, None, None))[1]
            result.append(NotifRead(
                resource=resource, is_enabled=cfg.is_enabled if cfg else True,
                thresholds=list(cfg.thresholds) if cfg else list(DEFAULT_THRESHOLDS), configured=cfg is not None,
                sent=sorted(t for r, t, p in sent if r == resource and start is not None and p == start),
            ))
        return result

    def evaluate_alerts(self, organization_id: uuid.UUID) -> int:
        """Alert the Master Admins and the organization's Org Admins once per threshold and period.

        Claiming the (resource, threshold, period) row is the dedupe: two requests crossing the same threshold
        at once insert it once, and only that one notifies. A jump past several thresholds sends one alert, for
        the highest."""
        from app.modules.auth.permissions import Role
        from app.modules.users.model import User

        if not self._has_limits(organization_id):
            return 0  # nothing to measure against: most organizations, on every AI call
        levels = self._levels(organization_id)
        if not levels:
            return 0
        configs = self._notif_configs(organization_id)
        org = self.organization(organization_id)
        sent = 0
        for resource, (percent, start, resets_at) in levels.items():
            cfg = configs.get(resource)
            if cfg is not None and not cfg.is_enabled:
                continue
            crossed = [t for t in (cfg.thresholds if cfg else DEFAULT_THRESHOLDS) if percent >= t]
            if not crossed:
                continue
            claimed = set(self.db.scalars(insert(OrgAINotifSent).values([
                {"id": uuid.uuid4(), "organization_id": organization_id, "resource": resource, "threshold": t,
                 "period_start": start} for t in crossed
            ]).on_conflict_do_nothing(constraint="uq_org_ai_notif_sent").returning(OrgAINotifSent.threshold)))
            top = max(crossed)
            if top not in claimed:
                continue
            label = RESOURCE_LABELS[resource]
            meter = self._meters(organization_id)[resource][0]
            amounts = _amounts(resource, meter)
            heading = _ALERT_HEADINGS[resource]
            until = f" until {resets_at:%d %b %Y} (UTC)" if resets_at else ""
            if top >= 100:
                title = f"{heading} — limit reached"
                master_body = (f"{org.name} has used all of its {label} ({amounts}). New AI requests of this kind are "
                               f"blocked{until} or until the limit is raised.")
                admin_body = (f"Your organization has used all of its {label} ({amounts}). New AI requests of this "
                              f"kind are blocked{until}. Please contact the Super Admin to review or increase the limit.")
            else:
                title = f"{heading} — {top}% reached"
                master_body = f"{org.name} has used {top}% of its {label}: {amounts}."
                admin_body = (f"Your organization has consumed {amounts} for this period. "
                              "Please contact the Super Admin to review or increase the limit.")
            data = {"organization_id": str(organization_id), "resource": str(resource), "threshold": top,
                    "percent": percent, "used": meter.used, "limit": meter.limit,
                    "severity": _severity(top)}
            # The limit is part of the event: after a raise, crossing a threshold again is a new alert.
            key = f"ai:{organization_id}:{resource}:{top}:{start.isoformat()}:{meter.limit}"
            masters = self.db.scalars(select(User.id).where(User.role == Role.MASTER_ADMIN, User.is_active,
                                                             User.deleted_at.is_(None)))
            admins = self.db.scalars(select(User.id).where(User.role == Role.ORG_ADMIN, User.is_active,
                                                            User.organization_id == organization_id,
                                                            User.deleted_at.is_(None)))
            notify(self.db, masters, NotificationType.AI_THRESHOLD, f"{org.name}: {title}", organization_id=organization_id,
                   body=master_body, link=f"/masteradmin/organization/{organization_id}/ai", data=data, dedupe_key=key)
            notify(self.db, admins, NotificationType.AI_THRESHOLD, title, organization_id=organization_id,
                   body=admin_body, link="/organizationadmin/ai-usage", data=data, dedupe_key=key)
            sent += 1
        return sent

    def _has_limits(self, organization_id: uuid.UUID) -> bool:
        return any(self.db.scalar(select(model.id).where(model.organization_id == organization_id, model.is_enabled))
                   for model in (OrgQuotaConfig, OrgTokenConfig, OrgCreditConfig))

    def sweep_alerts(self) -> int:
        """Backstop for the platform sweep: every organization with an AI limit."""
        ids = set()
        for model in (OrgQuotaConfig, OrgTokenConfig, OrgCreditConfig):
            ids |= set(self.db.scalars(select(model.organization_id).where(model.is_enabled)))
        total = 0
        for organization_id in ids:
            try:
                total += self.evaluate_alerts(organization_id)
            except NotFoundError:
                continue
        return total

    # --- overview -----------------------------------------------------------------------------------------------

    def daily_usage(self, organization_id: uuid.UUID, days: int = 14) -> list[UsageDay]:
        today = self._now().replace(hour=0, minute=0, second=0, microsecond=0)
        since = today - timedelta(days=days - 1)
        day = func.date_trunc("day", func.timezone("UTC", OrgAIUsage.created_at)).label("day")
        rows = self.db.execute(select(
            day,
            func.count().filter(OrgAIUsage.service == "question"),
            func.coalesce(func.sum(OrgAIUsage.input_tokens), 0), func.coalesce(func.sum(OrgAIUsage.output_tokens), 0),
            func.coalesce(func.sum(OrgAIUsage.embedding_tokens), 0), func.coalesce(func.sum(OrgAIUsage.cost_usd), 0),
        ).where(OrgAIUsage.organization_id == organization_id, OrgAIUsage.created_at >= since,
                OrgAIUsage.status == UsageStatus.SETTLED).group_by(day)).all()
        by_day = {r[0].date(): r for r in rows}
        result = []
        for offset in range(days - 1, -1, -1):
            date = (today - timedelta(days=offset)).date()
            r = by_day.get(date)
            result.append(UsageDay(date=date.isoformat(), questions=int(r[1]) if r else 0,
                                   input_tokens=int(r[2]) if r else 0, output_tokens=int(r[3]) if r else 0,
                                   embedding_tokens=int(r[4]) if r else 0,
                                   cost_usd=round(float(r[5]), 6) if r else 0.0))
        return result

    def catalog(self, organization_id: uuid.UUID) -> Catalog:
        """What the assign-model form offers: for every provider the organization has connected, the models its
        key can use (from its last check); OpenAI's price list is added only for an OpenAI connection."""
        models: list[CatalogModel] = []
        seen: set[tuple[str, str]] = set()
        for key in self._keys(organization_id):
            if not key.is_active or key.kind != KeyKind.API:
                continue
            names = list((key.details or {}).get("models") or [])
            if key.provider == AIProvider.OPENAI:
                names += [n for n in PRICES if n not in names and n != "text-embedding-ada-002"]  # ada-002: fixed size
            for name in names:
                if (key.provider, name) in seen:
                    continue
                seen.add((key.provider, name))
                models.append(CatalogModel(provider=key.provider, model_id=name,
                                           service="embeddings" if "embed" in name.lower() else "answers",
                                           price=self.price(name, key.provider)))
        offered = [p.strip().lower() for p in self.settings.AI_PANEL_PROVIDERS]
        options = [CatalogProvider(id=p.value, label=label, needs_key=needs_key, needs_base_url=needs_url,
                                   base_url_hint=hint, help=help_text)
                   for p, (label, needs_key, needs_url, hint, help_text) in PROVIDER_FORMS.items()
                   if p in ENABLED_PROVIDERS and p.value in offered]
        options.sort(key=lambda o: offered.index(o.id))
        return Catalog(providers=[o.id for o in options], models=models, provider_options=options)

    def overview(self, organization_id: uuid.UUID, *, manage: bool) -> AIOverview:
        org = self.organization(organization_id)
        return AIOverview(
            organization_id=org.id, organization_name=org.name, can_manage=manage,
            keys=[self.key_read(k) for k in self._keys(organization_id)] if manage else [],
            models=self.models_read(organization_id),
            questions=self.quota_read(organization_id), tokens=self.token_read(organization_id),
            credits=self.credit_read(organization_id, with_balance=manage),
            notifications=self.notifications_read(organization_id) if manage else [],
            daily_usage=self.daily_usage(organization_id), catalog=self.catalog(organization_id) if manage else None,
        )

