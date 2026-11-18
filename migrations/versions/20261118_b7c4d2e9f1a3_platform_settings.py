"""Platform settings (email / SMTP)

Revision ID: b7c4d2e9f1a3
Revises: a3f2e8b1c9d7
Create Date: 2026-11-18 10:00:00.000000

* platform_settings: one JSON value per key, edited by the Master Admin in the panel (Settings). The first key is
  "email": the SMTP server and which emails Governix sends. New table only; until it is saved, the SMTP_* values in
  .env still apply.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b7c4d2e9f1a3"
down_revision: str | Sequence[str] | None = "a3f2e8b1c9d7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "platform_settings",
        sa.Column("key", sa.String(64), nullable=False),
        sa.Column("value", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_by_id", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(["updated_by_id"], ["users.id"], ondelete="SET NULL",
                                name=op.f("fk_platform_settings_updated_by_id_users")),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_platform_settings")),
    )


def downgrade() -> None:
    op.drop_table("platform_settings")
