"""Branch address

Revision ID: a7c4e9d2f318
Revises: f3a9c2e7b154
Create Date: 2026-10-14 18:00:00.000000

* branches.street_address, branches.state, branches.postal_code: where the branch is.
  Required for new branches by the API; existing branches stay empty until edited.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a7c4e9d2f318"
down_revision: str | Sequence[str] | None = "f3a9c2e7b154"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("branches", sa.Column("street_address", sa.String(300), nullable=True))
    op.add_column("branches", sa.Column("state", sa.String(100), nullable=True))
    op.add_column("branches", sa.Column("postal_code", sa.String(20), nullable=True))


def downgrade() -> None:
    op.drop_column("branches", "postal_code")
    op.drop_column("branches", "state")
    op.drop_column("branches", "street_address")
