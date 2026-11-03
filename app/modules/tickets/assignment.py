"""Workload-based assignment.

Eligible: active profiles of the ticket's queue whose accounts are active, who are available, have room
under their capacity, and are not the ticket's creator. Among them, those with a skill the ticket needs
(when any has one). The one with the lowest normalized workload (open tickets / capacity) gets it; ties go
to fewer open tickets, then to whoever waited longest since their last assignment, then by user id, so
the same state always gives the same answer.

The queue's profile rows are locked (FOR UPDATE, in id order) for the rest of the transaction, so two
tickets confirmed at once in one queue are assigned one after the other, each counting the other.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.modules.tickets.model import WORKLOAD_STATUSES, StaffAvailability, SupportStaff, Ticket
from app.modules.tickets.queues import queue_staff_query

_NEVER = datetime.min.replace(tzinfo=UTC)


@dataclass(frozen=True)
class Candidate:
    staff: SupportStaff
    open_tickets: int

    @property
    def load(self) -> float:
        return self.open_tickets / self.staff.capacity

    def explain(self) -> dict:
        return {"open_tickets": self.open_tickets, "capacity": self.staff.capacity, "load": round(self.load, 3)}


def open_counts(session: Session, user_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    if not user_ids:
        return {}
    return dict(session.execute(
        select(Ticket.assigned_to_id, func.count())
        .where(Ticket.assigned_to_id.in_(user_ids), Ticket.status.in_(list(WORKLOAD_STATUSES)))
        .group_by(Ticket.assigned_to_id)
    ).all())


def candidates(session: Session, ticket: Ticket, *, lock: bool,
               exclude: frozenset[uuid.UUID] = frozenset()) -> list[Candidate]:
    """Eligible staff for the ticket's queue, best first."""
    query = queue_staff_query(ticket.level, ticket.organization_id, ticket.branch_id).order_by(SupportStaff.id)
    if lock:
        query = query.with_for_update(of=SupportStaff)
    staff = list(session.scalars(query))
    counts = open_counts(session, [s.user_id for s in staff])
    eligible = [
        Candidate(s, counts.get(s.user_id, 0)) for s in staff
        if s.availability == StaffAvailability.AVAILABLE and s.user_id != ticket.created_by_id
        and s.user_id not in exclude
        and counts.get(s.user_id, 0) < s.capacity
    ]
    needed = {skill.lower() for skill in ticket.skills or []}
    if needed:
        skilled = [c for c in eligible if needed & {skill.lower() for skill in c.staff.skills or []}]
        eligible = skilled or eligible
    return sorted(eligible, key=lambda c: (
        c.load, c.open_tickets, c.staff.last_assigned_at or _NEVER, str(c.staff.user_id),
    ))


def best(session: Session, ticket: Ticket, *, lock: bool = True,
         exclude: frozenset[uuid.UUID] = frozenset()) -> Candidate | None:
    found = candidates(session, ticket, lock=lock, exclude=exclude)
    return found[0] if found else None
