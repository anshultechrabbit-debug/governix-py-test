"""Organization-wise AI management: /organizations/{organization_id}/ai.

The Master Admin configures every organization. An Org Admin reads its own organization's limits and usage
(never keys or alert settings) and changes nothing. No one else, and never another organization.
Adding, rotating, enabling and disabling a key needs a fresh authenticator code (auth/step_up.py).
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.database import get_db
from app.core.dependencies import get_app_settings
from app.core.exceptions import PermissionDeniedError
from app.core.responses import ApiResponse, ok
from app.modules.ai_management.model import NotifResource
from app.modules.ai_management.schema import (
    AIOverview, APIKeyCreate, APIKeyRead, APIKeyRotate, APIKeyUpdate, CreditRead, CreditSet, ModelAssign, ModelsRead,
    ModelUpdate, NotifRead, NotifSet, ProviderAccount, QuotaRead, QuotaSet, TokenRead, TokenSet,
)
from app.modules.ai_management.service import AIManagementService
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
    record_event(service.db, f"organization.ai.{action}", actor=principal, organization_id=organization_id,
                 resource_type="organization", resource_id=organization_id, details=details)
    service.db.commit()


@router.get("", response_model=ApiResponse[AIOverview])
def overview(organization_id: uuid.UUID, principal: Reader, service: Service):
    """Everything on the AI Management page: configuration, usage, remaining, alerts."""
    return ok(service.overview(organization_id, manage=principal.role == Role.MASTER_ADMIN))


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
