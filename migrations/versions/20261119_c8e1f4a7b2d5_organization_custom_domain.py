"""Organization custom domain

Revision ID: c8e1f4a7b2d5
Revises: b7c4d2e9f1a3
Create Date: 2026-11-19 10:00:00.000000

* organizations.custom_domain: the bank's own address for its portal (portal.bank.com), lower case, unique.
  Organizations > Nginx serves it on deploy. Nullable, no backfill: existing organizations have none.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c8e1f4a7b2d5"
down_revision: str | Sequence[str] | None = "b7c4d2e9f1a3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("organizations", sa.Column("custom_domain", sa.String(253), nullable=True))
    op.create_unique_constraint("uq_organizations_custom_domain", "organizations", ["custom_domain"])


def downgrade() -> None:
    op.drop_constraint("uq_organizations_custom_domain", "organizations", type_="unique")
    op.drop_column("organizations", "custom_domain")
