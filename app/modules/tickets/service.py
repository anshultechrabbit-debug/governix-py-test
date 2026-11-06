"""Support Center tickets: creation with an AI routing recommendation, confirmation, workload-based
assignment, forwarding and escalation, the lifecycle, the timeline and dashboards.

    create   -> AWAITING_CONFIRMATION, with routing (team, priority, confidence, reason)   creator only
    confirm  -> OPEN in the chosen team's queue, assigned by workload (or left unassigned)  creator only
    then     IN_PROGRESS / WAITING_FOR_USER / WAITING_FOR_TEAM -> RESOLVED -> CLOSED (or CANCELLED)

Who may do what is decided here from the caller and the ticket (queues.py), never from the client, and
every query is limited to what the caller may see. Every change is a timeline entry, an audit event and
the notifications it implies, in one transaction.
"""

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, BinaryIO

from sqlalchemy import Text, cast, func, not_, or_, select
from sqlalchemy.orm import Session, aliased

from app.core.config import Settings
from app.core.exceptions import ConflictError, NotFoundError, PayloadTooLargeError, PermissionDeniedError, ValidationError
from app.infrastructure.ai.llm.base import LLMProvider
from app.infrastructure.storage.base import Storage
from app.modules.audit.service import record_event
from app.modules.auth.permissions import Principal, Role
from app.modules.branches.model import Branch
from app.modules.notifications.model import NotificationType
from app.modules.notifications.service import notify
from app.modules.organizations.model import Organization
from app.modules.policies.model import Policy
from app.modules.policies.repository import PolicyRepository
from app.modules.tickets import assignment, sla
from app.modules.tickets.model import (
    ACTIVE_STATUSES,
    MessageKind,
    SupportStaff,
    Ticket,
    TicketAttachment,
    TicketKind,
    TicketLevel,
    TicketMessage,
    TicketPriority,
    TicketStatus,
    TicketTeam,
)
from app.modules.tickets.queues import (
    ESCALATES_TO,
    LEVEL_TEAM,
    QUEUE_LABEL,
    Duty,
    can_handle,
    can_manage,
    forwarded_by_clause,
    handling_clause,
    load_duty,
    manager_ids,
    queue_name,
    queue_staff_query,
    reads_notes_of,
    visible_clause,
    works_queue,
)
from app.modules.tickets.triage import Recommendation, categories_for, recommend
from app.modules.users.model import User

MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
ATTACHMENT_TYPES = {
    ".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".txt": "text/plain", ".csv": "text/csv",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}
RAISERS = frozenset({Role.DEPARTMENT_USER, Role.BRANCH_MANAGER, Role.ORG_ADMIN})
# Forwarding destinations, by the name the API uses.
DESTINATIONS = {"branch": TicketLevel.BRANCH, "head_office": TicketLevel.ORGANIZATION, "technical": TicketLevel.PLATFORM}
DESTINATION_OF_LEVEL = {level: key for key, level in DESTINATIONS.items()}

# What the staff side may move a ticket to. Resolving needs a resolution note.
HANDLER_TRANSITIONS: dict[str, set[str]] = {
    TicketStatus.OPEN: {TicketStatus.IN_PROGRESS, TicketStatus.WAITING_FOR_USER, TicketStatus.WAITING_FOR_TEAM,
                        TicketStatus.RESOLVED},
    TicketStatus.IN_PROGRESS: {TicketStatus.WAITING_FOR_USER, TicketStatus.WAITING_FOR_TEAM, TicketStatus.RESOLVED},
    TicketStatus.WAITING_FOR_USER: {TicketStatus.IN_PROGRESS, TicketStatus.RESOLVED},
    TicketStatus.WAITING_FOR_TEAM: {TicketStatus.IN_PROGRESS, TicketStatus.RESOLVED},
    TicketStatus.RESOLVED: {TicketStatus.IN_PROGRESS},
}
# What the creator may do: accept a resolution, or reopen.
CREATOR_TRANSITIONS: dict[str, set[str]] = {
    TicketStatus.RESOLVED: {TicketStatus.CLOSED, TicketStatus.OPEN},
    TicketStatus.CLOSED: {TicketStatus.OPEN},
}
FINISHED = frozenset({TicketStatus.CLOSED, TicketStatus.CANCELLED})


def reference(ticket: Ticket) -> str:
    return f"SUP-{ticket.number:06d}" if ticket.number is not None else "SUP-NEW"


@dataclass
class LastReply:
    """The latest reply from someone other than the creator: the support side's answer."""

    body: str
    author_name: str | None
    at: datetime


@dataclass
class ForwardedBy:
    """The latest forward or escalation: who sent the ticket to the queue it is in now, from where, and why."""

    by_id: uuid.UUID | None
    by_name: str | None
    by_role: str | None
    from_queue: str | None
    to_queue: str | None
    reason: str | None
    escalation: bool
    at: datetime


@dataclass
class TicketView:
    ticket: Ticket
    created_by_name: str | None
    created_by_role: str | None
    assigned_to_name: str | None
    message_count: int
    can_handle: bool
    queue_name: str
    policy_name: str | None = None
    organization_name: str | None = None
    branch_name: str | None = None
    last_reply: LastReply | None = None
    forwarded: ForwardedBy | None = None
    # The caller forwarded it to a queue they do not work: they follow it, but cannot reply, note or attach.
    read_only: bool = False
    actions: dict[str, Any] = field(default_factory=dict)

    @property
    def overdue(self) -> bool:
        return sla.is_overdue(self.ticket)

    @property
    def response_overdue(self) -> bool:
        return sla.response_overdue(self.ticket)


@dataclass
class TimelineEntry:
    message: TicketMessage
    author_name: str | None
    author_role: str | None
    data: dict[str, Any]


class TicketService:
    def __init__(self, session: Session, storage: Storage, settings: Settings,
                 llm: LLMProvider | None = None) -> None:
        self.session = session
        self.storage = storage
        self.settings = settings
        self.llm = llm
        self._duty: dict[uuid.UUID, Duty] = {}

    def duty(self, principal: Principal) -> Duty:
        if principal.user_id not in self._duty:
            self._duty[principal.user_id] = load_duty(self.session, principal)
        return self._duty[principal.user_id]

    # --- create and confirm --------------------------------------------------------------------------------

    def categories(self, principal: Principal):
        if principal.organization_id is None:
            return []
        return categories_for(self.session, principal.organization_id)

    def create(self, principal: Principal, *, category_id: str, subject: str, description: str,
               priority: TicketPriority | None, policy_id: uuid.UUID | None = None) -> Ticket:
        """Write the ticket and its routing recommendation. Nobody else sees it until the creator confirms."""
        if principal.role not in RAISERS or principal.organization_id is None:
            raise PermissionDeniedError("Your role does not raise tickets.")
        options = self.categories(principal)
        category = next((c for c in options if c.id == category_id), None)
        if category is None:
            raise ValidationError("Choose one of the categories.", code="CATEGORY_NOT_FOUND")
        # Only a policy the creator can read (a User: one assigned to them), so a ticket never names another.
        if policy_id is not None and PolicyRepository(self.session).get_visible(principal, policy_id) is None:
            raise ValidationError("Choose one of the policies you can read.", code="POLICY_NOT_FOUND")

        recommendation = recommend(
            self.llm, category=category, subject=subject, description=description,
            skills=self._skill_tags(principal), enabled=self.settings.SUPPORT_TRIAGE_AI, categories=options,
        )
        team = recommendation.team or TicketTeam.BRANCH
        ticket = Ticket(
            organization_id=principal.organization_id, branch_id=principal.branch_id,
            kind=TicketKind.SUPPORT, level=self._level_for(principal, team), team=team,
            category=category.name, category_id=None if category.id.startswith("default:") else uuid.UUID(category.id),
            issue_type=recommendation.issue_type, skills=recommendation.skills,
            subject=subject.strip(), description=description.strip(),
            priority=priority or recommendation.priority, status=TicketStatus.AWAITING_CONFIRMATION,
            created_by_id=principal.user_id, policy_id=policy_id,
        )
        self.session.add(ticket)
        self.session.flush()
        ticket.routing = self._routing(principal, ticket, recommendation, priority_chosen=priority is not None)
        record_event(self.session, "ticket.created", actor=principal, organization_id=ticket.organization_id,
                     resource_type="ticket", resource_id=ticket.id,
                     details={"reference": reference(ticket), "subject": ticket.subject, "category": category.name,
                              "recommended_team": recommendation.team, "confidence": recommendation.confidence,
                              "source": recommendation.source})
        self.session.commit()
        return ticket

    def _skill_tags(self, principal: Principal) -> list[str]:
        """The skill tags of the staff who could take a ticket from this person (any team)."""
        tags: list[str] = []
        for level in (TicketLevel.BRANCH, TicketLevel.ORGANIZATION, TicketLevel.PLATFORM):
            if level == TicketLevel.BRANCH and principal.branch_id is None:
                continue
            for staff in self.session.scalars(queue_staff_query(level, principal.organization_id, principal.branch_id)):
                tags.extend(staff.skills or [])
        return list(dict.fromkeys(tags))

    @staticmethod
    def _level_for(principal: Principal, team: str) -> str:
        """Where a team's ticket from this person starts: Technical at the platform; Branch Support at their
        branch, or at Head Office for a Branch Manager or an Org Admin (the branch is theirs to run)."""
        if team == TicketTeam.TECHNICAL:
            return TicketLevel.PLATFORM
        if principal.role is Role.DEPARTMENT_USER and principal.branch_id is not None:
            return TicketLevel.BRANCH
        return TicketLevel.ORGANIZATION

    def _routing(self, principal: Principal, ticket: Ticket, recommendation: Recommendation, *,
                 priority_chosen: bool) -> dict[str, Any]:
        options = {}
        for team in (TicketTeam.TECHNICAL, TicketTeam.BRANCH):
            level = self._level_for(principal, team)
            options[team] = {"level": level, "queue": queue_name(self.session, level, ticket.branch_id)}
        suggested = None
        if recommendation.team is not None:
            found = assignment.best(self.session, ticket, lock=False)
            if found is not None:
                user = self.session.get(User, found.staff.user_id)
                suggested = {"id": str(user.id), "name": user.full_name}
        return {
            **recommendation.as_dict(self.settings.SUPPORT_TRIAGE_MIN_CONFIDENCE),
            "priority_chosen": priority_chosen, "options": options, "suggested_assignee": suggested,
        }

    def confirm(self, principal: Principal, ticket_id: uuid.UUID, team: TicketTeam) -> Ticket:
        """The creator confirms (or switches) the destination: the ticket opens there and is assigned.
        Confirming twice is harmless: the second call returns the ticket as it is."""
        ticket = self._locked(principal, ticket_id)
        if ticket.created_by_id != principal.user_id:
            raise PermissionDeniedError("Only the person who raised the ticket confirms where it goes.")
        if ticket.status != TicketStatus.AWAITING_CONFIRMATION:
            if ticket.confirmed_at is not None and ticket.team == team:
                return ticket  # a repeated request
            raise ConflictError("This ticket has already been sent.", code="ALREADY_CONFIRMED")
        routing = ticket.routing or {}
        now = datetime.now(UTC)
        ticket.team, ticket.level = team, self._level_for(principal, team)
        ticket.status, ticket.confirmed_at = TicketStatus.OPEN, now
        sla.set_due(self.settings, ticket)
        destination = {"level": ticket.level, "team": team, "queue": queue_name(self.session, ticket.level, ticket.branch_id)}
        switched = routing.get("team") is not None and routing.get("team") != team
        self.session.add(TicketMessage(
            ticket_id=ticket.id, organization_id=ticket.organization_id, author_id=principal.user_id,
            kind=MessageKind.FORWARD, data={
                "initial": True, "from": None, "to": destination, "recommended": routing.get("team"),
                "chosen": team, "switched": switched, "confirmed": True, "confidence": routing.get("confidence"),
            },
        ))
        record_event(self.session, "ticket.routed", actor=principal, organization_id=ticket.organization_id,
                     resource_type="ticket", resource_id=ticket.id,
                     details={"reference": reference(ticket), "to": destination, "recommended": routing.get("team"),
                              "switched": switched})
        assignee = self._auto_assign(principal, ticket)
        notify(self.session, [ticket.created_by_id], NotificationType.TICKET_UPDATED,
               f"{reference(ticket)} raised: {ticket.subject}",
               body=f"With {destination['queue']}" + (f", assigned to {assignee.full_name}." if assignee else "."),
               organization_id=ticket.organization_id, link=f"/support/{ticket.id}",
               data={"ticket_id": str(ticket.id)})
        self.session.commit()
        return ticket

    # --- assignment ------------------------------------------------------------------------------------------

    def _auto_assign(self, actor: Principal | None, ticket: Ticket, *,
                     exclude: frozenset[uuid.UUID] = frozenset(), why: str | None = None,
                     forwarded: str | None = None) -> User | None:
        """Give the ticket to the best eligible staff member of its queue, or leave it unassigned and tell the
        queue's managers. The caller holds the ticket's row lock."""
        found = assignment.best(self.session, ticket, exclude=exclude)
        previous = ticket.assigned_to_id
        if found is None:
            ticket.assigned_to_id = None
            self.session.add(TicketMessage(
                ticket_id=ticket.id, organization_id=ticket.organization_id,
                author_id=actor.user_id if actor else None, kind=MessageKind.ASSIGN,
                body=why, data={"auto": True, "to": None, "from": str(previous) if previous else None, "unassigned": True},
            ))
            notify(self.session, manager_ids(self.session, ticket.level, ticket.organization_id, ticket.branch_id),
                   NotificationType.TICKET_UPDATED, f"Unassigned: {reference(ticket)} {ticket.subject}",
                   body=f"No one in {queue_name(self.session, ticket.level, ticket.branch_id)} is available to take it.",
                   organization_id=ticket.organization_id, link=f"/support/{ticket.id}",
                   data={"ticket_id": str(ticket.id)}, exclude=actor.user_id if actor else None)
            if previous:
                notify(self.session, [previous], NotificationType.TICKET_ASSIGNED,
                       f"Unassigned: {reference(ticket)} {ticket.subject}", body=why,
                       organization_id=ticket.organization_id, link=f"/support/{ticket.id}",
                       data={"ticket_id": str(ticket.id)}, exclude=actor.user_id if actor else None)
            return None
        user = self.session.get(User, found.staff.user_id)
        self._set_assignee(actor, ticket, user, auto=True, why=why, load=found.explain(), forwarded=forwarded)
        found.staff.last_assigned_at = datetime.now(UTC)
        return user

    def _set_assignee(self, actor: Principal | None, ticket: Ticket, user: User | None, *, auto: bool,
                      why: str | None = None, load: dict | None = None, forwarded: str | None = None) -> None:
        """`forwarded`: who forwarded it, from where and why, told to the new assignee (not written on the
        timeline: the forward entry already says it)."""
        previous = ticket.assigned_to_id
        ticket.assigned_to_id = user.id if user else None
        self.session.add(TicketMessage(
            ticket_id=ticket.id, organization_id=ticket.organization_id, author_id=actor.user_id if actor else None,
            kind=MessageKind.ASSIGN, body=why,
            data={"auto": auto, "to": str(user.id) if user else None, "to_name": user.full_name if user else None,
                  "from": str(previous) if previous else None, **({"load": load} if load else {})},
        ))
        record_event(self.session, "ticket.assigned", actor=actor, organization_id=ticket.organization_id,
                     resource_type="ticket", resource_id=ticket.id,
                     details={"assigned_to": str(user.id) if user else None, "auto": auto,
                              "from": str(previous) if previous else None})
        exclude = actor.user_id if actor else None
        if user is not None:
            notify(self.session, [user.id], NotificationType.TICKET_ASSIGNED,
                   f"{'Forwarded' if forwarded else 'Assigned'} to you: {reference(ticket)} {ticket.subject}",
                   organization_id=ticket.organization_id, body=forwarded or why, link=f"/support/{ticket.id}",
                   data={"ticket_id": str(ticket.id)}, exclude=exclude)
        if previous and previous != (user.id if user else None):
            notify(self.session, [previous], NotificationType.TICKET_ASSIGNED,
                   f"Reassigned: {reference(ticket)} {ticket.subject}",
                   body=f"Now with {user.full_name}." if user else "Now unassigned.",
                   organization_id=ticket.organization_id, link=f"/support/{ticket.id}",
                   data={"ticket_id": str(ticket.id)}, exclude=exclude)

    def assign(self, principal: Principal, ticket_id: uuid.UUID, user_id: uuid.UUID | None) -> Ticket:
        """Take a ticket (a member of its queue), or assign/reassign/unassign it (its managers and leads)."""
        ticket = self._locked(principal, ticket_id)
        duty = self.duty(principal)
        if ticket.status in FINISHED or ticket.status == TicketStatus.AWAITING_CONFIRMATION:
            raise ConflictError("This ticket cannot be assigned now.", code="INVALID_STATE")
        taking = user_id == principal.user_id
        if taking:
            if not can_handle(principal, duty, ticket):
                raise PermissionDeniedError("Only the team handling this ticket can take it.")
        elif not can_manage(principal, duty, ticket):
            raise PermissionDeniedError("Only the queue's manager or lead can assign this ticket.")
        if user_id == ticket.assigned_to_id:
            return ticket
        user = None
        if user_id is not None:
            if not taking and user_id not in self._queue_member_ids(ticket):
                raise ValidationError("That person is not on duty in this ticket's queue.", code="NOT_IN_QUEUE")
            user = self.session.get(User, user_id)
            if user is None or not user.is_active:
                raise ValidationError("That person is not on duty in this ticket's queue.", code="NOT_IN_QUEUE")
        self._set_assignee(principal, ticket, user, auto=False)
        ticket.updated_at = datetime.now(UTC)
        self.session.commit()
        return ticket

    def _queue_member_ids(self, ticket: Ticket) -> set[uuid.UUID]:
        return {s.user_id for s in self.session.scalars(
            queue_staff_query(ticket.level, ticket.organization_id, ticket.branch_id))}

    def assignees(self, principal: Principal, ticket_id: uuid.UUID, destination: str | None) -> list[dict]:
        """The staff of the ticket's queue (or of a forwarding destination) with their workload."""
        ticket = self.get(principal, ticket_id)
        if not can_handle(principal, self.duty(principal), ticket):
            raise PermissionDeniedError("Only the team handling this ticket can see its queue.")
        level = DESTINATIONS.get(destination) if destination else ticket.level
        if level is None or (level == TicketLevel.BRANCH and ticket.branch_id is None):
            raise ValidationError("Unknown destination.", code="INVALID_DESTINATION")
        rows = self.session.execute(
            queue_staff_query(level, ticket.organization_id, ticket.branch_id)
            .add_columns(User.full_name).order_by(User.full_name)
        ).all()
        counts = assignment.open_counts(self.session, [s.user_id for s, _ in rows])
        return [{"user_id": s.user_id, "name": name, "availability": s.availability, "staff_role": s.staff_role,
                 "skills": s.skills, "open_tickets": counts.get(s.user_id, 0), "capacity": s.capacity,
                 "load": round(counts.get(s.user_id, 0) / s.capacity, 3)} for s, name in rows]

    def reassign_open(self, principal: Principal, staff: SupportStaff, note: str | None) -> int:
        """Move an (away or deactivated) staff member's open tickets to others, each one recorded and notified.
        Only tickets in queues the caller manages."""
        duty = self.duty(principal)
        user = self.session.get(User, staff.user_id)
        tickets = self.session.scalars(
            select(Ticket).where(Ticket.assigned_to_id == staff.user_id, Ticket.status.in_(list(ACTIVE_STATUSES)))
            .order_by(Ticket.created_at).with_for_update()
        ).all()
        moved = 0
        for ticket in tickets:
            if not can_manage(principal, duty, ticket):
                continue
            why = note or f"Reassigned from {user.full_name if user else 'a staff member'}."
            self._auto_assign(principal, ticket, exclude=frozenset({staff.user_id}), why=why)
            ticket.updated_at = datetime.now(UTC)
            moved += 1
        self.session.commit()
        return moved

    # --- forward and escalate --------------------------------------------------------------------------------

    def destinations(self, ticket: Ticket) -> list[dict[str, str]]:
        found = []
        for key, level in DESTINATIONS.items():
            if level == ticket.level or (level == TicketLevel.BRANCH and ticket.branch_id is None):
                continue
            found.append({"key": key, "level": level, "team": LEVEL_TEAM[level],
                          "label": queue_name(self.session, level, ticket.branch_id)})
        return found

    def forward(self, principal: Principal, ticket_id: uuid.UUID, *, to: str, reason: str, confirmed: bool,
                user_id: uuid.UUID | None = None) -> Ticket:
        """Move a ticket to another queue (or `to="escalate"`: one level up), with a reason, recorded and
        notified. The staff side forwards; the creator may escalate an overdue ticket."""
        ticket = self._locked(principal, ticket_id)
        duty = self.duty(principal)
        if not confirmed:
            raise ValidationError("Confirm the forward.", code="CONFIRMATION_REQUIRED")
        if ticket.status not in ACTIVE_STATUSES:
            raise ConflictError("Only an open ticket can be forwarded.", code="INVALID_STATE")
        escalation = to == "escalate"
        handler = can_handle(principal, duty, ticket)
        by_creator = ticket.created_by_id == principal.user_id and not handler
        if by_creator:
            if not (escalation and sla.is_overdue(ticket)):
                raise PermissionDeniedError("You can escalate your ticket once it is overdue.")
        elif not handler:
            raise PermissionDeniedError("Only the team handling this ticket can forward it.")
        level = ESCALATES_TO.get(ticket.level) if escalation else DESTINATIONS.get(to)
        if level is None:
            raise ValidationError("This ticket cannot go further up." if escalation else "Unknown destination.",
                                  code="INVALID_DESTINATION")
        if level == ticket.level or (level == TicketLevel.BRANCH and ticket.branch_id is None):
            raise ValidationError("Choose another team.", code="INVALID_DESTINATION")
        reason = " ".join(reason.split())
        if len(reason) < 3:
            raise ValidationError("Give a reason for forwarding.", code="REASON_REQUIRED")

        source = {"level": ticket.level, "team": ticket.team, "queue": queue_name(self.session, ticket.level, ticket.branch_id),
                  "assigned_to": str(ticket.assigned_to_id) if ticket.assigned_to_id else None}
        previous_assignee = ticket.assigned_to_id
        ticket.level, ticket.team = level, LEVEL_TEAM[level]
        ticket.status, ticket.assigned_to_id = TicketStatus.OPEN, None
        ticket.updated_at = datetime.now(UTC)
        destination = {"level": level, "team": ticket.team, "queue": queue_name(self.session, level, ticket.branch_id)}
        self.session.add(TicketMessage(
            ticket_id=ticket.id, organization_id=ticket.organization_id, author_id=principal.user_id,
            kind=MessageKind.FORWARD, body=reason,
            data={"from": source, "to": destination, "confirmed": True, "escalation": escalation, "by_creator": by_creator},
        ))
        record_event(self.session, "ticket.escalated" if escalation else "ticket.forwarded", actor=principal,
                     organization_id=ticket.organization_id, resource_type="ticket", resource_id=ticket.id,
                     details={"reference": reference(ticket), "from": source, "to": destination})
        exclude = principal.user_id
        if previous_assignee:
            notify(self.session, [previous_assignee], NotificationType.TICKET_FORWARDED,
                   f"Forwarded: {reference(ticket)} {ticket.subject}", body=f"Now with {destination['queue']}: {reason}",
                   organization_id=ticket.organization_id, link=f"/support/{ticket.id}",
                   data={"ticket_id": str(ticket.id)}, exclude=exclude)
        notify(self.session, [ticket.created_by_id], NotificationType.TICKET_FORWARDED,
               f"{reference(ticket)} is now with {destination['queue']}", body=reason,
               organization_id=ticket.organization_id, link=f"/support/{ticket.id}",
               data={"ticket_id": str(ticket.id)}, exclude=exclude)
        # The new assignee is told who sent it, from where and why, so they can answer accordingly.
        sender = self.session.get(User, principal.user_id)
        notice = (f"{sender.full_name if sender else 'Someone'} {'escalated' if escalation else 'forwarded'} it "
                  f"from {source['queue']}: {reason}")
        if user_id is not None:
            if user_id not in self._queue_member_ids(ticket) or user_id == ticket.created_by_id:
                raise ValidationError("That person is not on duty in that queue.", code="NOT_IN_QUEUE")
            self._set_assignee(principal, ticket, self.session.get(User, user_id), auto=False, forwarded=notice)
        else:
            # or, with nobody available, the queue's managers are told
            self._auto_assign(principal, ticket, forwarded=notice)
        self.session.commit()
        return ticket

    def _forwarded_away(self, principal: Principal, duty: Duty, ticket: Ticket) -> bool:
        """The caller forwarded this ticket and no longer works its queue: they follow it, read-only. The
        creator and the handling team are never read-only (a ticket forwarded back is theirs again)."""
        if ticket.created_by_id == principal.user_id or can_handle(principal, duty, ticket):
            return False
        return self.session.scalar(
            select(TicketMessage.id).where(TicketMessage.ticket_id == ticket.id, TicketMessage.kind == MessageKind.FORWARD,
                                           TicketMessage.author_id == principal.user_id).limit(1)) is not None

    def _refuse_if_forwarded_away(self, principal: Principal, ticket: Ticket) -> None:
        if self._forwarded_away(principal, self.duty(principal), ticket):
            raise PermissionDeniedError(
                f"You forwarded this ticket to {queue_name(self.session, ticket.level, ticket.branch_id)}. "
                "You can follow it, but only that team answers it now.", code="TICKET_FORWARDED_AWAY")

    def forwarded(self, ticket: Ticket) -> dict[str, Any]:
        """Where a forwarded ticket went."""
        assignee = self.session.get(User, ticket.assigned_to_id) if ticket.assigned_to_id else None
        return {"reference": reference(ticket), "level": ticket.level, "team": ticket.team,
                "queue_name": queue_name(self.session, ticket.level, ticket.branch_id),
                "assigned_to_name": assignee.full_name if assignee else None}

    # --- conversation ------------------------------------------------------------------------------------------

    def reply(self, principal: Principal, ticket_id: uuid.UUID, body: str, *, internal: bool = False) -> TicketMessage:
        ticket = self._locked(principal, ticket_id)
        duty = self.duty(principal)
        if ticket.status in FINISHED:
            raise ConflictError("This ticket is closed. Reopen it to reply.", code="INVALID_STATE")
        if ticket.status == TicketStatus.AWAITING_CONFIRMATION:
            raise ConflictError("Confirm where the ticket goes first.", code="INVALID_STATE")
        self._refuse_if_forwarded_away(principal, ticket)
        handling = can_handle(principal, duty, ticket)
        if internal and not reads_notes_of(principal, duty, ticket, ticket.level):
            raise PermissionDeniedError("Only the team handling this ticket writes internal notes.")
        now = datetime.now(UTC)
        message = TicketMessage(
            ticket_id=ticket.id, organization_id=ticket.organization_id, author_id=principal.user_id,
            kind=MessageKind.NOTE if internal else MessageKind.REPLY, body=body.strip(),
            data={"level": ticket.level, "team": ticket.team} if internal else {},
        )
        self.session.add(message)
        ticket.updated_at = now
        creator = ticket.created_by_id == principal.user_id
        if internal:
            if ticket.assigned_to_id:
                notify(self.session, [ticket.assigned_to_id], NotificationType.TICKET_UPDATED,
                       f"Internal note: {reference(ticket)} {ticket.subject}", body=body.strip()[:300],
                       organization_id=ticket.organization_id, link=f"/support/{ticket.id}",
                       data={"ticket_id": str(ticket.id)}, exclude=principal.user_id)
        elif handling and not creator:
            # The support side has answered: the response target is met, and the ticket is being worked.
            ticket.first_response_at = ticket.first_response_at or now
            if ticket.status == TicketStatus.OPEN:
                self._set_status(principal, ticket, TicketStatus.IN_PROGRESS, record=False)
            notify(self.session, [ticket.created_by_id], NotificationType.SUPPORT_RESPONSE,
                   f"Response to {reference(ticket)}: {ticket.subject}", body=body.strip()[:300],
                   organization_id=ticket.organization_id, link=f"/support/{ticket.id}",
                   data={"ticket_id": str(ticket.id)}, exclude=principal.user_id)
        else:
            if creator and ticket.status == TicketStatus.WAITING_FOR_USER:
                self._set_status(principal, ticket, TicketStatus.IN_PROGRESS, record=False)
            recipients = ({ticket.assigned_to_id} if ticket.assigned_to_id
                          else manager_ids(self.session, ticket.level, ticket.organization_id, ticket.branch_id)
                          | self._queue_member_ids(ticket))
            if not creator:
                recipients.add(ticket.created_by_id)
            notify(self.session, recipients, NotificationType.TICKET_UPDATED,
                   f"New reply: {reference(ticket)} {ticket.subject}", body=body.strip()[:300],
                   organization_id=ticket.organization_id, link=f"/support/{ticket.id}",
                   data={"ticket_id": str(ticket.id)}, exclude=principal.user_id)
        self.session.commit()
        return message

    # --- lifecycle ---------------------------------------------------------------------------------------------

    def allowed_statuses(self, principal: Principal, ticket: Ticket) -> list[str]:
        allowed: set[str] = set()
        if can_handle(principal, self.duty(principal), ticket):
            allowed |= HANDLER_TRANSITIONS.get(ticket.status, set())
        if ticket.created_by_id == principal.user_id:
            allowed |= CREATOR_TRANSITIONS.get(ticket.status, set())
        return sorted(allowed)

    def change_status(self, principal: Principal, ticket_id: uuid.UUID, status: TicketStatus, note: str | None) -> Ticket:
        ticket = self._locked(principal, ticket_id)
        if status == ticket.status:
            return ticket
        if status not in self.allowed_statuses(principal, ticket):
            raise PermissionDeniedError("You cannot move this ticket to that status.")
        note = (note or "").strip() or None
        if status == TicketStatus.RESOLVED:
            if not note or len(note) < 3:
                raise ValidationError("Write a resolution note.", code="RESOLUTION_NOTE_REQUIRED")
            ticket.resolution_note = note
        reopening = status == TicketStatus.OPEN and ticket.status in (TicketStatus.RESOLVED, TicketStatus.CLOSED)
        self._set_status(principal, ticket, status, note=note)
        recipients = {ticket.created_by_id}
        if reopening:
            ticket.reopen_count += 1
            ticket.resolution_note = None
            if ticket.due_at is None:
                sla.set_due(self.settings, ticket)
            record_event(self.session, "ticket.reopened", actor=principal, organization_id=ticket.organization_id,
                         resource_type="ticket", resource_id=ticket.id, details={"reference": reference(ticket)})
            # Back to whoever had it, if they are still on duty in its queue; otherwise by workload.
            if ticket.assigned_to_id not in self._queue_member_ids(ticket):
                self._auto_assign(principal, ticket, why="Reopened.")
        if ticket.assigned_to_id:
            recipients.add(ticket.assigned_to_id)
        else:
            recipients |= manager_ids(self.session, ticket.level, ticket.organization_id, ticket.branch_id)
        label = "reopened" if reopening else status.replace("_", " ")
        notify(self.session, recipients, NotificationType.TICKET_UPDATED, f"{reference(ticket)} {label}: {ticket.subject}",
               body=note, organization_id=ticket.organization_id, link=f"/support/{ticket.id}",
               data={"ticket_id": str(ticket.id), "status": status}, exclude=principal.user_id)
        self.session.commit()
        return ticket

    def cancel(self, principal: Principal, ticket_id: uuid.UUID, note: str | None) -> Ticket:
        """The creator withdraws (a draft or an open ticket); a manager cancels (a duplicate, say), with a note."""
        ticket = self._locked(principal, ticket_id)
        creator = ticket.created_by_id == principal.user_id
        cancellable = ticket.status == TicketStatus.AWAITING_CONFIRMATION or ticket.status in ACTIVE_STATUSES
        if not cancellable:
            raise ConflictError("This ticket can no longer be cancelled.", code="INVALID_STATE")
        note = (note or "").strip() or None
        if not creator:
            if not can_manage(principal, self.duty(principal), ticket):
                raise PermissionDeniedError("You cannot cancel this ticket.")
            if not note:
                raise ValidationError("Say why the ticket is cancelled.", code="REASON_REQUIRED")
        draft = ticket.status == TicketStatus.AWAITING_CONFIRMATION
        self._set_status(principal, ticket, TicketStatus.CANCELLED, note=note)
        ticket.cancelled_at = datetime.now(UTC)
        if not draft:
            recipients = {ticket.created_by_id, ticket.assigned_to_id}
            notify(self.session, recipients, NotificationType.TICKET_UPDATED,
                   f"{reference(ticket)} cancelled: {ticket.subject}", body=note, organization_id=ticket.organization_id,
                   link=f"/support/{ticket.id}", data={"ticket_id": str(ticket.id)}, exclude=principal.user_id)
        self.session.commit()
        return ticket

    def set_priority(self, principal: Principal, ticket_id: uuid.UUID, priority: TicketPriority, note: str | None) -> Ticket:
        ticket = self._locked(principal, ticket_id)
        if not can_handle(principal, self.duty(principal), ticket) or ticket.status in FINISHED:
            raise PermissionDeniedError("Only the team handling this ticket changes its priority.")
        if priority == ticket.priority:
            return ticket
        previous = ticket.priority
        ticket.priority = priority
        if ticket.confirmed_at is not None:
            sla.set_due(self.settings, ticket)
        self.session.add(TicketMessage(
            ticket_id=ticket.id, organization_id=ticket.organization_id, author_id=principal.user_id,
            kind=MessageKind.STATUS, body=(note or "").strip() or None, data={"priority": {"from": previous, "to": priority}},
        ))
        record_event(self.session, "ticket.priority_changed", actor=principal, organization_id=ticket.organization_id,
                     resource_type="ticket", resource_id=ticket.id, details={"from": previous, "to": priority})
        ticket.updated_at = datetime.now(UTC)
        self.session.commit()
        return ticket

    def _set_status(self, principal: Principal | None, ticket: Ticket, status: TicketStatus, *, note: str | None = None,
                    record: bool = True) -> None:
        now = datetime.now(UTC)
        previous = ticket.status
        ticket.status = status
        if status == TicketStatus.RESOLVED:
            ticket.resolved_at = now
        elif status != TicketStatus.CLOSED:
            ticket.resolved_at = None
        ticket.closed_at = now if status == TicketStatus.CLOSED else None
        ticket.updated_at = now
        self.session.add(TicketMessage(ticket_id=ticket.id, organization_id=ticket.organization_id,
                                       author_id=principal.user_id if principal else None, kind=MessageKind.STATUS,
                                       body=note, status_change=status))
        if record:
            record_event(self.session, "ticket.status_changed", actor=principal, organization_id=ticket.organization_id,
                         resource_type="ticket", resource_id=ticket.id,
                         details={"reference": reference(ticket), "from": previous, "to": status})

    # --- read --------------------------------------------------------------------------------------------------

    def get(self, principal: Principal, ticket_id: uuid.UUID) -> Ticket:
        ticket = self.session.scalar(
            select(Ticket).where(Ticket.id == ticket_id, visible_clause(principal, self.duty(principal))))
        if ticket is None:
            raise NotFoundError("Ticket not found.")
        return ticket

    def _locked(self, principal: Principal, ticket_id: uuid.UUID) -> Ticket:
        """The ticket, if visible, with its row locked until commit: concurrent changes go one at a time."""
        ticket = self.session.scalar(
            select(Ticket).where(Ticket.id == ticket_id, visible_clause(principal, self.duty(principal)))
            .with_for_update())
        if ticket is None:
            raise NotFoundError("Ticket not found.")
        return ticket

    def _boxed(self, principal: Principal, query, box: str):
        duty = self.duty(principal)
        query = query.where(visible_clause(principal, duty))
        if box == "mine":
            return query.where(Ticket.created_by_id == principal.user_id)
        if box == "queue":
            return query.where(handling_clause(principal, duty), Ticket.created_by_id != principal.user_id)
        if box == "assigned":
            return query.where(Ticket.assigned_to_id == principal.user_id)
        if box == "unassigned":
            return query.where(handling_clause(principal, duty), Ticket.assigned_to_id.is_(None),
                               Ticket.status.in_(list(ACTIVE_STATUSES)))
        if box == "forwarded":
            # Forwarded by me and now with a queue I do not work: the ones I follow read-only. IS NOT true,
            # because the handling clause is NULL (not false) for an unassigned ticket.
            return query.where(forwarded_by_clause(principal), handling_clause(principal, duty).is_not(True),
                               or_(Ticket.created_by_id.is_(None), Ticket.created_by_id != principal.user_id))
        # all: drafts are their creator's only
        return query.where(or_(Ticket.status != TicketStatus.AWAITING_CONFIRMATION,
                               Ticket.created_by_id == principal.user_id))

    def find(self, principal: Principal, *, box: str, status: str | None, team: str | None, priority: str | None,
             overdue: bool, assignee: str | None, search: str | None, limit: int, offset: int) -> tuple[list[TicketView], int]:
        query = self._boxed(principal, select(Ticket), box)
        if status == "active":
            query = query.where(Ticket.status.in_(list(ACTIVE_STATUSES)))
        elif status == "done":
            query = query.where(Ticket.status.in_([TicketStatus.RESOLVED, TicketStatus.CLOSED, TicketStatus.CANCELLED]))
        elif status:
            query = query.where(Ticket.status == status)
        if team:
            query = query.where(Ticket.team == team)
        if priority:
            query = query.where(Ticket.priority == priority)
        if overdue:
            query = query.where(Ticket.status.in_(list(ACTIVE_STATUSES)), Ticket.due_at <= datetime.now(UTC))
        if assignee == "me":
            query = query.where(Ticket.assigned_to_id == principal.user_id)
        elif assignee == "none":
            query = query.where(Ticket.assigned_to_id.is_(None))
        elif assignee:
            try:
                query = query.where(Ticket.assigned_to_id == uuid.UUID(assignee))
            except ValueError:
                raise ValidationError("Unknown assignee.") from None
        if search and search.strip():
            text = search.strip().lower()
            term = f"%{text}%"
            creator, policy = aliased(User), aliased(Policy)
            number = text.removeprefix("sup-").lstrip("0")
            query = (
                query.outerjoin(creator, creator.id == Ticket.created_by_id)
                .outerjoin(policy, policy.id == Ticket.policy_id)
                .where(or_(
                    *(func.lower(column).like(term) for column in (
                        Ticket.subject, Ticket.description, Ticket.category, Ticket.issue_type, creator.full_name,
                        policy.name)),
                    *([cast(Ticket.number, Text) == number] if number.isdigit() else []),
                ))
            )
        total = self.session.scalar(select(func.count()).select_from(query.subquery())) or 0
        tickets = self.session.scalars(
            query.order_by(Ticket.updated_at.desc(), Ticket.id).limit(limit).offset(offset)).all()
        return self.views(principal, list(tickets)), total

    def dashboard(self, principal: Principal, *, box: str) -> dict[str, Any]:
        """Counts for one box: by status, team, priority and level; overdue and unassigned; and, for the
        queues the caller manages, each staff member's workload."""
        base = self._boxed(principal, select(Ticket), box).subquery()

        def grouped(column) -> dict[str, int]:
            return {str(k): v for k, v in self.session.execute(
                select(getattr(base.c, column), func.count()).group_by(getattr(base.c, column))).all()}

        active = [s.value for s in ACTIVE_STATUSES]
        now = datetime.now(UTC)
        counts = {
            "by_status": {s.value: 0 for s in TicketStatus} | grouped("status"),
            "by_team": grouped("team"),
            "by_priority": {p.value: 0 for p in TicketPriority} | {
                str(k): v for k, v in self.session.execute(
                    select(base.c.priority, func.count()).where(base.c.status.in_(active)).group_by(base.c.priority)).all()},
            "by_level": grouped("level"),
            "active": self.session.scalar(select(func.count()).where(base.c.status.in_(active))) or 0,
            "overdue": self.session.scalar(select(func.count()).where(
                base.c.status.in_(active), base.c.due_at <= now)) or 0,
            "response_overdue": self.session.scalar(select(func.count()).where(
                base.c.status.in_(active), base.c.first_response_at.is_(None), base.c.response_due_at <= now)) or 0,
            "unassigned": self.session.scalar(select(func.count()).where(
                base.c.status.in_(active), base.c.assigned_to_id.is_(None))) or 0,
        }
        rows = self.session.execute(
            select(base.c.assigned_to_id, User.full_name, func.count())
            .join(User, User.id == base.c.assigned_to_id).where(base.c.status.in_(active))
            .group_by(base.c.assigned_to_id, User.full_name).order_by(func.count().desc()).limit(50)
        ).all()
        capacity = dict(self.session.execute(
            select(SupportStaff.user_id, SupportStaff.capacity).where(SupportStaff.user_id.in_([r[0] for r in rows]))
        ).all()) if rows else {}
        counts["by_assignee"] = [
            {"user_id": user_id, "name": name, "open_tickets": n, "capacity": capacity.get(user_id),
             "load": round(n / capacity[user_id], 3) if capacity.get(user_id) else None}
            for user_id, name, n in rows
        ]
        return counts

    def views(self, principal: Principal, tickets: list[Ticket], *, with_actions: bool = False) -> list[TicketView]:
        if not tickets:
            return []
        duty = self.duty(principal)
        people = {t.created_by_id for t in tickets} | {t.assigned_to_id for t in tickets}
        users = {uid: (name, role) for uid, name, role in self.session.execute(
            select(User.id, User.full_name, User.role).where(User.id.in_(people - {None}))
        ).all()}
        organizations = dict(self.session.execute(
            select(Organization.id, Organization.name).where(Organization.id.in_({t.organization_id for t in tickets}))
        ).all())
        branches = dict(self.session.execute(
            select(Branch.id, Branch.name).where(Branch.id.in_({t.branch_id for t in tickets} - {None}))
        ).all())
        counts = dict(self.session.execute(
            select(TicketMessage.ticket_id, func.count()).where(
                TicketMessage.ticket_id.in_([t.id for t in tickets]), TicketMessage.kind == MessageKind.REPLY)
            .group_by(TicketMessage.ticket_id)
        ).all())
        policies = dict(self.session.execute(
            select(Policy.id, Policy.name).where(Policy.id.in_({t.policy_id for t in tickets} - {None}))
        ).all())
        replies = self._last_replies(tickets)
        forwards = self._last_forwards(tickets)
        sent_by_me = set(self.session.scalars(
            select(TicketMessage.ticket_id).where(TicketMessage.ticket_id.in_([t.id for t in tickets]),
                                                  TicketMessage.kind == MessageKind.FORWARD,
                                                  TicketMessage.author_id == principal.user_id).distinct()))
        views = []
        for t in tickets:
            label = QUEUE_LABEL[TicketLevel(t.level)]
            if t.level == TicketLevel.BRANCH and t.branch_id in branches:
                label = f"{label} · {branches[t.branch_id]}"
            handles = can_handle(principal, duty, t)
            view = TicketView(
                t, users.get(t.created_by_id, (None, None))[0], users.get(t.created_by_id, (None, None))[1],
                users.get(t.assigned_to_id, (None, None))[0], counts.get(t.id, 0), handles,
                label, policies.get(t.policy_id), organizations.get(t.organization_id), branches.get(t.branch_id),
                replies.get(t.id), forwards.get(t.id),
                # the same rule as _forwarded_away, from the batch
                read_only=t.id in sent_by_me and not handles and t.created_by_id != principal.user_id,
            )
            if with_actions:
                view.actions = self._actions(principal, duty, t, read_only=view.read_only)
            views.append(view)
        return views

    def _last_forwards(self, tickets: list[Ticket]) -> dict[uuid.UUID, ForwardedBy]:
        """Each ticket's latest forward or escalation (not the creator's first routing)."""
        author = aliased(User)
        ranked = (
            select(TicketMessage.ticket_id, TicketMessage.author_id, TicketMessage.body, TicketMessage.data,
                   TicketMessage.created_at, author.full_name.label("name"), author.role.label("role"),
                   func.row_number().over(partition_by=TicketMessage.ticket_id,
                                          order_by=(TicketMessage.created_at.desc(), TicketMessage.id.desc())).label("n"))
            .outerjoin(author, author.id == TicketMessage.author_id)
            .where(TicketMessage.ticket_id.in_([t.id for t in tickets]), TicketMessage.kind == MessageKind.FORWARD,
                   not_(TicketMessage.data.has_key("initial")))
            .subquery()
        )
        found = {}
        for row in self.session.execute(select(ranked).where(ranked.c.n == 1)).all():
            data = row.data or {}
            found[row.ticket_id] = ForwardedBy(
                by_id=row.author_id, by_name=row.name, by_role=row.role,
                from_queue=(data.get("from") or {}).get("queue"), to_queue=(data.get("to") or {}).get("queue"),
                reason=row.body, escalation=bool(data.get("escalation")), at=row.created_at,
            )
        return found

    def _actions(self, principal: Principal, duty: Duty, ticket: Ticket, *, read_only: bool = False) -> dict[str, Any]:
        """What the caller may do now. The UI shows these; the endpoints check the same rules again.
        read_only: they forwarded it away, so they only follow it (the other actions already need the queue)."""
        handler = can_handle(principal, duty, ticket)
        creator = ticket.created_by_id == principal.user_id
        manager = can_manage(principal, duty, ticket)
        active = ticket.status in ACTIVE_STATUSES
        draft = ticket.status == TicketStatus.AWAITING_CONFIRMATION
        next_level = ESCALATES_TO.get(TicketLevel(ticket.level))
        writable = not draft and ticket.status not in FINISHED and not read_only
        return {
            "confirm": creator and draft,
            "reply": writable,
            "note": writable and reads_notes_of(principal, duty, ticket, ticket.level),
            "statuses": self.allowed_statuses(principal, ticket),
            "cancel": (creator and (draft or active)) or (manager and active),
            "forward": self.destinations(ticket) if handler and active else [],
            "escalate": bool(next_level) and active and (handler or (creator and sla.is_overdue(ticket))),
            "escalate_to": queue_name(self.session, next_level, ticket.branch_id) if next_level else None,
            "take": handler and active and ticket.assigned_to_id != principal.user_id
            and works_queue(duty, ticket.level, ticket.organization_id, ticket.branch_id),
            "assign": manager and active,
            "priority": handler and not draft and ticket.status not in FINISHED,
            "attach": ticket.status not in FINISHED and not read_only,
        }

    def _last_replies(self, tickets: list[Ticket]) -> dict[uuid.UUID, LastReply]:
        author = aliased(User)
        ranked = (
            select(TicketMessage.ticket_id, TicketMessage.body, TicketMessage.created_at, author.full_name.label("name"),
                   func.row_number().over(partition_by=TicketMessage.ticket_id,
                                          order_by=TicketMessage.created_at.desc()).label("n"))
            .join(Ticket, Ticket.id == TicketMessage.ticket_id)
            .outerjoin(author, author.id == TicketMessage.author_id)
            .where(TicketMessage.ticket_id.in_([t.id for t in tickets]), TicketMessage.body.is_not(None),
                   TicketMessage.kind.in_([MessageKind.REPLY, MessageKind.STATUS]),
                   or_(TicketMessage.author_id.is_(None), TicketMessage.author_id != Ticket.created_by_id))
            .subquery()
        )
        rows = self.session.execute(
            select(ranked.c.ticket_id, ranked.c.body, ranked.c.name, ranked.c.created_at).where(ranked.c.n == 1)
        ).all()
        return {ticket_id: LastReply(body, name, at) for ticket_id, body, name, at in rows}

    def thread(self, principal: Principal, ticket_id: uuid.UUID) -> tuple[TicketView, list[TimelineEntry], list[TicketAttachment]]:
        ticket = self.get(principal, ticket_id)
        duty = self.duty(principal)
        author = aliased(User)
        rows = self.session.execute(
            select(TicketMessage, author.full_name, author.role)
            .outerjoin(author, author.id == TicketMessage.author_id)
            .where(TicketMessage.ticket_id == ticket.id).order_by(TicketMessage.created_at, TicketMessage.id)
        ).all()
        staff_side = can_handle(principal, duty, ticket) or can_manage(principal, duty, ticket)
        timeline = []
        for message, name, role in rows:
            if message.kind == MessageKind.NOTE and not reads_notes_of(
                    principal, duty, ticket, (message.data or {}).get("level", ticket.level)):
                continue
            data = dict(message.data or {})
            if message.kind == MessageKind.ASSIGN and not staff_side:
                data.pop("load", None)  # the creator sees who has it, not the staff's workload figures
            timeline.append(TimelineEntry(message, name, role, data))
        attachments = self.session.scalars(
            select(TicketAttachment).where(TicketAttachment.ticket_id == ticket.id).order_by(TicketAttachment.created_at)
        ).all()
        return self.views(principal, [ticket], with_actions=True)[0], timeline, list(attachments)

    # --- attachments -------------------------------------------------------------------------------------------

    def attach(self, principal: Principal, ticket_id: uuid.UUID, *, file: BinaryIO, filename: str) -> TicketAttachment:
        ticket = self.get(principal, ticket_id)
        if ticket.status in FINISHED:
            raise ConflictError("This ticket is closed.", code="INVALID_STATE")
        self._refuse_if_forwarded_away(principal, ticket)
        safe = (filename or "attachment").replace("/", "_").replace("\\", "_")[:300]
        extension = "." + safe.rsplit(".", 1)[-1].lower() if "." in safe else ""
        if extension not in ATTACHMENT_TYPES:
            raise ValidationError(f"Attach PDF, image, text, CSV, Word or Excel files ({', '.join(sorted(ATTACHMENT_TYPES))}).",
                                  code="UNSUPPORTED_FILE_TYPE")
        data = file.read(MAX_ATTACHMENT_BYTES + 1)
        if len(data) > MAX_ATTACHMENT_BYTES:
            raise PayloadTooLargeError(f"Attachments are limited to {MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB.")
        if not data:
            raise ValidationError("The file is empty.")
        attachment_id = uuid.uuid4()
        key = f"tickets/{ticket.organization_id}/{ticket.id}/{attachment_id}{extension}"
        stored = self.storage.put(key, data)
        try:
            attachment = TicketAttachment(
                id=attachment_id, ticket_id=ticket.id, organization_id=ticket.organization_id, filename=safe,
                content_type=ATTACHMENT_TYPES[extension], size_bytes=stored.size, storage_key=key,
                uploaded_by_id=principal.user_id,
            )
            self.session.add(attachment)
            ticket.updated_at = datetime.now(UTC)
            self.session.commit()
        except BaseException:
            self.session.rollback()
            self.storage.delete(key)
            raise
        return attachment

    def attachment(self, principal: Principal, ticket_id: uuid.UUID, attachment_id: uuid.UUID) -> TicketAttachment:
        ticket = self.get(principal, ticket_id)
        attachment = self.session.get(TicketAttachment, attachment_id)
        if attachment is None or attachment.ticket_id != ticket.id:
            raise NotFoundError("Attachment not found.")
        return attachment
