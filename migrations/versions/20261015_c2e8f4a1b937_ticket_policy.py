"""Ticket policy

Revision ID: c2e8f4a1b937
Revises: a7c4e9d2f318
Create Date: 2026-10-15 10:00:00.000000

* tickets.policy_id: the policy a complaint is about (Complaints > Related policy). NULL for a
  general question; set to NULL if the policy is deleted.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c2e8f4a1b937"
down_revision: str | Sequence[str] | None = "a7c4e9d2f318"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("tickets", sa.Column("policy_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_tickets_policy_id_policies", "tickets", "policies", ["policy_id"], ["id"], ondelete="SET NULL"
    )
    op.create_index("ix_tickets_policy", "tickets", ["policy_id"])


def downgrade() -> None:
    op.drop_index("ix_tickets_policy", table_name="tickets")
    op.drop_constraint("fk_tickets_policy_id_policies", "tickets", type_="foreignkey")
    op.drop_column("tickets", "policy_id")
