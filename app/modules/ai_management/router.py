"""Organization-wise AI management: /organizations/{organization_id}/ai, plus /ai/status and /ai/platform.

The Master Admin configures and activates every organization. An Org Admin reads its own organization's
status, limits and usage (never keys or alert settings) and changes nothing. No one else, and never another
organization. Adding, rotating, enabling and disabling a key needs a fresh authenticator code (auth/step_up.py).
Every change re-evaluates the organization's AI status at once (lifecycle.after_change): no restart, no new
sign-in.
"""

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.database import get_db
from app.core.dependencies import get_app_settings
from app.core.exceptions import PermissionDeniedError
from app.core.responses import ApiResponse, ok
from app.modules.ai_management.model import NotifResource
from app.modules.ai_management import lifecycle
from app.modules.ai_management.reports import Filters, platform_report, usage_report
from app.modules.ai_management.schema import (
    AIOverview, AIStatusRead, APIKeyCreate, APIKeyRead, APIKeyRotate, APIKeyUpdate, CreditRead, CreditSet, ModelAssign,
    ModelsRead, ModelUpdate, NotifRead, NotifSet, PlatformAIReport, ProviderAccount, QuotaRead, QuotaSet,
    ReadinessRead, StatusMeters, SuspendRequest, TokenRead, TokenSet, UsageReport,
)
from app.modules.ai_management.service import AIManagementService
from app.modules.ai_management.status import USER_MESSAGE, AIStatus, readiness
from app.modules.audit.service import record_event
from app.modules.auth.dependencies import CurrentPrincipal, require
from app.modules.auth.permissions import Permission, Principal, Role
from app.modules.auth.step_up import StepUp

router = APIRouter(prefix="/organizations/{organization_id}/ai", tags=["ai-management"])

MasterAdmin = Annotated[Principal, Depends(require(Permission.ORGANIZATIONS_MANAGE))]


def get_service(db: Annotated[Session, Depends(get_db)],
                settings: Annotated[Settings, Depends(get_app_settings)]) -> AIManagementService:
    return AIManagementService(db, settings)


Service = Annotated[AIManagementService, Depends(get_service)]


def _reader(organization_id: uuid.UUID, principal: CurrentPrincipal) -> Principal:
    """The Master Admin, or an Org Admin of this very organization."""
    if principal.role == Role.MASTER_ADMIN:
        return principal
    if principal.role == Role.ORG_ADMIN and principal.organization_id == organization_id:
        return principal
    raise PermissionDeniedError("You can't view this organization's AI settings.")


Reader = Annotated[Principal, Depends(_reader)]


def _audit(service: AIManagementService, principal: Principal, organization_id: uuid.UUID, action: str,
           details: dict) -> None:
    """Record the change, then everything it sets off (status, alerts, notifications, resumed indexing). A changed
    status is audited too."""
    record_event(service.db, f"organization.ai.{action}", actor=principal, organization_id=organization_id,
                 resource_type="organization", resource_id=organization_id, details=details)
    service.db.commit()
    _changed(service, principal, organization_id, action)


def _changed(service: AIManagementService, principal: Principal, organization_id: uuid.UUID, action: str) -> None:
    from app.modules.ai_management.model import OrgAISettings
    from sqlalchemy import select

    row = service.db.scalar(select(OrgAISettings).where(OrgAISettings.organization_id == organization_id))
    before = row.notified_status if row else None
    after = lifecycle.after_change(service, organization_id)
    current = AIStatus.ACTIVE if after.status == AIStatus.LIMIT_REACHED else after.status
    if before is not None and current != before:
        record_event(service.db, "organization.ai.status_changed", actor=principal, organization_id=organization_id,
                     resource_type="organization", resource_id=organization_id,
                     details={"from": str(before), "to": str(after.status), "after": action})
        service.db.commit()


@router.get("", response_model=ApiResponse[AIOverview])
def overview(organization_id: uuid.UUID, principal: Reader, service: Service):
    """Everything on the AI Management page: status, configuration, usage, remaining, alerts."""
    data = service.overview(organization_id, manage=principal.role == Role.MASTER_ADMIN)
    data.readiness = lifecycle.readiness_read(readiness(service, organization_id))
    return ok(data)


@router.get("/status", response_model=ApiResponse[ReadinessRead])
def organization_status(organization_id: uuid.UUID, principal: Reader, service: Service):
    """The organization's AI status, computed now from its configuration and usage."""
    service.organization(organization_id)
    return ok(lifecycle.readiness_read(readiness(service, organization_id)))


@router.post("/activate", response_model=ApiResponse[ReadinessRead])
def activate(organization_id: uuid.UUID, principal: MasterAdmin, service: Service):
    """Switch AI on for the organization. Refused (409) while anything required is missing or invalid."""
    lifecycle.activate(service, organization_id, principal.user_id)
    _audit(service, principal, organization_id, "activated", {})
    return ok(lifecycle.readiness_read(readiness(service, organization_id)))


@router.post("/suspend", response_model=ApiResponse[ReadinessRead])
def suspend(organization_id: uuid.UUID, body: SuspendRequest, principal: MasterAdmin, service: Service):
    """Switch AI off for the organization at once. Everything that isn't AI keeps working."""
    lifecycle.suspend(service, organization_id, principal.user_id, body.reason)
    _audit(service, principal, organization_id, "suspended", {"reason": body.reason})
    return ok(lifecycle.readiness_read(readiness(service, organization_id)))


@router.get("/usage", response_model=ApiResponse[UsageReport])
def usage(
    organization_id: uuid.UUID, principal: Reader, service: Service,
    date_from: Annotated[datetime | None, Query(alias="from")] = None,
    date_to: Annotated[datetime | None, Query(alias="to")] = None,
    branch_id: uuid.UUID | None = None, user_id: uuid.UUID | None = None, feature: str | None = None,
    model: str | None = None, provider: str | None = None,
):
    """The organization's AI usage in a period (default: this month), by feature, model, branch and user."""
    service.organization(organization_id)
    return ok(usage_report(service.db, Filters(
        organization_ids=[organization_id], since=_utc(date_from), until=_utc(date_to), branch_id=branch_id,
        user_id=user_id, feature=feature, model=model, provider=provider)))


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


# 1. API key vault ---------------------------------------------------------------------------------------------------

@router.post("/keys", response_model=ApiResponse[APIKeyRead], status_code=201)
def add_key(organization_id: uuid.UUID, body: APIKeyCreate, principal: MasterAdmin, service: Service,
            confirm_code: StepUp):
    confirm_code("organization.ai_key.add")
    key = service.add_key(organization_id, body, principal.user_id)
    _audit(service, principal, organization_id, "key_added", {"provider": key.provider, "kind": key.kind,
                                                               "key": key.key_hint, "validation": key.validation_status})
    return ok(service.key_read(key))


@router.patch("/keys/{key_id}", response_model=ApiResponse[APIKeyRead])
def relabel_key(organization_id: uuid.UUID, key_id: uuid.UUID, body: APIKeyUpdate, principal: MasterAdmin,
                service: Service):
    key = service.relabel_key(organization_id, key_id, body.label)
    _audit(service, principal, organization_id, "key_relabelled", {"provider": key.provider, "key": key.key_hint})
    return ok(service.key_read(key))


@router.post("/keys/{key_id}/rotate", response_model=ApiResponse[APIKeyRead])
def rotate_key(organization_id: uuid.UUID, key_id: uuid.UUID, body: APIKeyRotate, principal: MasterAdmin,
               service: Service, confirm_code: StepUp):
    confirm_code("organization.ai_key.rotate")
    key = service.rotate_key(organization_id, key_id, body.api_key)
    _audit(service, principal, organization_id, "key_rotated",
           {"provider": key.provider, "key": key.key_hint, "validation": key.validation_status})
    return ok(service.key_read(key))


@router.post("/keys/{key_id}/disable", response_model=ApiResponse[APIKeyRead])
def disable_key(organization_id: uuid.UUID, key_id: uuid.UUID, principal: MasterAdmin, service: Service,
                confirm_code: StepUp):
    confirm_code("organization.ai_key.disable")
    key = service.set_key_active(organization_id, key_id, False)
    _audit(service, principal, organization_id, "key_disabled", {"provider": key.provider, "key": key.key_hint})
    return ok(service.key_read(key))


@router.post("/keys/{key_id}/enable", response_model=ApiResponse[APIKeyRead])
def enable_key(organization_id: uuid.UUID, key_id: uuid.UUID, principal: MasterAdmin, service: Service,
               confirm_code: StepUp):
    confirm_code("organization.ai_key.enable")
    key = service.set_key_active(organization_id, key_id, True)
    _audit(service, principal, organization_id, "key_enabled", {"provider": key.provider, "key": key.key_hint})
    return ok(service.key_read(key))


@router.post("/keys/{key_id}/validate", response_model=ApiResponse[APIKeyRead])
def validate_key(organization_id: uuid.UUID, key_id: uuid.UUID, principal: MasterAdmin, service: Service):
    key = service.validate_key(organization_id, key_id)
    service.db.commit()
    lifecycle.after_change(service, organization_id)
    return ok(service.key_read(key))


@router.get("/provider-account", response_model=ApiResponse[ProviderAccount])
def provider_account(organization_id: uuid.UUID, principal: MasterAdmin, service: Service, refresh: bool = False):
    """What OpenAI itself reports for this organization's credit period (Admin key only); `refresh` skips the
    10-minute cache."""
    service.organization(organization_id)
    start = service.credit_read(organization_id, with_balance=False).period_start
    return ok(service.provider_account(organization_id, start, refresh=refresh))


# 2. Models ----------------------------------------------------------------------------------------------------------

@router.post("/models", response_model=ApiResponse[ModelsRead], status_code=201)
def assign_model(organization_id: uuid.UUID, body: ModelAssign, principal: MasterAdmin, service: Service):
    row = service.assign_model(organization_id, body)
    _audit(service, principal, organization_id, "model_assigned",
           {"provider": row.provider, "model": row.model_id, "service": row.service, "default": row.is_default})
    return ok(service.models_read(organization_id))


@router.patch("/models/{model_id}", response_model=ApiResponse[ModelsRead])
def update_model(organization_id: uuid.UUID, model_id: uuid.UUID, body: ModelUpdate, principal: MasterAdmin,
                 service: Service):
    row = service.update_model(organization_id, model_id, body)
    _audit(service, principal, organization_id, "model_updated",
           {"model": row.model_id, "service": row.service, **body.model_dump(exclude_unset=True)})
    return ok(service.models_read(organization_id))


@router.delete("/models/{model_id}", response_model=ApiResponse[ModelsRead])
def remove_model(organization_id: uuid.UUID, model_id: uuid.UUID, principal: MasterAdmin, service: Service):
    service.remove_model(organization_id, model_id)
    _audit(service, principal, organization_id, "model_removed", {"model_assignment_id": str(model_id)})
    return ok(service.models_read(organization_id))


# 3-5. Limits --------------------------------------------------------------------------------------------------------

@router.put("/questions", response_model=ApiResponse[QuotaRead])
def set_questions(organization_id: uuid.UUID, body: QuotaSet, principal: MasterAdmin, service: Service):
    service.set_quota(organization_id, body)
    _audit(service, principal, organization_id, "question_limit_set", body.model_dump(mode="json"))
    return ok(service.quota_read(organization_id))


@router.put("/tokens", response_model=ApiResponse[TokenRead])
def set_tokens(organization_id: uuid.UUID, body: TokenSet, principal: MasterAdmin, service: Service):
    service.set_tokens(organization_id, body)
    _audit(service, principal, organization_id, "token_limits_set", body.model_dump(mode="json"))
    return ok(service.token_read(organization_id))


@router.put("/credits", response_model=ApiResponse[CreditRead])
def set_credits(organization_id: uuid.UUID, body: CreditSet, principal: MasterAdmin, service: Service):
    service.set_credits(organization_id, body)
    _audit(service, principal, organization_id, "credit_budget_set", body.model_dump(mode="json"))
    return ok(service.credit_read(organization_id))


# 6. Notifications ---------------------------------------------------------------------------------------------------

@router.put("/notifications/{resource}", response_model=ApiResponse[list[NotifRead]])
def set_notifications(organization_id: uuid.UUID, resource: NotifResource, body: NotifSet, principal: MasterAdmin,
                      service: Service):
    service.set_notifications(organization_id, resource, body)
    _audit(service, principal, organization_id, "alerts_set", {"resource": resource, **body.model_dump(mode="json")})
    return ok(service.notifications_read(organization_id))


# /ai/status and /ai/platform ------------------------------------------------------------------------------------

status_router = APIRouter(prefix="/ai", tags=["ai-management"])


@status_router.get("/status", response_model=ApiResponse[AIStatusRead])
def my_status(principal: CurrentPrincipal, service: Service):
    """What the signed-in person's organization may do with AI now; the pages poll it, so a change made by the
    Master Admin shows within a minute without signing in again. Everyone in the organization gets the status and
    a message; Org Admins also get what is wrong and the usage meters. Never keys or other configuration."""
    now = datetime.now(UTC)
    if principal.organization_id is None:
        return ok(AIStatusRead(scope="platform", ready=True, checked_at=now))
    org = service.organization(principal.organization_id)
    state = readiness(service, principal.organization_id)
    admin = principal.role == Role.ORG_ADMIN
    if state.ready:
        message = ""
    elif admin:
        message = state.admin_message
    elif state.status == AIStatus.LIMIT_REACHED:
        message = ("Your organization has reached its AI usage limit for this period. "
                   "Please contact your Organization Admin.")
    else:
        message = USER_MESSAGE
    meters = None
    if admin:
        quota = service.quota_read(org.id)
        tokens = service.token_read(org.id)
        credits = service.credit_read(org.id, with_balance=False)
        meters = StatusMeters(questions=quota.questions, tokens=tokens.total, credits=credits.credits,
                              questions_resets_at=quota.resets_at, tokens_resets_at=tokens.resets_at,
                              credits_resets_at=credits.resets_at)
    return ok(AIStatusRead(
        scope="organization", organization_id=org.id, organization_name=org.name, status=state.status,
        ready=state.ready, code=state.code, message=message,
        details=lifecycle.readiness_read(state) if admin else None, meters=meters, checked_at=now,
    ))


@status_router.get("/platform", response_model=ApiResponse[PlatformAIReport])
def platform(
    principal: MasterAdmin, service: Service,
    date_from: Annotated[datetime | None, Query(alias="from")] = None,
    date_to: Annotated[datetime | None, Query(alias="to")] = None,
    organization_id: uuid.UUID | None = None, provider: str | None = None, model: str | None = None,
    feature: str | None = None,
):
    """The Master Admin dashboard's AI section: every organization's AI status (with the ones that need
    configuring first) and the platform's usage, filtered by organization, period, provider, model and feature."""
    return ok(platform_report(service, Filters(
        organization_ids=[organization_id] if organization_id else None, since=_utc(date_from),
        until=_utc(date_to), feature=feature, model=model, provider=provider)))
