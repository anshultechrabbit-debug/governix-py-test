"""Support Center API (/support): tickets, routing confirmation, forwarding, staff and categories.

Every endpoint needs a signed-in caller (cookie + X-CSRF-Token, or Bearer). What each caller may see and
do is decided by the service from their role and support duty, so a direct call gets exactly what the
page would offer: 404 for a ticket they may not see, 403 for an action they may not take.
"""

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Query, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.database import get_db
from app.core.dependencies import get_app_settings, get_storage
from app.core.exceptions import NotFoundError
from app.core.pagination import Page, PageParams, page_params
from app.core.responses import ApiErrorResponse, ApiResponse, ok
from app.infrastructure.storage.base import Storage
from app.modules.auth.dependencies import require
from app.modules.auth.permissions import Permission, Principal
from app.modules.tickets.model import (
    CategoryRoute, StaffAvailability, StaffRole, TicketLevel, TicketPriority, TicketStatus, TicketTeam,
)
from app.modules.tickets.service import TicketService, TicketView, TimelineEntry, reference
from app.modules.tickets.staff import CategoryService, StaffService, StaffView
from app.workers.runtime import get_runtime

router = APIRouter(prefix="/support", tags=["Support Center"])
staff_router = APIRouter(prefix="/support/staff", tags=["Support Staff"])


def _errors(*codes: int) -> dict[int | str, dict[str, Any]]:
    text = {401: "Not signed in, or the session ended.", 403: "Your role or duty does not allow this.",
            404: "Not found, or not visible to you.", 409: "The ticket's state does not allow this now.",
            413: "The file is too large.", 422: "The request is invalid."}
    return {code: {"model": ApiErrorResponse, "description": text[code]} for code in (401, *codes)}


Supporter = Annotated[Principal, Depends(require(Permission.SUPPORT))]
StaffManager = Annotated[Principal, Depends(require(Permission.SUPPORT_STAFF_MANAGE))]


def get_service(db: Annotated[Session, Depends(get_db)], storage: Annotated[Storage, Depends(get_storage)],
                settings: Annotated[Settings, Depends(get_app_settings)]) -> TicketService:
    try:
        llm = get_runtime().llm
    except RuntimeError:  # no runtime configured: categories alone recommend
        llm = None
    return TicketService(db, storage, settings, llm)


Service = Annotated[TicketService, Depends(get_service)]


# --- schemas ---------------------------------------------------------------------------------------------------

class SupportCategoryRead(BaseModel):
    id: str
    name: str
    route: CategoryRoute
    description: str | None


class TicketCreate(BaseModel):
    category_id: str = Field(min_length=1, max_length=100, description="A category id from GET /support/categories.")
    subject: str = Field(min_length=3, max_length=300)
    description: str = Field(min_length=10, max_length=10_000)
    # None: the AI recommends one (medium without it). The support side may change it later.
    priority: TicketPriority | None = None
    policy_id: uuid.UUID | None = Field(default=None, description="A policy the ticket is about (one you can read).")


class RoutingOption(BaseModel):
    level: TicketLevel
    queue: str


class Routing(BaseModel):
    """The AI recommendation shown before the creator confirms. team None: the creator must choose."""

    team: TicketTeam | None
    priority: TicketPriority
    confidence: float
    reason: str
    source: Literal["ai", "text", "category", "none"]
    redirect: bool = Field(description="A question about what a policy says: the AI Assistant answers it.")
    issue_type: str | None
    skills: list[str]
    needs_choice: bool
    priority_chosen: bool = False
    options: dict[str, RoutingOption] = {}
    suggested_assignee: dict[str, str] | None = None


class RoutingConfirm(BaseModel):
    team: TicketTeam = Field(description="The recommended team, or the one the creator switched to.")


class TicketRead(BaseModel):
    id: uuid.UUID
    number: int
    reference: str
    organization_id: uuid.UUID
    organization_name: str | None
    branch_id: uuid.UUID | None
    branch_name: str | None
    kind: str
    level: TicketLevel
    team: TicketTeam
    queue_name: str
    category: str | None
    issue_type: str | None
    policy_id: uuid.UUID | None
    policy_name: str | None
    subject: str
    description: str
    priority: TicketPriority
    status: TicketStatus
    created_by_id: uuid.UUID | None
    created_by_name: str | None
    created_by_role: str | None
    assigned_to_id: uuid.UUID | None
    assigned_to_name: str | None
    message_count: int
    can_handle: bool
    last_reply_body: str | None
    last_reply_by: str | None
    last_reply_at: datetime | None
    created_at: datetime
    updated_at: datetime
    confirmed_at: datetime | None
    first_response_at: datetime | None
    response_due_at: datetime | None
    due_at: datetime | None
    overdue: bool
    response_overdue: bool
    resolution_note: str | None
    reopen_count: int
    resolved_at: datetime | None
    closed_at: datetime | None
    cancelled_at: datetime | None


class TimelineRead(BaseModel):
    id: uuid.UUID
    kind: Literal["reply", "note", "status", "forward", "assign"]
    author_id: uuid.UUID | None
    author_name: str | None
    author_role: str | None
    body: str | None
    status_change: TicketStatus | None
    data: dict[str, Any]
    created_at: datetime


class AttachmentRead(BaseModel):
    id: uuid.UUID
    filename: str
    content_type: str
    size_bytes: int
    uploaded_by_id: uuid.UUID | None
    created_at: datetime


class Destination(BaseModel):
    key: Literal["branch", "head_office", "technical"]
    level: TicketLevel
    team: TicketTeam
    label: str


class TicketActions(BaseModel):
    """What the caller may do now (the endpoints check the same rules)."""

    confirm: bool
    reply: bool
    note: bool
    statuses: list[TicketStatus]
    cancel: bool
    forward: list[Destination]
    escalate: bool
    escalate_to: str | None
    take: bool
    assign: bool
    priority: bool
    attach: bool


class TicketDetail(TicketRead):
    routing: Routing | None
    timeline: list[TimelineRead]
    attachments: list[AttachmentRead]
    actions: TicketActions


class Reply(BaseModel):
    body: str = Field(min_length=1, max_length=10_000)
    internal: bool = Field(default=False, description="An internal note: never shown to the person who raised it.")


class StatusChange(BaseModel):
    status: TicketStatus
    note: str | None = Field(default=None, max_length=4000, description="Required when resolving (the resolution note).")


class PriorityChange(BaseModel):
    priority: TicketPriority
    note: str | None = Field(default=None, max_length=2000)


class AssignTicket(BaseModel):
    user_id: uuid.UUID | None = Field(default=None, description="A staff member of the ticket's queue, yourself, or null.")


class Forward(BaseModel):
    to: Literal["branch", "head_office", "technical", "escalate"]
    reason: str = Field(min_length=3, max_length=2000)
    confirmed: bool = Field(description="Must be true: the person forwarding confirms the destination.")
    user_id: uuid.UUID | None = Field(default=None, description="A staff member of the destination queue (else by workload).")


class Cancel(BaseModel):
    note: str | None = Field(default=None, max_length=2000, description="Required when a manager cancels.")


class QueueAssignee(BaseModel):
    user_id: uuid.UUID
    name: str
    availability: StaffAvailability
    staff_role: StaffRole
    skills: list[str]
    open_tickets: int
    capacity: int
    load: float


class AssigneeLoad(BaseModel):
    user_id: uuid.UUID
    name: str
    open_tickets: int
    capacity: int | None
    load: float | None


class SupportDashboard(BaseModel):
    by_status: dict[str, int]
    by_team: dict[str, int]
    by_priority: dict[str, int] = Field(description="Active tickets only.")
    by_level: dict[str, int]
    by_assignee: list[AssigneeLoad] = Field(description="Active tickets per assignee, with their capacity.")
    active: int
    overdue: int
    response_overdue: int
    unassigned: int


Box = Literal["all", "mine", "queue", "assigned", "unassigned"]


def _read(view: TicketView) -> dict[str, Any]:
    t = view.ticket
    return dict(
        id=t.id, number=t.number, reference=reference(t), organization_id=t.organization_id,
        organization_name=view.organization_name, branch_id=t.branch_id, branch_name=view.branch_name, kind=t.kind,
        level=t.level, team=t.team, queue_name=view.queue_name, category=t.category, issue_type=t.issue_type,
        policy_id=t.policy_id, policy_name=view.policy_name, subject=t.subject, description=t.description,
        priority=t.priority, status=t.status, created_by_id=t.created_by_id, created_by_name=view.created_by_name,
        created_by_role=view.created_by_role, assigned_to_id=t.assigned_to_id, assigned_to_name=view.assigned_to_name,
        message_count=view.message_count, can_handle=view.can_handle,
        last_reply_body=reply.body if (reply := view.last_reply) else None,
        last_reply_by=reply.author_name if reply else None, last_reply_at=reply.at if reply else None,
        created_at=t.created_at, updated_at=t.updated_at, confirmed_at=t.confirmed_at,
        first_response_at=t.first_response_at, response_due_at=t.response_due_at, due_at=t.due_at,
        overdue=view.overdue, response_overdue=view.response_overdue, resolution_note=t.resolution_note,
        reopen_count=t.reopen_count, resolved_at=t.resolved_at, closed_at=t.closed_at, cancelled_at=t.cancelled_at,
    )


def _timeline(entry: TimelineEntry) -> TimelineRead:
    m = entry.message
    return TimelineRead(id=m.id, kind=m.kind, author_id=m.author_id, author_name=entry.author_name,
                        author_role=entry.author_role, body=m.body, status_change=m.status_change, data=entry.data,
                        created_at=m.created_at)


def _detail(service: TicketService, principal: Principal, ticket_id: uuid.UUID) -> TicketDetail:
    view, timeline, attachments = service.thread(principal, ticket_id)
    ticket = view.ticket
    # The recommendation is the creator's decision aid; the support side sees how it was routed in the timeline.
    routing = ticket.routing if ticket.routing and ticket.created_by_id == principal.user_id else None
    return TicketDetail(
        **_read(view), routing=Routing(**routing) if routing else None,
        timeline=[_timeline(e) for e in timeline],
        attachments=[AttachmentRead(id=a.id, filename=a.filename, content_type=a.content_type, size_bytes=a.size_bytes,
                                    uploaded_by_id=a.uploaded_by_id, created_at=a.created_at) for a in attachments],
        actions=TicketActions(**view.actions),
    )


# --- tickets ---------------------------------------------------------------------------------------------------

@router.get("/categories", response_model=ApiResponse[list[SupportCategoryRead]], responses=_errors(403))
def list_categories(principal: Supporter, service: Service):
    """The ticket categories of the caller's organization (its own, or the defaults)."""
    return ok([SupportCategoryRead(id=c.id, name=c.name, route=c.route, description=c.description)
               for c in service.categories(principal)])


@router.post("/tickets", response_model=ApiResponse[TicketDetail], status_code=201, responses=_errors(403, 422))
def create_ticket(body: TicketCreate, principal: Supporter, service: Service):
    """Raise a ticket (Users, Branch Managers, Organization Admins).

    The AI reads it and recommends a team, priority and issue type (`routing`). The ticket waits in
    `awaiting_confirmation`, seen by nobody else, until the creator confirms or switches the team with
    POST /support/tickets/{id}/confirm. Attachments may be added before confirming."""
    ticket = service.create(principal, category_id=body.category_id, subject=body.subject,
                            description=body.description, priority=body.priority, policy_id=body.policy_id)
    return ok(_detail(service, principal, ticket.id))


@router.post("/tickets/{ticket_id}/confirm", response_model=ApiResponse[TicketDetail], responses=_errors(403, 404, 409))
def confirm_routing(ticket_id: uuid.UUID, body: RoutingConfirm, principal: Supporter, service: Service):
    """The creator confirms the destination team (the recommended one, or another). The ticket opens in that
    team's queue, its SLA starts, and it is assigned by workload. Repeating the call changes nothing."""
    service.confirm(principal, ticket_id, body.team)
    return ok(_detail(service, principal, ticket_id))


@router.get("/tickets", response_model=ApiResponse[Page[TicketRead]], responses=_errors(422))
def list_tickets(
    principal: Supporter, service: Service, page: Annotated[PageParams, Depends(page_params)],
    box: Box = "all",
    status: Annotated[str | None, Query(pattern="^(active|done|awaiting_confirmation|open|in_progress|waiting_for_user"
                                                "|waiting_for_team|resolved|closed|cancelled)$")] = None,
    team: TicketTeam | None = None,
    priority: TicketPriority | None = None,
    overdue: bool = False,
    assignee: Annotated[str | None, Query(description="me, none, or a user id")] = None,
    search: Annotated[str | None, Query(max_length=200, description="Subject, details, category, creator, policy, SUP-number")] = None,
):
    """box: mine (raised by me), queue (the queues I work or manage), assigned (to me), unassigned (in my
    queues), all (everything I may see, including what I oversee)."""
    views, total = service.find(principal, box=box, status=status, team=team, priority=priority, overdue=overdue,
                                assignee=assignee, search=search, limit=page.limit, offset=page.offset)
    return ok(Page(items=[TicketRead(**_read(v)) for v in views], total=total, **page.model_dump()))


@router.get("/tickets/dashboard", response_model=ApiResponse[SupportDashboard])
def dashboard(principal: Supporter, service: Service, box: Box = "queue"):
    """Counts for one box: by status, team, priority, level and assignee; overdue and unassigned."""
    return ok(SupportDashboard(**service.dashboard(principal, box=box)))


@router.get("/tickets/{ticket_id}", response_model=ApiResponse[TicketDetail], responses=_errors(404))
def get_ticket(ticket_id: uuid.UUID, principal: Supporter, service: Service):
    """The ticket with its timeline (replies, the internal notes the caller may read, status changes, forwards,
    assignments), attachments and the actions the caller may take."""
    return ok(_detail(service, principal, ticket_id))


@router.post("/tickets/{ticket_id}/messages", response_model=ApiResponse[TicketDetail], status_code=201,
             responses=_errors(403, 404, 409))
def reply(ticket_id: uuid.UUID, body: Reply, principal: Supporter, service: Service):
    """A reply (visible to the creator), or with internal=true a note for the handling team only."""
    service.reply(principal, ticket_id, body.body, internal=body.internal)
    return ok(_detail(service, principal, ticket_id))


@router.post("/tickets/{ticket_id}/status", response_model=ApiResponse[TicketDetail], responses=_errors(403, 404, 422))
def change_status(ticket_id: uuid.UUID, body: StatusChange, principal: Supporter, service: Service):
    """Staff: open -> in_progress / waiting_for_user / waiting_for_team -> resolved (with a note), resolved ->
    in_progress. Creator: resolved -> closed (accept) or open (reopen), closed -> open."""
    service.change_status(principal, ticket_id, body.status, body.note)
    return ok(_detail(service, principal, ticket_id))


@router.post("/tickets/{ticket_id}/priority", response_model=ApiResponse[TicketDetail], responses=_errors(403, 404))
def change_priority(ticket_id: uuid.UUID, body: PriorityChange, principal: Supporter, service: Service):
    """The handling team changes the priority; the SLA due times follow it."""
    service.set_priority(principal, ticket_id, body.priority, body.note)
    return ok(_detail(service, principal, ticket_id))


@router.post("/tickets/{ticket_id}/assign", response_model=ApiResponse[TicketDetail], responses=_errors(403, 404, 409, 422))
def assign(ticket_id: uuid.UUID, body: AssignTicket, principal: Supporter, service: Service):
    """Take the ticket (your own id: a member of its queue), or assign / reassign / unassign it (the queue's
    manager or lead). Every change is recorded and both people are told."""
    service.assign(principal, ticket_id, body.user_id)
    return ok(_detail(service, principal, ticket_id))


@router.get("/tickets/{ticket_id}/assignees", response_model=ApiResponse[list[QueueAssignee]], responses=_errors(403, 404, 422))
def assignees(ticket_id: uuid.UUID, principal: Supporter, service: Service,
              to: Literal["branch", "head_office", "technical"] | None = None):
    """The staff of the ticket's queue (or of a forwarding destination) with their availability and workload."""
    return ok([QueueAssignee(**row) for row in service.assignees(principal, ticket_id, to)])


class ForwardResult(BaseModel):
    reference: str
    level: TicketLevel
    team: TicketTeam
    queue_name: str
    assigned_to_name: str | None
    # The ticket as the caller now sees it; null when it left every queue they work or oversee.
    ticket: TicketDetail | None


@router.post("/tickets/{ticket_id}/forward", response_model=ApiResponse[ForwardResult], responses=_errors(403, 404, 409, 422))
def forward(ticket_id: uuid.UUID, body: Forward, principal: Supporter, service: Service):
    """Forward to Branch Support, Head Office Support or Technical Support (or `escalate`: one level up), with a
    reason and confirmed=true. The ticket keeps its history and SLA; it is assigned in the new queue by workload
    (or to `user_id`). The handling team forwards; the creator may escalate an overdue ticket. `ticket` is null
    when the caller can no longer see it (it left their queue)."""
    ticket = service.forward(principal, ticket_id, to=body.to, reason=body.reason, confirmed=body.confirmed,
                             user_id=body.user_id)
    summary = service.forwarded(ticket)
    try:
        detail = _detail(service, principal, ticket_id)
    except NotFoundError:
        detail = None
    return ok(ForwardResult(**summary, ticket=detail))


@router.post("/tickets/{ticket_id}/cancel", response_model=ApiResponse[TicketDetail], responses=_errors(403, 404, 409, 422))
def cancel(ticket_id: uuid.UUID, body: Cancel, principal: Supporter, service: Service):
    """The creator withdraws a draft or open ticket; a queue manager cancels one with a note."""
    service.cancel(principal, ticket_id, body.note)
    return ok(_detail(service, principal, ticket_id))


@router.post("/tickets/{ticket_id}/attachments", response_model=ApiResponse[TicketDetail], status_code=201,
             responses=_errors(404, 409, 413, 422))
def attach(ticket_id: uuid.UUID, principal: Supporter, service: Service, file: Annotated[UploadFile, File()]):
    """PDF, image, text, CSV, Word or Excel, up to 10 MB."""
    service.attach(principal, ticket_id, file=file.file, filename=file.filename or "attachment")
    return ok(_detail(service, principal, ticket_id))


@router.get("/tickets/{ticket_id}/attachments/{attachment_id}", responses=_errors(404))
def download_attachment(ticket_id: uuid.UUID, attachment_id: uuid.UUID, principal: Supporter, service: Service):
    attachment = service.attachment(principal, ticket_id, attachment_id)
    with service.storage.open(attachment.storage_key) as handle:
        content = handle.read()
    return Response(
        content, media_type=attachment.content_type,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(attachment.filename)}",
                 "X-Content-Type-Options": "nosniff"},
    )


# --- staff -------------------------------------------------------------------------------------------------------

class NewAgent(BaseModel):
    full_name: str = Field(min_length=2, max_length=200)
    email: EmailStr
    phone: str | None = Field(default=None, max_length=50)


class StaffCreate(BaseModel):
    level: TicketLevel
    user_id: uuid.UUID | None = Field(default=None, description="An existing account (see /support/staff/candidates).")
    new_account: NewAgent | None = Field(default=None, description="Platform only: create a support agent account.")
    branch_id: uuid.UUID | None = Field(default=None, description="Branch level: the branch (a Branch Manager's own).")
    staff_role: StaffRole = StaffRole.AGENT
    skills: list[str] = Field(default_factory=list, max_length=30)
    capacity: int = Field(default=10, ge=1, le=500)
    availability: StaffAvailability = StaffAvailability.AVAILABLE
    serves: list[uuid.UUID] | None = Field(default=None, description="Platform only: banks served (none: all).")


class StaffUpdate(BaseModel):
    staff_role: StaffRole | None = None
    skills: list[str] | None = Field(default=None, max_length=30)
    capacity: int | None = Field(default=None, ge=1, le=500)
    availability: StaffAvailability | None = None
    is_active: bool | None = None
    serves: list[uuid.UUID] | None = None


class MyAvailability(BaseModel):
    availability: StaffAvailability


class Served(BaseModel):
    id: uuid.UUID
    name: str


class StaffRead(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    full_name: str
    email: str
    role: str
    level: TicketLevel
    organization_id: uuid.UUID | None
    organization_name: str | None
    branch_id: uuid.UUID | None
    branch_name: str | None
    staff_role: StaffRole
    skills: list[str]
    capacity: int
    availability: StaffAvailability
    is_active: bool
    account_active: bool
    open_tickets: int
    load: float
    serves: list[Served]
    last_assigned_at: datetime | None
    created_at: datetime


class StaffCreated(StaffRead):
    temporary_password: str | None = Field(description="Only for a new support agent account; shown once.")


class StaffCandidate(BaseModel):
    user_id: uuid.UUID
    full_name: str
    email: str
    role: str
    branch_name: str | None


class ReassignOpen(BaseModel):
    note: str | None = Field(default=None, max_length=500)


class Reassigned(BaseModel):
    moved: int


def _staff(view: StaffView) -> dict[str, Any]:
    s, u = view.staff, view.user
    return dict(
        id=s.id, user_id=u.id, full_name=u.full_name, email=u.email, role=u.role, level=s.level,
        organization_id=s.organization_id, organization_name=view.organization_name, branch_id=s.branch_id,
        branch_name=view.branch_name, staff_role=s.staff_role, skills=s.skills or [], capacity=s.capacity,
        availability=s.availability, is_active=s.is_active, account_active=u.is_active,
        open_tickets=view.open_tickets, load=round(view.open_tickets / s.capacity, 3),
        serves=[Served(id=i, name=n) for i, n in view.serves], last_assigned_at=s.last_assigned_at, created_at=s.created_at,
    )


def get_staff_service(db: Annotated[Session, Depends(get_db)]) -> StaffService:
    return StaffService(db)


def get_category_service(db: Annotated[Session, Depends(get_db)]) -> CategoryService:
    return CategoryService(db)


StaffServiceDep = Annotated[StaffService, Depends(get_staff_service)]
CategoryServiceDep = Annotated[CategoryService, Depends(get_category_service)]


@staff_router.get("", response_model=ApiResponse[list[StaffRead]], responses=_errors(403))
def list_staff(principal: StaffManager, staff: StaffServiceDep, level: TicketLevel | None = None,
               branch_id: uuid.UUID | None = None, search: Annotated[str | None, Query(max_length=200)] = None):
    """The support staff the caller manages: Master Admin, platform; Organization Admin, their bank's Head Office
    and branches; Branch Manager, their branch."""
    return ok([StaffRead(**_staff(v)) for v in staff.list(principal, level=level, branch_id=branch_id, search=search)])


@staff_router.get("/candidates", response_model=ApiResponse[list[StaffCandidate]], responses=_errors(403, 422))
def staff_candidates(principal: StaffManager, staff: StaffServiceDep, level: TicketLevel,
                     branch_id: uuid.UUID | None = None, search: Annotated[str | None, Query(max_length=200)] = None):
    """Accounts the caller may put on duty at that level (not on duty already)."""
    rows = staff.candidates(principal, level=level, branch_id=branch_id, search=search)
    return ok([StaffCandidate(user_id=u.id, full_name=u.full_name, email=u.email, role=u.role, branch_name=b) for u, b in rows])


@staff_router.post("", response_model=ApiResponse[StaffCreated], status_code=201, responses=_errors(403, 409, 422))
def add_staff(body: StaffCreate, principal: StaffManager, staff: StaffServiceDep):
    """Put someone on support duty, with their skills, capacity and availability. The Master Admin may create
    a new support agent account instead (new_account); its temporary password is returned once."""
    created, password = staff.create(
        principal, level=body.level, user_id=body.user_id,
        new_account=body.new_account.model_dump() if body.new_account else None, branch_id=body.branch_id,
        staff_role=body.staff_role, skills=body.skills, capacity=body.capacity, availability=body.availability,
        serves=body.serves,
    )
    return ok(StaffCreated(**_staff(staff.view(principal, created.id)), temporary_password=password))


@staff_router.get("/me", response_model=ApiResponse[StaffRead | None])
def my_duty(principal: Supporter, staff: StaffServiceDep):
    """The caller's own support duty, or null."""
    view = staff.mine(principal)
    return ok(StaffRead(**_staff(view)) if view else None)


@staff_router.patch("/me", response_model=ApiResponse[StaffRead], responses=_errors(404))
def set_my_availability(body: MyAvailability, principal: Supporter, staff: StaffServiceDep):
    """Staff set their own availability. Away, busy or offline: no new tickets; their own stay with them."""
    staff.set_my_availability(principal, body.availability)
    return ok(StaffRead(**_staff(staff.mine(principal))))


@staff_router.patch("/{staff_id}", response_model=ApiResponse[StaffRead], responses=_errors(403, 404, 422))
def update_staff(staff_id: uuid.UUID, body: StaffUpdate, principal: StaffManager, staff: StaffServiceDep):
    """Change role (agent/lead), skills, capacity, availability, active, or the banks a platform member serves.
    Their open tickets stay with them (see /reassign)."""
    staff.update(principal, staff_id, body.model_dump(exclude_unset=True))
    return ok(StaffRead(**_staff(staff.view(principal, staff_id))))


@staff_router.delete("/{staff_id}", response_model=ApiResponse[dict], responses=_errors(403, 404, 409))
def remove_staff(staff_id: uuid.UUID, principal: StaffManager, staff: StaffServiceDep):
    """Take someone off support duty (their account stays). Refused while they hold open tickets."""
    staff.remove(principal, staff_id)
    return ok({"removed": True})


@staff_router.post("/{staff_id}/reassign", response_model=ApiResponse[Reassigned], responses=_errors(403, 404))
def reassign_open(staff_id: uuid.UUID, body: ReassignOpen, principal: StaffManager, staff: StaffServiceDep, service: Service):
    """Move this person's open tickets to others by workload, each one recorded and notified."""
    profile = staff.profile_of(principal, staff_id)
    return ok(Reassigned(moved=service.reassign_open(principal, profile, body.note)))


# --- categories (Organization Admin) -----------------------------------------------------------------------------

class SupportCategoryCreate(BaseModel):
    name: str = Field(min_length=2, max_length=100)
    route: CategoryRoute = CategoryRoute.AUTO
    description: str | None = Field(default=None, max_length=300)


class SupportCategoryUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=100)
    route: CategoryRoute | None = None
    description: str | None = Field(default=None, max_length=300)
    is_active: bool | None = None
    sort_order: int | None = Field(default=None, ge=0, le=100_000)


class SupportCategoryAdmin(SupportCategoryRead):
    is_active: bool


@router.get("/categories/manage", response_model=ApiResponse[list[SupportCategoryAdmin]], responses=_errors(403))
def manage_categories(principal: StaffManager, categories: CategoryServiceDep):
    """Organization Admin: every category, active or not."""
    return ok([SupportCategoryAdmin(id=c.id, name=c.name, route=c.route, description=c.description, is_active=active)
               for c, active in categories.list(principal)])


@router.post("/categories", response_model=ApiResponse[SupportCategoryRead], status_code=201, responses=_errors(403, 409, 422))
def create_category(body: SupportCategoryCreate, principal: StaffManager, categories: CategoryServiceDep):
    """Organization Admin: add a category. `route` is where it points before the AI reads a ticket."""
    category = categories.create(principal, name=body.name, route=body.route, description=body.description)
    return ok(SupportCategoryRead(id=str(category.id), name=category.name, route=category.route, description=category.description))


@router.patch("/categories/{category_id}", response_model=ApiResponse[SupportCategoryRead], responses=_errors(403, 404, 409, 422))
def update_category(category_id: str, body: SupportCategoryUpdate, principal: StaffManager, categories: CategoryServiceDep):
    """Organization Admin: rename, re-route, reorder or deactivate a category (the defaults too)."""
    category = categories.update(principal, category_id, body.model_dump(exclude_unset=True))
    return ok(SupportCategoryRead(id=str(category.id), name=category.name, route=category.route, description=category.description))
