"""Is an organization AI-ready? Computed from its configuration and usage on every check, never stored.

| Status          | Meaning                                                                   |
|-----------------|---------------------------------------------------------------------------|
| NOT_CONFIGURED  | nothing set up: no models, no keys, no limits, never activated            |
| INCOMPLETE      | something required is missing (a model, a key, a key check, activation)   |
| INVALID         | the provider rejected the key, the key can't be read, or it can't use the |
|                 | assigned model                                                            |
| ACTIVE          | configured, checked and switched on by the Master Admin                   |
| SUSPENDED       | the Master Admin switched AI off on purpose                               |
| LIMIT_REACHED   | active, but a hard question, token or budget limit is used up             |

Required for AI: an enabled answering model; an active API key the provider accepted for the provider of that
model and of the embedding model (the local and Ollama providers need none); and activation. Without an assigned
embedding model the platform's embedding model is called with the organization's own key, so the vectors match
the index. Usage limits are recommended, not required: leaving them unset means unlimited, which is shown as a
warning.

`ensure_ready` is the gate every AI call passes (ai_management.runtime). With AI_REQUIRE_ORG_CONFIG off it lets
everything through, as before.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from sqlalchemy import select

from app.modules.ai_management.model import (
    AIProvider, KeyKind, ModelService, OrgAIModel, OrgAISettings, OrgAPIKey, OrgCreditConfig, OrgQuotaConfig,
    OrgTokenConfig,
)
from app.modules.auth import mfa


class AIStatus(StrEnum):
    NOT_CONFIGURED = "NOT_CONFIGURED"
    INCOMPLETE = "INCOMPLETE"
    INVALID = "INVALID"
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    LIMIT_REACHED = "LIMIT_REACHED"


# Statuses in which no AI call runs at all (LIMIT_REACHED is enforced limit by limit, by the meter).
BLOCKED = frozenset({AIStatus.NOT_CONFIGURED, AIStatus.INCOMPLETE, AIStatus.INVALID, AIStatus.SUSPENDED})

# Error code and HTTP status of a refused AI request, per status.
ERRORS: dict[str, tuple[str, int]] = {
    AIStatus.NOT_CONFIGURED: ("AI_NOT_CONFIGURED", 409),
    AIStatus.INCOMPLETE: ("AI_CONFIGURATION_INCOMPLETE", 409),
    AIStatus.INVALID: ("AI_CONFIGURATION_INVALID", 409),
    AIStatus.SUSPENDED: ("AI_SUSPENDED", 403),
}

# What anyone in the organization is told. Never names keys, models or other configuration.
USER_MESSAGE = "AI services are currently unavailable for your organization. Please contact your Organization Admin."

# Providers that need no API key.
KEYLESS = frozenset({AIProvider.LOCAL, AIProvider.OLLAMA})

PROVIDER_NAMES = {
    AIProvider.OPENAI: "OpenAI", AIProvider.OPENAI_COMPATIBLE: "the custom API", AIProvider.OPENCODE: "OpenCode",
    AIProvider.OLLAMA: "Ollama", AIProvider.LOCAL: "Local",
}


@dataclass
class Readiness:
    status: AIStatus
    # Whether the Master Admin has switched AI on (and not suspended it).
    activated: bool
    # Everything required is in place: the Master Admin may activate (or AI is already active).
    can_activate: bool
    # What must still be set up ("an answering model", "an API key for OpenAI", ...).
    missing: list[str] = field(default_factory=list)
    # What is set up but wrong: [(code, message)]. Any of these makes the status INVALID.
    problems: list[tuple[str, str]] = field(default_factory=list)
    # Hard limits used up in their current period ("questions", "total_tokens", "credits", ...).
    exhausted: list[str] = field(default_factory=list)
    # Worth knowing, never blocking (no limits set, the embedding token limit used up, ...).
    warnings: list[str] = field(default_factory=list)
    activated_at: datetime | None = None
    suspended_at: datetime | None = None
    suspended_reason: str | None = None

    @property
    def ready(self) -> bool:
        """AI may be used now (some requests may still be refused by a limit of their own kind)."""
        return self.status == AIStatus.ACTIVE

    @property
    def code(self) -> str | None:
        if self.status in ERRORS:
            if self.status == AIStatus.INVALID and self.problems:
                return self.problems[0][0]
            return ERRORS[self.status][0]
        if self.status == AIStatus.LIMIT_REACHED:
            return _LIMIT_CODES.get(self.exhausted[0], "AI_LIMIT_REACHED") if self.exhausted else "AI_LIMIT_REACHED"
        return None

    @property
    def admin_message(self) -> str:
        """For the Master Admin and the Org Admin: what is wrong, in a sentence."""
        if self.status == AIStatus.ACTIVE:
            return "AI is configured and active."
        if self.status == AIStatus.SUSPENDED:
            return "The platform administrator has suspended AI for this organization" + (
                f": {self.suspended_reason}" if self.suspended_reason else ".")
        if self.status == AIStatus.INVALID:
            return " ".join(message for _, message in self.problems)
        if self.status == AIStatus.LIMIT_REACHED:
            names = ", ".join(_LIMIT_NAMES.get(e, e) for e in self.exhausted)
            return f"AI is active, but the organization has used up its {names} for this period."
        if self.status == AIStatus.NOT_CONFIGURED:
            return "AI has not been configured for this organization yet."
        if self.can_activate:
            return "Everything required is set up. AI starts once the platform administrator activates it."
        return "Still required: " + "; ".join(self.missing) + "."

    def error(self, *, embeddings: bool = False):
        """The exception refusing an AI request in this status (AIBlocked, or AIEmbeddingBlocked)."""
        from app.modules.ai_management.service import AIBlocked, AIEmbeddingBlocked

        code, status = ERRORS.get(self.status, ("AI_LIMIT_REACHED", 429))
        if self.status == AIStatus.INVALID and self.problems:
            code = self.problems[0][0]
        exc = (AIEmbeddingBlocked if embeddings else AIBlocked)(USER_MESSAGE, code=code)
        exc.status_code = status
        return exc


_LIMIT_CODES = {
    "questions": "QUESTION_LIMIT_REACHED", "credits": "CREDIT_LIMIT_REACHED", "input_tokens": "TOKEN_LIMIT_REACHED",
    "output_tokens": "TOKEN_LIMIT_REACHED", "total_tokens": "TOKEN_LIMIT_REACHED",
}
_LIMIT_NAMES = {
    "questions": "question limit", "credits": "AI budget", "input_tokens": "input token limit",
    "output_tokens": "output token limit", "total_tokens": "total token limit",
    "embedding_tokens": "embedding token limit",
}


def _settings_row(db, organization_id: uuid.UUID) -> OrgAISettings | None:
    return db.scalar(select(OrgAISettings).where(OrgAISettings.organization_id == organization_id))


def _listed(model: str, available: list[str]) -> bool:
    """Whether the provider's model list (from the key check) includes the model, or a dated version of it."""
    return any(m == model or m.startswith(f"{model}-") or model.startswith(f"{m}-") for m in available)


def _check_model(service, organization_id: uuid.UUID, kind: str, models: list[OrgAIModel],
                 keys: dict[str, OrgAPIKey], result: Readiness) -> None:
    """Add what the organization lacks to use `kind` (answers or embeddings) to `result`."""
    label = "answering" if kind == ModelService.ANSWERS else "embedding"
    rows = [m for m in models if m.service == kind]
    if rows:
        enabled = [m for m in rows if m.is_enabled]
        if not enabled:
            result.missing.append(f"an enabled {label} model")
            return
        chosen = next((m for m in enabled if m.is_default), enabled[0])
        provider, model = chosen.provider, chosen.model_id
    elif kind == ModelService.ANSWERS:
        result.missing.append("an answering model")
        return
    else:
        # No embedding model assigned: the platform's default one, if the organization has a key for its provider
        # (the vectors then match the search index); otherwise one must be assigned.
        provider, model = service.settings.EMBEDDING_PROVIDER.lower(), service.settings.EMBEDDING_MODEL
        if provider not in KEYLESS and provider not in keys:
            result.missing.append("an embedding model (assign one in Models)")
            return
    if provider in KEYLESS:
        return
    name = PROVIDER_NAMES.get(provider, provider)
    key = keys.get(provider)
    if key is None:
        result.missing.append(f"an API key for {name} (the {label} model {model} uses it)")
        return
    if mfa.decrypt(service.settings, key.key_encrypted) is None:
        problem = ("AI_CONFIGURATION_INVALID",
                   f"The {name} API key can no longer be read (the server's encryption key changed); rotate it.")
    elif key.validation_status == "invalid":
        problem = ("AI_CONFIGURATION_INVALID", f"{name} rejected the API key ({key.key_hint}).")
    elif key.validation_status != "valid":
        result.missing.append(f"a successful check of the {name} API key (press Check)")
        return
    else:
        available = list((key.details or {}).get("models") or [])
        if available and not _listed(model, available):
            problem = ("AI_MODEL_NOT_ALLOWED", f"The {name} API key cannot use the {label} model {model}.")
        else:
            return
    if problem not in result.problems:
        result.problems.append(problem)


def readiness(service, organization_id: uuid.UUID, *, limits: bool = True) -> Readiness:
    """The organization's AI status from its settings row, keys, models and (with `limits`) its usage.
    `service` is an AIManagementService."""
    db = service.db
    row = _settings_row(db, organization_id)
    models = service._models(organization_id)
    keys = {k.provider: k for k in service._keys(organization_id) if k.is_active and k.kind == KeyKind.API}
    activated = bool(row and row.is_active)
    result = Readiness(status=AIStatus.ACTIVE, activated=activated, can_activate=False,
                       activated_at=row.activated_at if row else None,
                       suspended_at=row.suspended_at if row else None,
                       suspended_reason=row.suspended_reason if row else None)
    _check_model(service, organization_id, ModelService.ANSWERS, models, keys, result)
    _check_model(service, organization_id, ModelService.EMBEDDINGS, models, keys, result)
    result.can_activate = not result.missing and not result.problems

    if limits:
        quota = service.quota_read(organization_id)
        tokens = service.token_read(organization_id)
        credits = service.credit_read(organization_id, with_balance=False)
        for name, meter in (("questions", quota.questions), ("input_tokens", tokens.input),
                            ("output_tokens", tokens.output), ("total_tokens", tokens.total),
                            ("credits", credits.credits)):
            if meter.exhausted:
                result.exhausted.append(name)
        if tokens.embedding.exhausted:
            result.warnings.append("The embedding token limit is used up: new documents wait to be indexed for AI "
                                   "until it resets or is raised.")
        if quota.questions.limit is None and tokens.total.limit is None and credits.credits.limit is None:
            result.warnings.append("No question, token or budget limit is set, so AI usage is unlimited.")
        embedding = next((m for m in models if m.service == ModelService.EMBEDDINGS and m.is_enabled and m.is_default),
                         None)
        if embedding is not None and embedding.provider in (AIProvider.OPENAI_COMPATIBLE, AIProvider.OPENCODE):
            result.warnings.append(
                f"The embedding model {embedding.model_id} must return vectors of "
                f"{service.settings.EMBEDDING_DIMENSIONS} dimensions (the size of the search index); check that it "
                "supports it, or indexing will fail.")
        if credits.unpriced_models:
            result.warnings.append("No price is known for " + ", ".join(credits.unpriced_models)
                                   + ": the budget can't be applied to it.")

    configured = bool(row or models or keys or any(db.scalar(select(m.id).where(m.organization_id == organization_id))
                                                   for m in (OrgQuotaConfig, OrgTokenConfig, OrgCreditConfig)))
    if row is not None and not row.is_active and row.suspended_at is not None:
        result.status = AIStatus.SUSPENDED
    elif result.problems:
        result.status = AIStatus.INVALID
    elif not configured:
        result.status = AIStatus.NOT_CONFIGURED
    elif result.missing:
        result.status = AIStatus.INCOMPLETE
    elif not activated:
        result.status = AIStatus.INCOMPLETE
        result.missing.append("activation by the platform administrator")
    elif result.exhausted:
        result.status = AIStatus.LIMIT_REACHED
    return result


def ensure_ready(service, organization_id: uuid.UUID, *, embeddings: bool = False) -> None:
    """Refuse any AI work of an organization that is not configured, checked and activated (the limits are
    applied call by call by the meter). Does nothing with AI_REQUIRE_ORG_CONFIG off."""
    if not service.settings.AI_REQUIRE_ORG_CONFIG:
        return
    state = readiness(service, organization_id, limits=False)
    if state.status in BLOCKED:
        raise state.error(embeddings=embeddings)


def is_active(db, organization_id: uuid.UUID) -> bool:
    """The one-row check made inside every reservation, so a suspension stops calls already being prepared."""
    row = _settings_row(db, organization_id)
    return bool(row and row.is_active)
