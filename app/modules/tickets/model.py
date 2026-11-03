"""Support Center: tickets, their timeline, support staff and categories.

Three queues handle tickets (TicketLevel x TicketTeam):

    branch        Branch Support of one branch          staff added by its Branch Manager (or the Org Admin)
    organization  Head Office Support of one bank       staff added by the Organization Admin
    platform      Governix Technical Support            staff added by the Master Admin

A ticket is written, given an AI routing recommendation, and waits (AWAITING_CONFIRMATION) until its
creator confirms or switches the destination. It is then OPEN in that queue and assigned by workload.
Every reply, internal note, status change, forward and assignment is a TicketMessage, the timeline.
"""

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, Identity, Index, Integer, String, Text, func,
)
from sqlalchemy import text as sql
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, TimestampMixin, UUIDPrimaryKeyMixin


class TicketKind(StrEnum):
    COMPLAINT = "complaint"  # legacy: raised before the Support Center; handled as Branch Support
    SUPPORT = "support"


class TicketLevel(StrEnum):
    BRANCH = "branch"
    ORGANIZATION = "organization"
    PLATFORM = "platform"


class TicketTeam(StrEnum):
    TECHNICAL = "technical"  # platform level
    BRANCH = "branch"  # branch or Head Office (organization) level


class TicketStatus(StrEnum):
    # NEW and CLASSIFICATION_PENDING happen inside the create request (the AI call); they are never stored.
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    WAITING_FOR_USER = "waiting_for_user"
    WAITING_FOR_TEAM = "waiting_for_team"
    RESOLVED = "resolved"
    CLOSED = "closed"
    CANCELLED = "cancelled"


ACTIVE_STATUSES = frozenset({
    TicketStatus.OPEN, TicketStatus.IN_PROGRESS, TicketStatus.WAITING_FOR_USER, TicketStatus.WAITING_FOR_TEAM,
})
# Workload counts what a staff member must still act on; waiting for the user is not their move, but it is still theirs.
WORKLOAD_STATUSES = ACTIVE_STATUSES


class TicketPriority(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    URGENT = "urgent"


class MessageKind(StrEnum):
    REPLY = "reply"  # visible to the creator
    NOTE = "note"  # internal: only the staff side of the level it was written at
    STATUS = "status"
    FORWARD = "forward"  # routing confirmed, forwarded, escalated, sent back
    ASSIGN = "assign"


class StaffAvailability(StrEnum):
    AVAILABLE = "available"
    BUSY = "busy"  # keeps their tickets, gets no new ones
    AWAY = "away"
    OFFLINE = "offline"


class StaffRole(StrEnum):
    AGENT = "agent"
    LEAD = "lead"  # also assigns and reassigns within the queue, like its manager


class CategoryRoute(StrEnum):
    """Where a category points before the AI reads the ticket."""

    TECHNICAL = "technical"
    BRANCH = "branch"
    POLICY = "policy"  # questions about what a document says: the AI Assistant / policy workflow
    AUTO = "auto"  # no default: the AI recommends, or the creator chooses


class Ticket(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "tickets"

    # Shown as SUP-000123.
    number: Mapped[int] = mapped_column(BigInteger, Identity(), unique=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="RESTRICT"))
    # Where it came from (the creator's branch); with level=branch also whose queue it is in.
    branch_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("branches.id", ondelete="RESTRICT"))
    kind: Mapped[str] = mapped_column(String(20))
    level: Mapped[str] = mapped_column(String(20))
    team: Mapped[str] = mapped_column(String(20), default=TicketTeam.BRANCH)
    category: Mapped[str | None] = mapped_column(String(100))
    category_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("support_categories.id", ondelete="SET NULL"))
    issue_type: Mapped[str | None] = mapped_column(String(80))
    # Skill tags the ticket needs (from the AI, chosen among the staff's own tags); used by auto-assignment.
    skills: Mapped[list[str]] = mapped_column(JSONB, default=list)
    # The routing recommendation shown before confirmation: team, priority, confidence, reason, source.
    routing: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # The policy a ticket is about (one the creator can read); NULL for a general question.
    policy_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("policies.id", ondelete="SET NULL"))
    subject: Mapped[str] = mapped_column(String(300))
    description: Mapped[str] = mapped_column(Text)
    priority: Mapped[str] = mapped_column(String(10), default=TicketPriority.MEDIUM)
    status: Mapped[str] = mapped_column(String(30), default=TicketStatus.AWAITING_CONFIRMATION)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    assigned_to_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_response_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # SLA: first staff reply by response_due_at, resolution by due_at (set on confirmation, from the priority).
    response_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolution_note: Mapped[str | None] = mapped_column(Text)
    reopen_count: Mapped[int] = mapped_column(Integer, default=0)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_tickets_queue", "organization_id", "level", "status"),
        Index("ix_tickets_branch_queue", "branch_id", "level", "status"),
        Index("ix_tickets_created_by", "created_by_id", "created_at"),
        Index("ix_tickets_policy", "policy_id"),  # ON DELETE SET NULL lookup
        Index("ix_tickets_assignee_status", "assigned_to_id", "status"),  # workload
        Index("ix_tickets_category", "category_id"),  # ON DELETE SET NULL lookup
        Index("ix_tickets_platform_queue", "level", "status", postgresql_where=sql("level = 'platform'")),
        Index("ix_tickets_due", "due_at", postgresql_where=sql(
            "status IN ('open', 'in_progress', 'waiting_for_user', 'waiting_for_team')")),
        CheckConstraint("team IN ('technical', 'branch')", name="ticket_team"),
        CheckConstraint(
            "status IN ('awaiting_confirmation', 'open', 'in_progress', 'waiting_for_user', 'waiting_for_team',"
            " 'resolved', 'closed', 'cancelled')",
            name="ticket_status",
        ),
    )


class TicketMessage(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One timeline entry: a reply, an internal note, a status change, a forward or an assignment."""

    __tablename__ = "ticket_messages"

    # Set in Python, not by the database: entries written in one transaction (a forward and the assignment it
    # leads to) would otherwise share now() and lose their order on the timeline.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), default=lambda: datetime.now(UTC), nullable=False
    )

    ticket_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tickets.id", ondelete="CASCADE"))
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="RESTRICT"))
    author_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    kind: Mapped[str] = mapped_column(String(20), default=MessageKind.REPLY)
    body: Mapped[str | None] = mapped_column(Text)
    status_change: Mapped[str | None] = mapped_column(String(30))
    # forward: {"from": {...}, "to": {...}, "reason", "confirmed", "escalation"}; assign: {"to", "auto", ...};
    # note: {"level", "team"} (who may read it).
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    __table_args__ = (Index("ix_ticket_messages_ticket", "ticket_id", "created_at"),)


class TicketAttachment(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "ticket_attachments"

    ticket_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tickets.id", ondelete="CASCADE"))
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="RESTRICT"))
    message_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ticket_messages.id", ondelete="CASCADE"))
    filename: Mapped[str] = mapped_column(String(300))
    content_type: Mapped[str] = mapped_column(String(100))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    storage_key: Mapped[str] = mapped_column(String(500))
    uploaded_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))

    __table_args__ = (
        Index("ix_ticket_attachments_ticket", "ticket_id"),
        Index("ix_ticket_attachments_message", "message_id"),  # ON DELETE CASCADE lookup
    )


class SupportStaff(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A person on support duty: which queue they work, their skills, capacity and availability.

    A profile on an existing account (bank staff keep their normal work); Governix's technical staff
    are support_agent accounts. The level fixes the team: platform -> technical, otherwise branch.
    """

    __tablename__ = "support_staff"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True)
    level: Mapped[str] = mapped_column(String(20))
    organization_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"))
    branch_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("branches.id", ondelete="CASCADE"))
    staff_role: Mapped[str] = mapped_column(String(20), default=StaffRole.AGENT)
    skills: Mapped[list[str]] = mapped_column(JSONB, default=list)
    # Open tickets at which they are full (auto-assignment skips them).
    capacity: Mapped[int] = mapped_column(Integer, default=10)
    availability: Mapped[str] = mapped_column(String(20), default=StaffAvailability.AVAILABLE)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Tie-break for auto-assignment: the one who waited longest for a ticket goes first.
    last_assigned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))

    __table_args__ = (
        CheckConstraint(
            "(level = 'platform' AND organization_id IS NULL AND branch_id IS NULL)"
            " OR (level = 'organization' AND organization_id IS NOT NULL AND branch_id IS NULL)"
            " OR (level = 'branch' AND organization_id IS NOT NULL AND branch_id IS NOT NULL)",
            name="support_staff_scope",
        ),
        CheckConstraint("capacity BETWEEN 1 AND 500", name="support_staff_capacity"),
        CheckConstraint("availability IN ('available', 'busy', 'away', 'offline')", name="support_staff_availability"),
        CheckConstraint("staff_role IN ('agent', 'lead')", name="support_staff_role"),
        Index("ix_support_staff_queue", "level", "organization_id", "branch_id"),
        Index("ix_support_staff_organization", "organization_id"),  # ON DELETE CASCADE lookup
        Index("ix_support_staff_branch", "branch_id"),  # ON DELETE CASCADE lookup
        Index("ix_support_staff_created_by", "created_by_id"),  # ON DELETE SET NULL lookup
    )


class SupportStaffOrganization(Base):
    """The banks a platform staff member serves. None listed: every bank."""

    __tablename__ = "support_staff_organizations"

    staff_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("support_staff.id", ondelete="CASCADE"), primary_key=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True
    )

    __table_args__ = (Index("ix_support_staff_organizations_org", "organization_id"),)


class SupportCategory(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """An organization's ticket categories. An organization with none uses DEFAULT_CATEGORIES."""

    __tablename__ = "support_categories"

    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(100))
    description: Mapped[str | None] = mapped_column(String(300))
    route: Mapped[str] = mapped_column(String(20), default=CategoryRoute.AUTO)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    __table_args__ = (
        Index("uq_support_categories_name", "organization_id", func.lower(name), unique=True),
        CheckConstraint("route IN ('technical', 'branch', 'policy', 'auto')", name="support_category_route"),
    )
