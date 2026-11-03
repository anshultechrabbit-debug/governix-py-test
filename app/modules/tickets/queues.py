"""Queues: who works a ticket, who manages that work, and who may see it.

    queue                     works it (members)                      manages it
    Branch Support (branch)   active staff profiles of that branch     the branch's Branch Manager, the Org Admin
    Head Office (organization) active staff profiles of the bank       the Org Admin
    Technical (platform)      active platform profiles serving the bank the Master Admin

A lead member also manages their queue (assigns, reassigns). Every rule keeps to the ticket's own
organization: nothing here reaches another bank's tickets or staff.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import ColumnElement, and_, exists, false, not_, or_, select
from sqlalchemy.orm import Session

from app.modules.auth.permissions import Principal, Role
from app.modules.branches.model import Branch
from app.modules.tickets.model import (
    StaffRole,
    SupportStaff,
    SupportStaffOrganization,
    Ticket,
    TicketLevel,
    TicketStatus,
    TicketTeam,
)
from app.modules.users.model import User

LEVEL_TEAM = {
    TicketLevel.BRANCH: TicketTeam.BRANCH,
    TicketLevel.ORGANIZATION: TicketTeam.BRANCH,
    TicketLevel.PLATFORM: TicketTeam.TECHNICAL,
}
# Escalation goes one level up.
ESCALATES_TO = {TicketLevel.BRANCH: TicketLevel.ORGANIZATION, TicketLevel.ORGANIZATION: TicketLevel.PLATFORM}
QUEUE_LABEL = {
    TicketLevel.BRANCH: "Branch Support",
    TicketLevel.ORGANIZATION: "Head Office Support",
    TicketLevel.PLATFORM: "Technical Support",
}


@dataclass(frozen=True)
class Duty:
    """The caller's support-staff profile, loaded once per request."""

    staff: SupportStaff | None
    # Platform staff only: the banks they serve (empty: every bank).
    serves: frozenset[uuid.UUID] = frozenset()

    @property
    def active(self) -> bool:
        return self.staff is not None and self.staff.is_active

    @property
    def lead(self) -> bool:
        return self.active and self.staff.staff_role == StaffRole.LEAD


def load_duty(session: Session, principal: Principal) -> Duty:
    staff = session.scalar(select(SupportStaff).where(SupportStaff.user_id == principal.user_id))
    serves: frozenset[uuid.UUID] = frozenset()
    if staff is not None and staff.level == TicketLevel.PLATFORM:
        serves = frozenset(session.scalars(
            select(SupportStaffOrganization.organization_id).where(SupportStaffOrganization.staff_id == staff.id)
        ))
    return Duty(staff, serves)


def serves_organization(staff: SupportStaff, serves: frozenset[uuid.UUID], organization_id: uuid.UUID) -> bool:
    return not serves or organization_id in serves


# --- membership and management, for one ticket -------------------------------------------------------------

def works_queue(duty: Duty, level: str, organization_id: uuid.UUID, branch_id: uuid.UUID | None) -> bool:
    """Whether the caller is an active member of the (level, organization, branch) queue."""
    if not duty.active or duty.staff.level != level:
        return False
    staff = duty.staff
    if level == TicketLevel.PLATFORM:
        return serves_organization(staff, duty.serves, organization_id)
    if staff.organization_id != organization_id:
        return False
    return level == TicketLevel.ORGANIZATION or staff.branch_id == branch_id


def manages_queue(principal: Principal, duty: Duty, level: str, organization_id: uuid.UUID,
                  branch_id: uuid.UUID | None) -> bool:
    """Whether the caller manages that queue: its manager, or a lead member."""
    if duty.lead and works_queue(duty, level, organization_id, branch_id):
        return True
    if level == TicketLevel.PLATFORM:
        return principal.role is Role.MASTER_ADMIN
    if principal.organization_id != organization_id:
        return False
    if principal.role is Role.ORG_ADMIN:
        return True  # Head Office, and oversight of every branch's queue
    return (level == TicketLevel.BRANCH and principal.role is Role.BRANCH_MANAGER
            and branch_id is not None and principal.branch_id == branch_id)


def can_handle(principal: Principal, duty: Duty, ticket: Ticket) -> bool:
    """Works or manages the ticket's current queue (or holds it). Reply as staff, change status, forward."""
    if ticket.status == TicketStatus.AWAITING_CONFIRMATION:
        return False
    if ticket.assigned_to_id == principal.user_id:
        return True
    return (works_queue(duty, ticket.level, ticket.organization_id, ticket.branch_id)
            or manages_queue(principal, duty, ticket.level, ticket.organization_id, ticket.branch_id))


def can_manage(principal: Principal, duty: Duty, ticket: Ticket) -> bool:
    """Assign to someone else, reassign, cancel."""
    return ticket.status != TicketStatus.AWAITING_CONFIRMATION and manages_queue(
        principal, duty, ticket.level, ticket.organization_id, ticket.branch_id)


def reads_notes_of(principal: Principal, duty: Duty, ticket: Ticket, level: str) -> bool:
    """Internal notes written at `level` are for that level's staff side only, never the creator."""
    if ticket.created_by_id == principal.user_id and not can_handle(principal, duty, ticket):
        return False
    return (works_queue(duty, level, ticket.organization_id, ticket.branch_id)
            or manages_queue(principal, duty, level, ticket.organization_id, ticket.branch_id)
            or (level == ticket.level and ticket.assigned_to_id == principal.user_id))


# --- the same rules as SQL, for lists ----------------------------------------------------------------------

def _member_clause(duty: Duty) -> ColumnElement[bool]:
    if not duty.active:
        return false()
    staff = duty.staff
    if staff.level == TicketLevel.PLATFORM:
        clause = Ticket.level == TicketLevel.PLATFORM
        return and_(clause, Ticket.organization_id.in_(duty.serves)) if duty.serves else clause
    same = and_(Ticket.level == staff.level, Ticket.organization_id == staff.organization_id)
    return and_(same, Ticket.branch_id == staff.branch_id) if staff.level == TicketLevel.BRANCH else same


def handling_clause(principal: Principal, duty: Duty) -> ColumnElement[bool]:
    """Tickets in the queues the caller works or manages (their "Queue"), plus those assigned to them."""
    clauses = [Ticket.assigned_to_id == principal.user_id, _member_clause(duty)]
    if principal.role is Role.MASTER_ADMIN:
        clauses.append(Ticket.level == TicketLevel.PLATFORM)
    elif principal.role is Role.ORG_ADMIN:
        clauses.append(and_(Ticket.organization_id == principal.organization_id,
                            Ticket.level == TicketLevel.ORGANIZATION))
    elif principal.role is Role.BRANCH_MANAGER:
        clauses.append(and_(Ticket.organization_id == principal.organization_id,
                            Ticket.branch_id == principal.branch_id, Ticket.level == TicketLevel.BRANCH))
    return and_(Ticket.status != TicketStatus.AWAITING_CONFIRMATION, or_(*clauses))


def visible_clause(principal: Principal, duty: Duty) -> ColumnElement[bool]:
    """Everything the caller may open: their own tickets (drafts too), their queues, and what they oversee.

    A Branch Manager follows every ticket raised in their branch wherever it went; an Org Admin, every
    ticket of the bank. Neither reads another level's internal notes (reads_notes_of).
    """
    oversight = [handling_clause(principal, duty)]
    if principal.role is Role.ORG_ADMIN:
        oversight.append(Ticket.organization_id == principal.organization_id)
    elif principal.role is Role.BRANCH_MANAGER:
        oversight.append(and_(Ticket.organization_id == principal.organization_id,
                              Ticket.branch_id == principal.branch_id))
    return or_(
        Ticket.created_by_id == principal.user_id,
        and_(Ticket.status != TicketStatus.AWAITING_CONFIRMATION, or_(*oversight)),
    )


# --- who to tell -------------------------------------------------------------------------------------------

def queue_staff_query(level: str, organization_id: uuid.UUID, branch_id: uuid.UUID | None):
    """Active profiles (with active, live accounts) of a queue. Platform: those serving the organization."""
    query = (
        select(SupportStaff).join(User, User.id == SupportStaff.user_id)
        .where(SupportStaff.is_active, SupportStaff.level == level, User.is_active, User.deleted_at.is_(None))
    )
    if level == TicketLevel.PLATFORM:
        serves_any = not_(exists().where(SupportStaffOrganization.staff_id == SupportStaff.id))
        serves_this = exists().where(SupportStaffOrganization.staff_id == SupportStaff.id,
                                     SupportStaffOrganization.organization_id == organization_id)
        return query.where(or_(serves_any, serves_this))
    query = query.where(SupportStaff.organization_id == organization_id)
    return query.where(SupportStaff.branch_id == branch_id) if level == TicketLevel.BRANCH else query


def manager_ids(session: Session, level: str, organization_id: uuid.UUID, branch_id: uuid.UUID | None) -> set[uuid.UUID]:
    """The queue's managers (Branch Manager / Org Admin / Master Admin) and its leads."""
    query = select(User.id).where(User.is_active, User.deleted_at.is_(None))
    if level == TicketLevel.BRANCH:
        query = query.where(User.role == Role.BRANCH_MANAGER, User.organization_id == organization_id,
                            User.branch_id == branch_id)
    elif level == TicketLevel.ORGANIZATION:
        query = query.where(User.role == Role.ORG_ADMIN, User.organization_id == organization_id)
    else:
        query = query.where(User.role == Role.MASTER_ADMIN)
    leads = select(SupportStaff.user_id).where(
        SupportStaff.id.in_(queue_staff_query(level, organization_id, branch_id)
                            .where(SupportStaff.staff_role == StaffRole.LEAD).with_only_columns(SupportStaff.id))
    )
    return set(session.scalars(query)) | set(session.scalars(leads))


def queue_name(session: Session, level: str, branch_id: uuid.UUID | None) -> str:
    if level == TicketLevel.BRANCH and branch_id is not None:
        branch = session.get(Branch, branch_id)
        if branch is not None:
            return f"{QUEUE_LABEL[TicketLevel.BRANCH]} · {branch.name}"
    return QUEUE_LABEL[TicketLevel(level)]
