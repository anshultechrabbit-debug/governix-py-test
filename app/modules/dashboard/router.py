import uuid
from datetime import date, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import get_app_settings
from app.core.exceptions import PermissionDeniedError
from app.core.responses import ApiResponse, ok
from app.modules.ai_usage.service import usage_overview
from app.modules.audit.model import AuditEvent
from app.modules.auth.dependencies import get_current_principal, require
from app.modules.auth.permissions import Permission, Principal, Role
from app.modules.dashboard import service
from app.modules.dashboard.schema import BranchDashboard, OrganizationDashboard, Period, PlatformDashboard
from app.modules.documents.model import Document, DocumentStatus
from app.modules.documents.repository import document_visible
from app.modules.ingestion.model import Decision, DocumentAnalysis, ReviewStatus
from app.modules.policies.model import Policy, PolicyStatus, PolicyVersion, VersionStatus
from app.modules.policies.repository import policy_visible

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

Reader = Annotated[Principal, Depends(require(Permission.DOCUMENTS_READ))]


class RecentChange(BaseModel):
    policy_id: uuid.UUID
    policy_name: str
    version_id: uuid.UUID
    version_label: str
    effective_from: date
    effective_date_source: str = "entered"
    confirmed_at: datetime | None


class RecentQuery(BaseModel):
    id: uuid.UUID
    question: str
    status: str | None
    created_at: datetime


class Dashboard(BaseModel):
    counts: dict[str, int]
    recent_changes: list[RecentChange]
    recent_queries: list[RecentQuery]
    attention: list[dict[str, Any]]


@router.get("", response_model=ApiResponse[Dashboard])
def dashboard(principal: Reader, db: Annotated[Session, Depends(get_db)]):
    visible_docs = document_visible(principal)
    by_status = dict(db.execute(
        select(Document.status, func.count()).where(visible_docs).group_by(Document.status)
    ).all())
    processing = sum(by_status.get(s, 0) for s in (DocumentStatus.UPLOADED, DocumentStatus.PROCESSING, DocumentStatus.INDEXING))
    pending = db.execute(
        select(DocumentAnalysis.decision, func.count())
        .join(Document, Document.id == DocumentAnalysis.document_id)
        .where(visible_docs, DocumentAnalysis.review_status == ReviewStatus.PENDING,
               Document.status == DocumentStatus.AWAITING_CONFIRMATION)
        .group_by(DocumentAnalysis.decision)
    ).all()
    pending = dict(pending)
    active_policies = db.scalar(
        select(func.count()).select_from(Policy).where(policy_visible(principal), Policy.status == PolicyStatus.ACTIVE)
    ) or 0

    changes = db.execute(
        select(PolicyVersion, Policy)
        .join(Policy, Policy.id == PolicyVersion.policy_id)
        .where(policy_visible(principal), PolicyVersion.status == VersionStatus.ACTIVE)
        .order_by(PolicyVersion.created_at.desc())
        .limit(8)
    ).all()

    queries = select(AuditEvent).where(AuditEvent.action == "ai.query")
    if principal.role is Role.ORG_ADMIN:
        queries = queries.where(AuditEvent.organization_id == principal.organization_id)
    else:
        queries = queries.where(AuditEvent.actor_user_id == principal.user_id)
    recent_queries = db.scalars(queries.order_by(AuditEvent.created_at.desc()).limit(8)).all()

    attention = [
        {"id": str(d.id), "title": d.title or d.original_filename, "status": d.status,
         "reason": (d.error or {}).get("message") if d.status == DocumentStatus.FAILED else "Awaiting confirmation"}
        for d in db.scalars(
            select(Document).where(visible_docs, Document.status.in_([DocumentStatus.FAILED, DocumentStatus.AWAITING_CONFIRMATION]))
            .order_by(Document.updated_at.desc()).limit(10)
        )
    ]
    return ok(Dashboard(
        counts={
            "documents": sum(v for k, v in by_status.items() if k not in (DocumentStatus.REJECTED,)),
            "ready_documents": by_status.get(DocumentStatus.READY, 0),
            "active_policies": active_policies,
            "processing": processing,
            "awaiting_confirmation": by_status.get(DocumentStatus.AWAITING_CONFIRMATION, 0),
            "failed": by_status.get(DocumentStatus.FAILED, 0),
            "version_conflicts": pending.get(Decision.VERSION_CONFLICT, 0),
            "requires_review": sum(pending.values()),
        },
        recent_changes=[
            RecentChange(policy_id=p.id, policy_name=p.name, version_id=v.id, version_label=v.version_label,
                         effective_from=v.effective_from, effective_date_source=v.effective_date_source,
                         confirmed_at=v.confirmed_at)
            for v, p in changes
        ],
        recent_queries=[
            RecentQuery(id=e.id, question=e.details.get("question", ""), status=e.details.get("status"),
                        created_at=e.created_at)
            for e in recent_queries
        ],
        attention=attention,
    ))


def _only(role: Role, page: str):
    def check(principal: Annotated[Principal, Depends(get_current_principal)]) -> Principal:
        if principal.role is not role:
            raise PermissionDeniedError(f"This is the {page} dashboard.", code="WRONG_DASHBOARD")
        return principal
    return check


Rows = Annotated[int, Query(ge=1, le=50, description="How many rows each list section holds.")]
Activity = Annotated[int, Query(ge=1, le=50, description="How many entries the activity feed holds.")]
PeriodQuery = Annotated[Period, Query(description="Activity charts: 7 days (daily), 4 weeks (weekly), 3 months.")]


@router.get("/platform", response_model=ApiResponse[PlatformDashboard])
def platform_dashboard(
    _principal: Annotated[Principal, Depends(_only(Role.MASTER_ADMIN, "Master Admin"))],
    db: Annotated[Session, Depends(get_db)],
    settings=Depends(get_app_settings),
    period: PeriodQuery = "7d",
    organizations: Annotated[int, Query(ge=1, le=100, description="Recent organizations listed.")] = 10,
    audit: Activity = 10,
):
    """The Master Admin's dashboard: organizations, users, ports, Technical Support tickets, app activity per
    organization and the latest audit events. Counts only: never an organization's documents."""
    return ok(service.platform(db, settings, period=period, organizations_limit=organizations, audit_limit=audit))


@router.get("/organization", response_model=ApiResponse[OrganizationDashboard])
def organization_dashboard(
    principal: Annotated[Principal, Depends(_only(Role.ORG_ADMIN, "Org Admin"))],
    db: Annotated[Session, Depends(get_db)],
    settings=Depends(get_app_settings),
    period: PeriodQuery = "7d",
    rows: Rows = 5,
    activity: Activity = 8,
):
    """The Org Admin's dashboard: users, branches and their managers, the library, how far policies reached their
    Users (opened since assigned), uploads and assignments over time, the latest audit events, the portal's port."""
    return ok(service.organization(db, settings, principal, period=period, rows=rows, activity=activity))


@router.get("/branch", response_model=ApiResponse[BranchDashboard])
def branch_dashboard(
    principal: Annotated[Principal, Depends(_only(Role.BRANCH_MANAGER, "Branch Manager"))],
    db: Annotated[Session, Depends(get_db)],
    period: PeriodQuery = "7d",
    rows: Rows = 5,
    activity: Activity = 8,
):
    """The Branch Manager's dashboard: the branch's documents, staff by department, assignments and how many were
    opened, open tickets, uploads and assignments over time, and what happened in the branch lately."""
    return ok(service.branch(db, principal, period=period, rows=rows, activity=activity))


def _platform_admin(principal: Annotated[Principal, Depends(get_current_principal)]) -> Principal:
    """The provider key and its credit are the platform's: shown to organization and platform admins."""
    if principal.role not in (Role.MASTER_ADMIN, Role.ORG_ADMIN):
        raise PermissionDeniedError("Only administrators can see AI usage and credit.")
    return principal


@router.get("/ai-usage", response_model=ApiResponse[dict])
def ai_usage(
    _principal: Annotated[Principal, Depends(_platform_admin)],
    db: Annotated[Session, Depends(get_db)],
    settings=Depends(get_app_settings),
):
    """AI provider usage (tokens, estimated or exact cost) and the credit left, if one is configured."""
    return ok(usage_overview(db, settings))
