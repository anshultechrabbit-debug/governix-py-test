import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.pagination import Page, PageParams, page_params
from app.core.responses import ApiResponse, ok
from app.modules.audit.describe import describe
from app.modules.audit.repository import AuditRepository
from app.modules.audit.schema import AuditEventRead
from app.modules.auth.dependencies import require
from app.modules.auth.permissions import Permission, Principal

router = APIRouter(prefix="/audit-events", tags=["audit"])

Auditor = Annotated[Principal, Depends(require(Permission.AUDIT_READ))]


def _scope(principal: Principal, organization_id: uuid.UUID | None) -> tuple[uuid.UUID | None, bool]:
    """(organization, every organization). An Organization Admin reads its own organization's events, whatever
    it asks for. The master admin reads every organization's and the platform's, or one organization's."""
    if principal.is_tenant_user:
        return principal.organization_id, False
    return organization_id, organization_id is None


@router.get("", response_model=ApiResponse[Page[AuditEventRead]])
def list_audit_events(
    principal: Auditor,
    db: Annotated[Session, Depends(get_db)],
    page: Annotated[PageParams, Depends(page_params)],
    action: Annotated[str | None, Query(max_length=100)] = None,
    resource_type: Annotated[str | None, Query(max_length=50)] = None,
    resource_id: uuid.UUID | None = None,
    actor_user_id: uuid.UUID | None = None,
    since: datetime | None = None,
    organization_id: uuid.UUID | None = None,  # master admin only
):
    scope, every = _scope(principal, organization_id)
    items, total = AuditRepository(db).list(
        scope,
        every_organization=every,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        actor_user_id=actor_user_id,
        since=since,
        limit=page.limit,
        offset=page.offset,
    )
    return ok(Page(items=describe(db, items), total=total, **page.model_dump()))


@router.get("/actions", response_model=ApiResponse[list[str]])
def list_audit_actions(
    principal: Auditor,
    db: Annotated[Session, Depends(get_db)],
    organization_id: uuid.UUID | None = None,  # master admin only
):
    scope, every = _scope(principal, organization_id)
    return ok(AuditRepository(db).actions(scope, every_organization=every))
