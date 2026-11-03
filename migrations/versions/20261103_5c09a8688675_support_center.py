"""Support Center

Revision ID: 5c09a8688675
Revises: e4b7d2a9c615
Create Date: 2026-11-03 16:00:00.000000

* users: the support_agent role (Governix technical support; no organization).
* support_staff (+ support_staff_organizations): who is on support duty, in which queue, with what skills,
  capacity and availability. support_categories: each organization's ticket categories.
* tickets: number (SUP-000123), team, category_id, issue_type, skills, routing, SLA due times, resolution note,
  confirmation/cancellation times. ticket_messages: kind (reply/note/status/forward/assign) and data.
* Existing tickets keep their queue: platform -> technical, branch/organization -> branch. The status
  waiting_for_response becomes waiting_for_user. Nothing is deleted.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "5c09a8688675"
down_revision: str | Sequence[str] | None = "e4b7d2a9c615"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROLE_SCOPE_OLD = (
    "(role = 'master_admin' AND organization_id IS NULL AND branch_id IS NULL AND department_id IS NULL)"
    " OR (role = 'org_admin' AND organization_id IS NOT NULL AND branch_id IS NULL AND department_id IS NULL)"
    " OR (role = 'branch_manager' AND organization_id IS NOT NULL AND branch_id IS NOT NULL AND department_id IS NULL)"
    " OR (role = 'department_user' AND organization_id IS NOT NULL AND branch_id IS NOT NULL)"
)
ROLE_SCOPE_NEW = (
    "(role IN ('master_admin', 'support_agent') AND organization_id IS NULL AND branch_id IS NULL"
    " AND department_id IS NULL)"
    " OR (role = 'org_admin' AND organization_id IS NOT NULL AND branch_id IS NULL AND department_id IS NULL)"
    " OR (role = 'branch_manager' AND organization_id IS NOT NULL AND branch_id IS NOT NULL AND department_id IS NULL)"
    " OR (role = 'department_user' AND organization_id IS NOT NULL AND branch_id IS NOT NULL)"
)
ACTIVE = "status IN ('open', 'in_progress', 'waiting_for_user', 'waiting_for_team')"


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    ]


def upgrade() -> None:
    op.drop_constraint(op.f("ck_users_role_scope"), "users", type_="check")
    op.create_check_constraint(op.f("ck_users_role_scope"), "users", ROLE_SCOPE_NEW)

    op.create_table(
        "support_categories",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("description", sa.String(300)),
        sa.Column("route", sa.String(20), nullable=False, server_default="auto"),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        *_timestamps(),
        sa.CheckConstraint("route IN ('technical', 'branch', 'policy', 'auto')", name=op.f("ck_support_categories_support_category_route")),
    )
    op.create_index("uq_support_categories_name", "support_categories",
                    ["organization_id", sa.literal_column("lower(name)")], unique=True)

    op.create_table(
        "support_staff",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("level", sa.String(20), nullable=False),
        sa.Column("organization_id", sa.Uuid(), sa.ForeignKey("organizations.id", ondelete="CASCADE")),
        sa.Column("branch_id", sa.Uuid(), sa.ForeignKey("branches.id", ondelete="CASCADE")),
        sa.Column("staff_role", sa.String(20), nullable=False, server_default="agent"),
        sa.Column("skills", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("capacity", sa.Integer(), nullable=False, server_default="10"),
        sa.Column("availability", sa.String(20), nullable=False, server_default="available"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_assigned_at", sa.DateTime(timezone=True)),
        sa.Column("created_by_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        *_timestamps(),
        sa.CheckConstraint(
            "(level = 'platform' AND organization_id IS NULL AND branch_id IS NULL)"
            " OR (level = 'organization' AND organization_id IS NOT NULL AND branch_id IS NULL)"
            " OR (level = 'branch' AND organization_id IS NOT NULL AND branch_id IS NOT NULL)",
            name=op.f("ck_support_staff_support_staff_scope"),
        ),
        sa.CheckConstraint("capacity BETWEEN 1 AND 500", name=op.f("ck_support_staff_support_staff_capacity")),
        sa.CheckConstraint("availability IN ('available', 'busy', 'away', 'offline')",
                           name=op.f("ck_support_staff_support_staff_availability")),
        sa.CheckConstraint("staff_role IN ('agent', 'lead')", name=op.f("ck_support_staff_support_staff_role")),
    )
    op.create_index("ix_support_staff_queue", "support_staff", ["level", "organization_id", "branch_id"])
    op.create_index("ix_support_staff_organization", "support_staff", ["organization_id"])
    op.create_index("ix_support_staff_branch", "support_staff", ["branch_id"])
    op.create_index("ix_support_staff_created_by", "support_staff", ["created_by_id"])

    op.create_table(
        "support_staff_organizations",
        sa.Column("staff_id", sa.Uuid(), sa.ForeignKey("support_staff.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), sa.ForeignKey("organizations.id", ondelete="CASCADE"),
                  primary_key=True),
    )
    op.create_index("ix_support_staff_organizations_org", "support_staff_organizations", ["organization_id"])

    # --- tickets ------------------------------------------------------------------------------------------
    op.add_column("tickets", sa.Column("number", sa.BigInteger(), sa.Identity(), nullable=False))
    op.create_unique_constraint(op.f("uq_tickets_number"), "tickets", ["number"])
    op.add_column("tickets", sa.Column("team", sa.String(20), nullable=False, server_default="branch"))
    op.execute("UPDATE tickets SET team = 'technical' WHERE level = 'platform'")
    op.add_column("tickets", sa.Column(
        "category_id", sa.Uuid(), sa.ForeignKey("support_categories.id", ondelete="SET NULL",
                                                name=op.f("fk_tickets_category_id_support_categories"))))
    op.add_column("tickets", sa.Column("issue_type", sa.String(80)))
    op.add_column("tickets", sa.Column("skills", postgresql.JSONB(), nullable=False,
                                       server_default=sa.text("'[]'::jsonb")))
    op.add_column("tickets", sa.Column("routing", postgresql.JSONB()))
    for column in ("confirmed_at", "first_response_at", "response_due_at", "due_at", "cancelled_at"):
        op.add_column("tickets", sa.Column(column, sa.DateTime(timezone=True)))
    op.add_column("tickets", sa.Column("resolution_note", sa.Text()))
    op.add_column("tickets", sa.Column("reopen_count", sa.Integer(), nullable=False, server_default="0"))
    # Tickets raised before the Support Center were routed when raised.
    op.execute("UPDATE tickets SET confirmed_at = created_at")
    op.execute("UPDATE tickets SET status = 'waiting_for_user' WHERE status = 'waiting_for_response'")
    op.create_check_constraint(op.f("ck_tickets_ticket_team"), "tickets", "team IN ('technical', 'branch')")
    op.create_check_constraint(
        op.f("ck_tickets_ticket_status"), "tickets",
        "status IN ('awaiting_confirmation', 'open', 'in_progress', 'waiting_for_user', 'waiting_for_team',"
        " 'resolved', 'closed', 'cancelled')",
    )
    op.create_index("ix_tickets_assignee_status", "tickets", ["assigned_to_id", "status"])
    op.create_index("ix_tickets_category", "tickets", ["category_id"])
    op.create_index("ix_tickets_platform_queue", "tickets", ["level", "status"],
                    postgresql_where=sa.text("level = 'platform'"))
    op.create_index("ix_tickets_due", "tickets", ["due_at"], postgresql_where=sa.text(ACTIVE))

    op.add_column("ticket_messages", sa.Column("kind", sa.String(20), nullable=False, server_default="reply"))
    op.add_column("ticket_messages", sa.Column("data", postgresql.JSONB(), nullable=False,
                                               server_default=sa.text("'{}'::jsonb")))
    op.execute("UPDATE ticket_messages SET kind = 'status' WHERE status_change IS NOT NULL AND body IS NULL")
    op.execute("UPDATE ticket_messages SET status_change = 'waiting_for_user' "
               "WHERE status_change = 'waiting_for_response'")


def downgrade() -> None:
    op.execute("UPDATE ticket_messages SET status_change = 'waiting_for_response' "
               "WHERE status_change IN ('waiting_for_user', 'waiting_for_team')")
    op.drop_column("ticket_messages", "data")
    op.drop_column("ticket_messages", "kind")

    op.drop_index("ix_tickets_due", table_name="tickets")
    op.drop_index("ix_tickets_platform_queue", table_name="tickets")
    op.drop_index("ix_tickets_category", table_name="tickets")
    op.drop_index("ix_tickets_assignee_status", table_name="tickets")
    op.drop_constraint(op.f("ck_tickets_ticket_status"), "tickets", type_="check")
    op.drop_constraint(op.f("ck_tickets_ticket_team"), "tickets", type_="check")
    # The old statuses: a draft never reached anyone, and a cancelled ticket was closed.
    op.execute("DELETE FROM tickets WHERE status = 'awaiting_confirmation'")
    op.execute("UPDATE tickets SET status = 'closed' WHERE status = 'cancelled'")
    op.execute("UPDATE tickets SET status = 'waiting_for_response' "
               "WHERE status IN ('waiting_for_user', 'waiting_for_team')")
    for column in ("reopen_count", "resolution_note", "cancelled_at", "due_at", "response_due_at",
                   "first_response_at", "confirmed_at", "routing", "skills", "issue_type"):
        op.drop_column("tickets", column)
    op.drop_constraint(op.f("fk_tickets_category_id_support_categories"), "tickets", type_="foreignkey")
    op.drop_column("tickets", "category_id")
    op.drop_column("tickets", "team")
    op.drop_constraint(op.f("uq_tickets_number"), "tickets", type_="unique")
    op.drop_column("tickets", "number")

    op.drop_index("ix_support_staff_organizations_org", table_name="support_staff_organizations")
    op.drop_table("support_staff_organizations")
    for index in ("ix_support_staff_created_by", "ix_support_staff_branch", "ix_support_staff_organization",
                  "ix_support_staff_queue"):
        op.drop_index(index, table_name="support_staff")
    op.drop_table("support_staff")
    op.drop_index("uq_support_categories_name", table_name="support_categories")
    op.drop_table("support_categories")

    # Fails while a support_agent account exists: delete those accounts first.
    op.drop_constraint(op.f("ck_users_role_scope"), "users", type_="check")
    op.create_check_constraint(op.f("ck_users_role_scope"), "users", ROLE_SCOPE_OLD)
