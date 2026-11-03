"""SLA: due times from the priority, overdue flags, and the sweep that warns before and after a breach.

The clock starts when the creator confirms the routing, and keeps running across forwards (the creator
waits the same). A first staff reply meets the response target; resolving meets the resolution target.
"""

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.modules.notifications.model import NotificationType
from app.modules.notifications.service import notify
from app.modules.tickets.model import ACTIVE_STATUSES, MessageKind, Ticket, TicketMessage, TicketStatus
from app.modules.tickets.queues import manager_ids

logger = logging.getLogger(__name__)
SYSTEM = None  # author of what the sweep writes


def hours_for(settings: Settings, priority: str) -> tuple[float, float]:
    response, resolution = (settings.SUPPORT_SLA_HOURS.get(priority) or settings.SUPPORT_SLA_HOURS["medium"])[:2]
    return float(response), float(resolution)


def set_due(settings: Settings, ticket: Ticket) -> None:
    start = ticket.confirmed_at or datetime.now(UTC)
    response, resolution = hours_for(settings, ticket.priority)
    ticket.response_due_at = start + timedelta(hours=response)
    ticket.due_at = start + timedelta(hours=resolution)


def is_overdue(ticket: Ticket, now: datetime | None = None) -> bool:
    now = now or datetime.now(UTC)
    return ticket.status in ACTIVE_STATUSES and ticket.due_at is not None and ticket.due_at <= now


def response_overdue(ticket: Ticket, now: datetime | None = None) -> bool:
    now = now or datetime.now(UTC)
    return (ticket.status in ACTIVE_STATUSES and ticket.first_response_at is None
            and ticket.response_due_at is not None and ticket.response_due_at <= now)


def sweep(session: Session, settings: Settings, now: datetime | None = None) -> dict[str, int]:
    """Warn the assignee (or the queue's managers) of tickets nearing or past their due time, once each;
    close resolved tickets the creator left for SUPPORT_AUTO_CLOSE_DAYS. Safe to run repeatedly."""
    now = now or datetime.now(UTC)
    counts = {"warned": 0, "breached": 0, "closed": 0}
    active = session.scalars(
        select(Ticket).where(Ticket.status.in_(list(ACTIVE_STATUSES)), Ticket.due_at.is_not(None))
    ).all()
    for ticket in active:
        window = (ticket.due_at - (ticket.confirmed_at or ticket.created_at)).total_seconds()
        left = (ticket.due_at - now).total_seconds()
        if left <= 0:
            stage, title = "breached", f"Overdue: SUP-{ticket.number:06d} {ticket.subject}"
        elif window > 0 and left <= window * settings.SUPPORT_SLA_WARN_FRACTION:
            stage, title = "warned", f"Due soon: SUP-{ticket.number:06d} {ticket.subject}"
        else:
            continue
        recipients = {ticket.assigned_to_id} if ticket.assigned_to_id else set()
        if stage == "breached" or not recipients:
            recipients |= manager_ids(session, ticket.level, ticket.organization_id, ticket.branch_id)
        counts[stage] += notify(
            session, recipients, NotificationType.TICKET_SLA, title,
            body=f"Resolution due {ticket.due_at:%Y-%m-%d %H:%M} UTC.", organization_id=ticket.organization_id,
            link=f"/support/{ticket.id}", data={"ticket_id": str(ticket.id), "sla": stage},
            # Once per ticket, stage and due time: a new due time (priority changed) warns again.
            dedupe_key=f"sla:{ticket.id}:{stage}:{ticket.due_at:%Y%m%d%H%M}",
        )

    cutoff = now - timedelta(days=settings.SUPPORT_AUTO_CLOSE_DAYS)
    stale = session.scalars(
        select(Ticket).where(Ticket.status == TicketStatus.RESOLVED, Ticket.resolved_at <= cutoff)
        .with_for_update(skip_locked=True)
    ).all()
    for ticket in stale:
        ticket.status, ticket.closed_at = TicketStatus.CLOSED, now
        session.add(TicketMessage(
            ticket_id=ticket.id, organization_id=ticket.organization_id, author_id=SYSTEM, kind=MessageKind.STATUS,
            status_change=TicketStatus.CLOSED, data={"auto": True},
            body=f"Closed automatically {settings.SUPPORT_AUTO_CLOSE_DAYS} days after it was resolved.",
        ))
        counts["closed"] += 1
    return counts
