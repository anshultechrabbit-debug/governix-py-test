"""Figures for the role dashboards. schema.py says how "read" and an organization's app activity are counted.

Days are UTC days. Every query is scoped by the caller: the Master Admin's to the platform (counts only, never an
organization's documents), the Org Admin's to its organization, the Branch Manager's to its branch (and the
organization-wide documents and policies it can see).
"""

import uuid
from calendar import monthrange
from datetime import UTC, date, datetime, time, timedelta
from pathlib import PurePosixPath

from sqlalchemy import Date, cast, distinct, exists, func, literal_column, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.modules.assignments.model import PolicyAssignment
from app.modules.audit.describe import describe
from app.modules.audit.model import AuditEvent
from app.modules.auth.permissions import Principal, Role
from app.modules.branches.model import Branch
from app.modules.categories.model import Category
from app.modules.dashboard.schema import (
    ActivityItem, ActivityPoint, AdminCounts, AssignmentStatus, BranchCounts, BranchDashboard, BranchLoad, BranchRow,
    CategoryCount, DepartmentCount, DocumentRow, OrganizationActivity, OrganizationCounts, OrganizationDashboard,
    OrganizationRow, OrgBranchCounts, OrgDocumentCounts, OrgUserCounts, Period, PlatformDashboard, PolicyReach,
    PortalStatus, PortRow, ProxyCounts, RoleCount, StatusCount, SupportSummary, TicketCounts, UserCounts,
)
from app.modules.departments.model import Department
from app.modules.documents.model import Document, DocumentStatus
from app.modules.documents.repository import document_visible
from app.modules.nginx.service import NginxService
from app.modules.organizations.model import Organization, OrganizationStatus
from app.modules.policies.model import Policy, PolicyStatus, PolicyVersion
from app.modules.policies.repository import policy_visible
from app.modules.tickets.model import ACTIVE_STATUSES, Ticket, TicketLevel, TicketStatus
from app.modules.users.model import User

# A User opened a policy's document: the viewer sends one of these when a document is opened.
OPENED = ("document.accessed", "document.viewed")
# Not part of the library.
SET_ASIDE = (DocumentStatus.REJECTED, DocumentStatus.ARCHIVED)
PROCESSING = (DocumentStatus.UPLOADED, DocumentStatus.PROCESSING, DocumentStatus.INDEXING)
NEEDS_ATTENTION = (DocumentStatus.FAILED, DocumentStatus.AWAITING_CONFIRMATION)
WORKING = (TicketStatus.IN_PROGRESS, TicketStatus.WAITING_FOR_USER, TicketStatus.WAITING_FOR_TEAM)
DONE = (TicketStatus.RESOLVED, TicketStatus.CLOSED)
# An organization's activity in the app, by audit action.
APP_ACTIVITY = {
    "auth.login": "sign_ins", "ai.query": "questions", "document.accessed": "documents_opened",
    "document.uploaded": "uploads",
}
NOT_SERVING = ("not_deployed", "disabled")
SERVING = ("deployed", "outdated")


def _percent(part: int, whole: int) -> int:
    return round(part * 100 / whole) if whole else 0


def _midnight(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=UTC)


def spans(period: Period, today: date) -> list[tuple[str, date, date]]:
    """(label, first day, last day), oldest first: 7 days, 4 weeks, or this month and the two before."""
    if period == "7d":
        return [(day.strftime("%a"), day, day) for day in (today - timedelta(days=n) for n in range(6, -1, -1))]
    if period == "30d":
        weeks = []
        for n in range(3, -1, -1):
            end = today - timedelta(days=7 * n)
            start = end - timedelta(days=6)
            weeks.append((f"{start:%b %d} - {end:%b %d}", start, end))
        return weeks
    months = []
    for n in range(2, -1, -1):
        year, month = today.year, today.month - n
        if month < 1:
            year, month = year - 1, month + 12
        start = date(year, month, 1)
        months.append((f"{start:%B}", start, min(date(year, month, monthrange(year, month)[1]), today)))
    return months


def _per_day(db: Session, column, *where) -> dict[date, int]:
    # A literal, not a bound parameter: the same expression must appear in SELECT and GROUP BY.
    day = cast(func.timezone(literal_column("'UTC'"), column), Date)
    return dict(db.execute(select(day, func.count()).where(*where).group_by(day)).all())


def activity_points(db: Session, period: Period, today: date, *, uploads_where, assignments_where) -> list[ActivityPoint]:
    windows = spans(period, today)
    since = _midnight(windows[0][1])
    uploads = _per_day(db, Document.created_at, Document.created_at >= since, *uploads_where)
    assigned = _per_day(db, PolicyAssignment.created_at, PolicyAssignment.created_at >= since, *assignments_where)

    def within(counts: dict[date, int], start: date, end: date) -> int:
        return sum(count for day, count in counts.items() if start <= day <= end)

    return [ActivityPoint(label=label, start=start, end=end, uploads=within(uploads, start, end),
                          assignments=within(assigned, start, end)) for label, start, end in windows]


def _opened_since_assigned():
    """True for an assignment whose User opened one of its policy's documents after it was assigned."""
    return exists(
        select(AuditEvent.id)
        .join(Document, Document.id == AuditEvent.resource_id)
        .where(
            AuditEvent.actor_user_id == PolicyAssignment.user_id,
            AuditEvent.action.in_(OPENED),
            AuditEvent.resource_type == "document",
            Document.policy_id == PolicyAssignment.policy_id,
            AuditEvent.created_at >= PolicyAssignment.created_at,
        )
    )


def policy_reach(db: Session, organization_id: uuid.UUID, policies_where, *,
                 branch_id: uuid.UUID | None = None) -> list[PolicyReach]:
    """Every active policy in scope, most assigned first, with its live assignments (of the branch's Users only,
    for a branch) and how many of them were opened since."""
    stats = (
        select(
            PolicyAssignment.policy_id,
            func.count(),
            func.count().filter(_opened_since_assigned()),
            func.count(distinct(User.branch_id)),
        )
        .join(User, User.id == PolicyAssignment.user_id)
        .where(PolicyAssignment.organization_id == organization_id, PolicyAssignment.removed_at.is_(None),
               User.deleted_at.is_(None))
        .group_by(PolicyAssignment.policy_id)
    )
    if branch_id is not None:
        stats = stats.where(User.branch_id == branch_id)
    by_policy = {policy_id: (assigned, read, branches) for policy_id, assigned, read, branches in db.execute(stats)}
    policies = db.execute(
        select(Policy.id, Policy.name, Category.name)
        .outerjoin(Category, Category.id == Policy.category_id)
        .where(Policy.status == PolicyStatus.ACTIVE, *policies_where)
    ).all()
    rows = []
    for policy_id, name, category in policies:
        assigned, read, branches = by_policy.get(policy_id, (0, 0, 0))
        rows.append(PolicyReach(
            policy_id=policy_id, name=name, category=category, assigned=assigned, read=read,
            pending=assigned - read, read_percent=_percent(read, assigned) if assigned else None, branches=branches,
        ))
    rows.sort(key=lambda row: (-row.assigned, row.name.lower()))
    return rows


def recent_documents(db: Session, where, limit: int) -> list[DocumentRow]:
    rows = db.execute(
        select(Document, Category.name, PolicyVersion.version_label)
        .outerjoin(Policy, Policy.id == Document.policy_id)
        .outerjoin(Category, Category.id == func.coalesce(Document.category_id, Policy.category_id))
        .outerjoin(PolicyVersion, PolicyVersion.id == Document.policy_version_id)
        .where(Document.status.not_in(SET_ASIDE), *where)
        .order_by(Document.updated_at.desc())
        .limit(limit)
    ).all()
    return [
        DocumentRow(
            id=document.id, title=document.title or PurePosixPath(document.original_filename).stem,
            filename=document.original_filename,
            extension=PurePosixPath(document.original_filename).suffix.lstrip(".").upper(),
            category=category, version_label=version_label, size_bytes=document.size_bytes,
            status=document.status, updated_at=document.updated_at,
        )
        for document, category, version_label in rows
    ]


def ticket_summary(db: Session, *where) -> SupportSummary:
    sent = (Ticket.status != TicketStatus.AWAITING_CONFIRMATION, *where)
    by_status = dict(db.execute(select(Ticket.status, func.count()).where(*sent).group_by(Ticket.status)).all())
    opened = by_status.get(TicketStatus.OPEN, 0)
    working = sum(by_status.get(status, 0) for status in WORKING)
    resolved = sum(by_status.get(status, 0) for status in DONE)
    cancelled = by_status.get(TicketStatus.CANCELLED, 0)
    total = opened + working + resolved + cancelled
    overdue = db.scalar(select(func.count()).select_from(Ticket).where(
        Ticket.status.in_(list(ACTIVE_STATUSES)), Ticket.due_at <= datetime.now(UTC), *where)) or 0
    finished, in_time = db.execute(
        select(func.count(), func.count().filter(Ticket.resolved_at <= Ticket.due_at))
        .where(Ticket.status.in_(DONE), Ticket.resolved_at.is_not(None), Ticket.due_at.is_not(None), *where)
    ).one()
    categories: dict[str, list[int]] = {}
    for name, count, still_open in db.execute(
        select(Ticket.category, func.count(), func.count().filter(Ticket.status.in_(list(ACTIVE_STATUSES))))
        .where(*sent).group_by(Ticket.category)
    ):
        tally = categories.setdefault(name or "Uncategorised", [0, 0])
        tally[0] += count
        tally[1] += still_open
    busiest = sorted(categories.items(), key=lambda item: (-item[1][0], item[0].lower()))[:6]
    return SupportSummary(
        counts=TicketCounts(total=total, open=opened, in_progress=working, resolved=resolved, cancelled=cancelled,
                            overdue=overdue, sla_met_percent=_percent(in_time, finished) if finished else None),
        by_status=[
            StatusCount(status=status, count=count, percent=_percent(count, total))
            for status, count in (("resolved", resolved), ("in_progress", working), ("open", opened),
                                  ("cancelled", cancelled))
        ],
        categories=[CategoryCount(name=name, total=total, open=still_open)
                    for name, (total, still_open) in busiest],
    )


def _grouped(db: Session, key, *where, value=None) -> dict:
    return dict(db.execute(select(key, value if value is not None else func.count()).where(*where).group_by(key)).all())


# --- Master Admin ----------------------------------------------------------------------------------------------

def platform(db: Session, settings: Settings, *, period: Period, organizations_limit: int,
             audit_limit: int) -> PlatformDashboard:
    now = datetime.now(UTC)
    today = now.date()
    organizations = list(db.scalars(
        select(Organization).where(Organization.deleted_at.is_(None)).order_by(Organization.created_at.desc())
    ))
    nginx = NginxService(db, settings)
    state = {o.id: nginx.site(o).state for o in organizations if o.allowed_port is not None}

    def status(organization: Organization) -> str:
        if organization.status == OrganizationStatus.SUSPENDED:
            return "suspended"
        return "provisioning" if state.get(organization.id, "not_deployed") in NOT_SERVING else "active"

    statuses = [status(o) for o in organizations]
    month_start = _midnight(today.replace(day=1))

    live = User.deleted_at.is_(None)
    total, active, new = db.execute(select(
        func.count(), func.count().filter(User.is_active), func.count().filter(User.created_at >= now - timedelta(30))
    ).where(live)).one()
    admins, active_admins, admin_orgs = db.execute(select(
        func.count(), func.count().filter(User.is_active),
        func.count(distinct(User.organization_id)).filter(User.is_active),
    ).where(live, User.role == Role.ORG_ADMIN)).one()
    roles = _grouped(db, User.role, live)

    listed = organizations[:organizations_limit]
    ids = [o.id for o in listed]
    users_of = _grouped(db, User.organization_id, live, User.organization_id.in_(ids))
    admins_of = _grouped(db, User.organization_id, live, User.role == Role.ORG_ADMIN, User.organization_id.in_(ids))
    branches_of = _grouped(db, Branch.organization_id, Branch.deleted_at.is_(None), Branch.organization_id.in_(ids))
    storage_of = _grouped(db, Document.organization_id, Document.organization_id.in_(ids),
                          value=func.coalesce(func.sum(Document.size_bytes), 0))

    with_port = sorted((o for o in organizations if o.allowed_port is not None), key=lambda o: o.allowed_port)
    states = [state[o.id] for o in with_port]
    serving = [o.allowed_port for o in with_port if state[o.id] in SERVING]

    since = _midnight(spans(period, today)[0][1])
    activity: dict[uuid.UUID, dict[str, int]] = {}
    for organization_id, action, count in db.execute(
        select(AuditEvent.organization_id, AuditEvent.action, func.count())
        .where(AuditEvent.created_at >= since, AuditEvent.action.in_(tuple(APP_ACTIVITY)),
               AuditEvent.organization_id.is_not(None))
        .group_by(AuditEvent.organization_id, AuditEvent.action)
    ):
        activity.setdefault(organization_id, {})[APP_ACTIVITY[action]] = count
    busy = [
        OrganizationActivity(organization_id=o.id, name=o.name, port=o.allowed_port, **{
            field: activity.get(o.id, {}).get(field, 0) for field in APP_ACTIVITY.values()
        }, total=sum(activity.get(o.id, {}).values()))
        for o in organizations
    ]
    busy.sort(key=lambda row: (-row.total, row.name.lower()))

    events = db.scalars(select(AuditEvent).order_by(AuditEvent.created_at.desc()).limit(audit_limit)).all()
    return PlatformDashboard(
        period=period,
        organizations=OrganizationCounts(
            total=len(organizations), active=statuses.count("active"),
            suspended=statuses.count("suspended"), provisioning=statuses.count("provisioning"),
            new_this_month=sum(1 for o in organizations if o.created_at >= month_start),
        ),
        users=UserCounts(total=total, active=active, new_last_30_days=new,
                         growth_percent=round(new * 100 / (total - new), 1) if total - new else None),
        proxies=ProxyCounts(
            configured=nginx.conf_dir is not None, with_port=len(with_port), running=states.count("deployed"),
            outdated=states.count("outdated"), disabled=states.count("disabled"),
            not_deployed=states.count("not_deployed"), first_port=min(serving, default=None),
            last_port=max(serving, default=None),
        ),
        org_admins=AdminCounts(total=admins, active=active_admins, organizations=admin_orgs),
        support=ticket_summary(db, Ticket.level == TicketLevel.PLATFORM),
        recent_organizations=[
            OrganizationRow(
                id=o.id, name=o.name, slug=o.slug, contact_email=o.contact_email, port=o.allowed_port,
                status=status(o), nginx_state=state.get(o.id), admins=admins_of.get(o.id, 0),
                branches=branches_of.get(o.id, 0), users=users_of.get(o.id, 0),
                storage_bytes=int(storage_of.get(o.id, 0)), created_at=o.created_at,
            )
            for o in listed
        ],
        ports=[PortRow(port=o.allowed_port, organization_id=o.id, organization_name=o.name, state=state[o.id])
               for o in with_port],
        roles=[RoleCount(role=role, count=count, percent=_percent(count, total))
               for role, count in sorted(roles.items(), key=lambda item: -item[1])],
        activity_by_organization=busy,
        audit=describe(db, events),
    )


# --- Org Admin -------------------------------------------------------------------------------------------------

def organization(db: Session, settings: Settings, principal: Principal, *, period: Period, rows: int,
                 activity: int) -> OrganizationDashboard:
    now = datetime.now(UTC)
    org_id = principal.organization_id
    org = db.get(Organization, org_id)
    live = (User.deleted_at.is_(None), User.organization_id == org_id)
    total, active, new = db.execute(select(
        func.count(), func.count().filter(User.is_active), func.count().filter(User.created_at >= now - timedelta(90))
    ).where(*live)).one()

    branches = list(db.scalars(
        select(Branch).where(Branch.organization_id == org_id, Branch.deleted_at.is_(None))
        .order_by(Branch.created_at.desc())
    ))
    working = (*live, User.is_active)
    users_in = _grouped(db, User.branch_id, *working, User.role == Role.DEPARTMENT_USER)
    managers_in = _grouped(db, User.branch_id, *working, User.role == Role.BRANCH_MANAGER)
    managers = {}
    for manager in db.scalars(select(User).where(*working, User.role == Role.BRANCH_MANAGER)
                              .order_by(User.created_at)):
        managers.setdefault(manager.branch_id, manager)

    def branch_status(branch: Branch) -> str:
        if not branch.is_active:
            return "inactive"
        return "active" if managers_in.get(branch.id) else "needs_attention"

    documents = _grouped(db, Document.status, Document.organization_id == org_id)
    library = sum(count for status, count in documents.items() if status not in SET_ASIDE)
    events = db.scalars(select(AuditEvent).where(AuditEvent.organization_id == org_id)
                        .order_by(AuditEvent.created_at.desc()).limit(activity)).all()
    site = NginxService(db, settings).site(org) if org.allowed_port is not None else None
    return OrganizationDashboard(
        period=period,
        users=OrgUserCounts(total=total, active=active, new_last_90_days=new, by_role=_grouped(db, User.role, *live)),
        branches=OrgBranchCounts(
            total=len(branches), active=sum(1 for b in branches if b.is_active),
            with_manager=sum(1 for b in branches if managers_in.get(b.id)),
        ),
        documents=OrgDocumentCounts(
            total=library, ready=documents.get(DocumentStatus.READY, 0),
            processing=sum(documents.get(s, 0) for s in PROCESSING),
            needs_attention=sum(documents.get(s, 0) for s in NEEDS_ATTENTION),
        ),
        branch_users=[
            BranchLoad(branch_id=b.id, name=b.name, code=b.code, users=users_in.get(b.id, 0),
                       managers=managers_in.get(b.id, 0), is_active=b.is_active, status=branch_status(b))
            for b in sorted(branches, key=lambda b: b.name.lower())
        ],
        top_policies=policy_reach(db, org_id, (Policy.organization_id == org_id,))[:rows],
        document_activity=activity_points(
            db, period, now.date(), uploads_where=(Document.organization_id == org_id,),
            assignments_where=(PolicyAssignment.organization_id == org_id, PolicyAssignment.removed_at.is_(None)),
        ),
        latest_branches=[
            BranchRow(
                branch_id=b.id, name=b.name, code=b.code,
                location=", ".join(part for part in (b.street_address, b.state) if part) or None,
                manager_name=managers[b.id].full_name if b.id in managers else None,
                manager_email=managers[b.id].email if b.id in managers else None,
                is_active=b.is_active, created_at=b.created_at,
            )
            for b in branches[:rows]
        ],
        recent_documents=recent_documents(db, (Document.organization_id == org_id,), rows),
        activity=describe(db, events),
        portal=PortalStatus(port=org.allowed_port, state=site.state if site else None),
    )


# --- Branch Manager --------------------------------------------------------------------------------------------

def branch(db: Session, principal: Principal, *, period: Period, rows: int, activity: int) -> BranchDashboard:
    now = datetime.now(UTC)
    org_id, branch_id = principal.organization_id, principal.branch_id
    here = db.get(Branch, branch_id)
    staff_where = (User.deleted_at.is_(None), User.branch_id == branch_id, User.role == Role.DEPARTMENT_USER,
                   User.is_active)
    branch_users = select(User.id).where(User.deleted_at.is_(None), User.branch_id == branch_id)
    visible_docs = (document_visible(principal), Document.status.not_in(SET_ASIDE))

    staff = db.scalar(select(func.count()).select_from(User).where(*staff_where)) or 0
    reach = policy_reach(db, org_id, (policy_visible(principal),), branch_id=branch_id)
    assigned = sum(row.assigned for row in reach)
    read = sum(row.read for row in reach)
    branch_tickets = (Ticket.branch_id == branch_id, Ticket.status != TicketStatus.AWAITING_CONFIRMATION)

    departments = db.execute(
        select(User.department_id, Department.name, func.count())
        .outerjoin(Department, Department.id == User.department_id)
        .where(*staff_where).group_by(User.department_id, Department.name)
        .order_by(func.count().desc(), Department.name)
    ).all()

    return BranchDashboard(
        period=period, branch_id=branch_id, branch_name=here.name,
        counts=BranchCounts(
            documents=db.scalar(select(func.count()).select_from(Document).where(*visible_docs)) or 0,
            staff=staff, assignments=assigned,
            open_tickets=db.scalar(select(func.count()).select_from(Ticket).where(
                Ticket.branch_id == branch_id, Ticket.status.in_(list(ACTIVE_STATUSES)))) or 0,
        ),
        departments=[DepartmentCount(department_id=department_id, name=name or "No department", count=count,
                                     percent=_percent(count, staff)) for department_id, name, count in departments],
        document_activity=activity_points(
            db, period, now.date(), uploads_where=(document_visible(principal),),
            assignments_where=(PolicyAssignment.user_id.in_(branch_users), PolicyAssignment.removed_at.is_(None)),
        ),
        assignment_status=AssignmentStatus(
            read=read, pending=assigned - read, unassigned_policies=sum(1 for row in reach if not row.assigned),
        ),
        recent_documents=recent_documents(db, (document_visible(principal),), rows),
        top_policies=reach[:rows],
        activity=_branch_activity(db, principal, branch_users, branch_tickets, activity),
    )


def _branch_activity(db: Session, principal: Principal, branch_users, branch_tickets, limit: int) -> list[ActivityItem]:
    """What happened in the branch lately, from its own records (a Branch Manager does not read the audit log)."""
    items: list[ActivityItem] = []
    for ticket in db.scalars(select(Ticket).where(*branch_tickets).order_by(Ticket.updated_at.desc()).limit(limit)):
        title = ("Support ticket resolved" if ticket.status in (TicketStatus.RESOLVED, TicketStatus.CLOSED)
                 else "Support ticket withdrawn" if ticket.status == TicketStatus.CANCELLED
                 else "Support ticket raised" if ticket.status == TicketStatus.OPEN else "Support ticket updated")
        items.append(ActivityItem(kind="ticket", title=title, detail=f"#{ticket.number} {ticket.subject}",
                                  status=ticket.status, at=ticket.updated_at, resource_type="ticket",
                                  resource_id=ticket.id))
    for document in db.scalars(select(Document).where(document_visible(principal), Document.status.not_in(SET_ASIDE))
                               .order_by(Document.updated_at.desc()).limit(limit)):
        title = {DocumentStatus.READY: "Document ready", DocumentStatus.FAILED: "Document failed",
                 DocumentStatus.AWAITING_CONFIRMATION: "Document awaiting confirmation"}.get(
            document.status, "Document uploaded")
        items.append(ActivityItem(kind="document", title=title,
                                  detail=document.title or document.original_filename, status=document.status,
                                  at=document.updated_at, resource_type="document", resource_id=document.id))
    for assignment, policy_name, user_name in db.execute(
        select(PolicyAssignment, Policy.name, User.full_name)
        .join(Policy, Policy.id == PolicyAssignment.policy_id).join(User, User.id == PolicyAssignment.user_id)
        .where(PolicyAssignment.user_id.in_(branch_users), PolicyAssignment.removed_at.is_(None))
        .order_by(PolicyAssignment.created_at.desc()).limit(limit)
    ):
        items.append(ActivityItem(kind="assignment", title="Policy assigned", detail=f"{policy_name} to {user_name}",
                                  status="assigned", at=assignment.created_at, resource_type="policy",
                                  resource_id=assignment.policy_id))
    for user in db.scalars(select(User).where(User.id.in_(branch_users), User.role == Role.DEPARTMENT_USER)
                           .order_by(User.created_at.desc()).limit(limit)):
        items.append(ActivityItem(kind="staff", title="Staff member added", detail=user.full_name,
                                  status="active" if user.is_active else "inactive", at=user.created_at,
                                  resource_type="user", resource_id=user.id))
    items.sort(key=lambda item: item.at, reverse=True)
    return items[:limit]
