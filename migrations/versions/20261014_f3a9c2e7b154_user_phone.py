"""User phone number

Revision ID: f3a9c2e7b154
Revises: d5f1b8c3a6e2
Create Date: 2026-10-14 12:00:00.000000

* users.phone: optional contact number, set when the Master Admin adds an
  organization admin.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f3a9c2e7b154"
down_revision: str | Sequence[str] | None = "d5f1b8c3a6e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("phone", sa.String(50), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "phone")
