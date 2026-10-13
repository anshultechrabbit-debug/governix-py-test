"""Organization access port and brand font

Revision ID: d5f1b8c3a6e2
Revises: e1a7c3b95d42
Create Date: 2026-10-13 12:00:00.000000

* organizations.allowed_port: the tenant's dedicated access port (7001-7100 by
  default, TENANT_PORT_MIN/MAX), unique across organizations. Recorded and
  reserved here; serving the tenant on it is nginx's job.
* organizations.brand_font: the Google Font applied across the tenant portal.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d5f1b8c3a6e2"
down_revision: str | Sequence[str] | None = "e1a7c3b95d42"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("organizations", sa.Column("allowed_port", sa.Integer(), nullable=True))
    op.add_column("organizations", sa.Column("brand_font", sa.String(100), nullable=True))
    op.create_unique_constraint("uq_organizations_allowed_port", "organizations", ["allowed_port"])


def downgrade() -> None:
    op.drop_constraint("uq_organizations_allowed_port", "organizations", type_="unique")
    op.drop_column("organizations", "brand_font")
    op.drop_column("organizations", "allowed_port")
