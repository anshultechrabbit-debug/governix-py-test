"""Response models of the role dashboards (Master Admin, Org Admin, Branch Manager).

Every figure is counted from what Governix records. Two things the admin panel shows are not recorded, so they
are read this way:
- "Acknowledged / read": a live policy assignment counts as read once its User opened one of the policy's
  documents (`document.accessed` / `document.viewed` in the audit log) after it was assigned. Pending = not yet.
- Gateway traffic: nginx's own traffic is not recorded. The Master Admin sees each organization's activity in the
  app instead (sign-ins, AI questions, documents opened, uploads).
"""

import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.modules.audit.schema import AuditEventRead
from app.modules.nginx.schema import NginxState

Period = Literal["7d", "30d", "90d"]


class ActivityPoint(BaseModel):
    """One bar or point of an activity chart: a day (7d), a week (30d) or a month (90d)."""

    label: str = Field(description='"Mon", "Oct 01 - Oct 07" or "October".')
    start: date
    end: date
    uploads: int
    assignments: int


class PolicyReach(BaseModel):
    """How far a policy has reached its Users: live assignments, and how many of them were opened since."""

    policy_id: uuid.UUID
    name: str
    category: str | None
    assigned: int
    read: int
    pending: int
    read_percent: int | None = Field(description="read / assigned, rounded; None when nobody is assigned.")
    branches: int = Field(description="Branches whose Users hold an assignment of it.")


class DocumentRow(BaseModel):
    id: uuid.UUID
    title: str
    filename: str
    extension: str
    category: str | None
    version_label: str | None
    size_bytes: int
    status: str
    updated_at: datetime


# --- Master Admin ----------------------------------------------------------------------------------------------

class OrganizationCounts(BaseModel):
    total: int = Field(description="active + provisioning + suspended.")
    active: int = Field(description="Active and serving on its port.")
    suspended: int
    provisioning: int = Field(description="Active, but its port is not serving yet (no port, not deployed, disabled).")
    new_this_month: int


class UserCounts(BaseModel):
    total: int
    active: int
    new_last_30_days: int
    growth_percent: float | None = Field(description="Accounts added in the last 30 days, as a share of those before.")


class ProxyCounts(BaseModel):
    configured: bool = Field(description="False: deploying from Governix is off on this server (NGINX_CONF_DIR).")
    with_port: int
    running: int
    outdated: int
    disabled: int
    not_deployed: int
    first_port: int | None = Field(description="Lowest port being served.")
    last_port: int | None = Field(description="Highest port being served.")


class AdminCounts(BaseModel):
    total: int
    active: int
    organizations: int = Field(description="Organizations that have at least one active Org Admin.")


class TicketCounts(BaseModel):
    total: int = Field(description="Every sent ticket (drafts awaiting confirmation are left out).")
    open: int = Field(description="Waiting for or assigned to staff, not yet worked on.")
    in_progress: int = Field(description="In progress, or waiting for the user or another team.")
    resolved: int = Field(description="Resolved or closed.")
    cancelled: int
    overdue: int
    sla_met_percent: int | None = Field(description="Resolved tickets finished by their due time; None when none.")


class StatusCount(BaseModel):
    status: Literal["open", "in_progress", "resolved", "cancelled"]
    count: int
    percent: int


class CategoryCount(BaseModel):
    name: str
    total: int
    open: int


class SupportSummary(BaseModel):
    counts: TicketCounts
    by_status: list[StatusCount]
    categories: list[CategoryCount]


class OrganizationRow(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    contact_email: str | None
    port: int | None
    status: Literal["active", "suspended", "provisioning"]
    nginx_state: NginxState | None
    admins: int
    branches: int
    users: int
    storage_bytes: int
    created_at: datetime


class PortRow(BaseModel):
    port: int
    organization_id: uuid.UUID
    organization_name: str
    state: NginxState


class RoleCount(BaseModel):
    role: str
    count: int
    percent: int


class OrganizationActivity(BaseModel):
    organization_id: uuid.UUID
    name: str
    port: int | None
    sign_ins: int
    questions: int
    documents_opened: int
    uploads: int
    total: int


class PlatformDashboard(BaseModel):
    period: Period
    organizations: OrganizationCounts
    users: UserCounts
    proxies: ProxyCounts
    org_admins: AdminCounts
    support: SupportSummary = Field(description="Technical Support (platform) tickets: the queue Governix works.")
    recent_organizations: list[OrganizationRow]
    ports: list[PortRow]
    roles: list[RoleCount]
    activity_by_organization: list[OrganizationActivity] = Field(description="In the period, busiest first.")
    audit: list[AuditEventRead]


# --- Org Admin -------------------------------------------------------------------------------------------------

class OrgUserCounts(BaseModel):
    total: int
    active: int
    new_last_90_days: int
    by_role: dict[str, int]


class OrgBranchCounts(BaseModel):
    total: int
    active: int
    with_manager: int = Field(description="Branches with at least one active Branch Manager.")


class OrgDocumentCounts(BaseModel):
    total: int = Field(description="The library: every document except rejected and archived ones.")
    ready: int
    processing: int
    needs_attention: int = Field(description="Failed, or awaiting confirmation.")


class BranchLoad(BaseModel):
    branch_id: uuid.UUID
    name: str
    code: str
    users: int
    managers: int
    is_active: bool
    status: Literal["active", "needs_attention", "inactive"] = Field(
        description="needs_attention: an active branch with no active Branch Manager."
    )


class BranchRow(BaseModel):
    branch_id: uuid.UUID
    name: str
    code: str
    location: str | None
    manager_name: str | None
    manager_email: str | None
    is_active: bool
    created_at: datetime


class PortalStatus(BaseModel):
    port: int | None
    state: NginxState | None


class OrganizationDashboard(BaseModel):
    period: Period
    users: OrgUserCounts
    branches: OrgBranchCounts
    documents: OrgDocumentCounts
    branch_users: list[BranchLoad]
    top_policies: list[PolicyReach]
    document_activity: list[ActivityPoint]
    latest_branches: list[BranchRow]
    recent_documents: list[DocumentRow]
    activity: list[AuditEventRead]
    portal: PortalStatus


# --- Branch Manager --------------------------------------------------------------------------------------------

class BranchCounts(BaseModel):
    documents: int = Field(description="Documents the branch can see (its own and the organization-wide ones).")
    staff: int = Field(description="Active Users in the branch.")
    assignments: int = Field(description="Live policy assignments held by the branch's Users.")
    open_tickets: int = Field(description="Active support tickets raised in the branch.")


class DepartmentCount(BaseModel):
    department_id: uuid.UUID | None
    name: str
    count: int
    percent: int


class AssignmentStatus(BaseModel):
    read: int
    pending: int
    unassigned_policies: int = Field(description="Active policies the branch can see that no User of it holds.")


class ActivityItem(BaseModel):
    kind: Literal["ticket", "document", "assignment", "staff"]
    title: str
    detail: str
    status: str
    at: datetime
    resource_type: str
    resource_id: uuid.UUID


class BranchDashboard(BaseModel):
    period: Period
    branch_id: uuid.UUID
    branch_name: str
    counts: BranchCounts
    departments: list[DepartmentCount]
    document_activity: list[ActivityPoint]
    assignment_status: AssignmentStatus
    recent_documents: list[DocumentRow]
    top_policies: list[PolicyReach]
    activity: list[ActivityItem]
