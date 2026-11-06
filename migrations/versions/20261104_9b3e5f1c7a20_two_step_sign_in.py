"""Two-step sign-in (authenticator app)

Revision ID: 9b3e5f1c7a20
Revises: 5c09a8688675
Create Date: 2026-11-04 10:00:00.000000

* users: mfa_secret (the authenticator secret, encrypted), mfa_enabled_at, mfa_last_step (the last accepted
  30-second step, so a code is never accepted twice). All null: nobody has two-step sign-in until they turn it on.
* mfa_recovery_codes: one-time backup codes, hashed.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "9b3e5f1c7a20"
down_revision: str | Sequence[str] | None = "5c09a8688675"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("mfa_secret", sa.String(512)))
    op.add_column("users", sa.Column("mfa_enabled_at", sa.DateTime(timezone=True)))
    op.add_column("users", sa.Column("mfa_last_step", sa.BigInteger()))

    op.create_table(
        "mfa_recovery_codes",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("code_hash", sa.String(64), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index(op.f("ix_mfa_recovery_codes_user_id"), "mfa_recovery_codes", ["user_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_mfa_recovery_codes_user_id"), table_name="mfa_recovery_codes")
    op.drop_table("mfa_recovery_codes")
    op.drop_column("users", "mfa_last_step")
    op.drop_column("users", "mfa_enabled_at")
    op.drop_column("users", "mfa_secret")
