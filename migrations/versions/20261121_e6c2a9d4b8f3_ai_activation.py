"""Mandatory AI configuration: activation per organization and richer usage records

Revision ID: e6c2a9d4b8f3
Revises: d4a9e2c7f6b1
Create Date: 2026-11-21 16:00:00.000000

* org_ai_settings: whether the Master Admin has switched AI on for an organization (activated / suspended, by
  whom, when). With AI_REQUIRE_ORG_CONFIG (the default) no AI call of an organization runs without an active row.
* org_ai_usage gains branch_id, feature, estimated and request_id, for the usage breakdown and the audit trail.

Organizations the Master Admin had already configured (an active API key the provider accepted, and an enabled
answering model whose provider has that key) are activated here, so they keep their AI. Every other organization
starts NOT_CONFIGURED: it was running on the platform's key, which is no longer used for organizations.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e6c2a9d4b8f3"
down_revision: str | Sequence[str] | None = "d4a9e2c7f6b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "org_ai_settings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("activated_by_id", sa.Uuid(), nullable=True),
        sa.Column("suspended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("suspended_by_id", sa.Uuid(), nullable=True),
        sa.Column("suspended_reason", sa.String(300), nullable=True),
        sa.Column("notified_status", sa.String(30), nullable=True),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE",
                                name=op.f("fk_org_ai_settings_organization_id_organizations")),
        sa.ForeignKeyConstraint(["activated_by_id"], ["users.id"], ondelete="SET NULL",
                                name=op.f("fk_org_ai_settings_activated_by_id_users")),
        sa.ForeignKeyConstraint(["suspended_by_id"], ["users.id"], ondelete="SET NULL",
                                name=op.f("fk_org_ai_settings_suspended_by_id_users")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_org_ai_settings")),
        sa.UniqueConstraint("organization_id", name=op.f("uq_org_ai_settings_organization_id")),
    )

    op.add_column("org_ai_usage", sa.Column("branch_id", sa.Uuid(), nullable=True))
    op.add_column("org_ai_usage", sa.Column("feature", sa.String(40), nullable=True))
    op.add_column("org_ai_usage", sa.Column("estimated", sa.Boolean(), nullable=False, server_default=sa.text("false")))
    op.add_column("org_ai_usage", sa.Column("request_id", sa.String(64), nullable=True))
    op.create_foreign_key(op.f("fk_org_ai_usage_branch_id_branches"), "org_ai_usage", "branches",
                          ["branch_id"], ["id"], ondelete="SET NULL")
    op.create_index("ix_org_ai_usage_created", "org_ai_usage", ["created_at"])

    # Rows written before features were recorded: the service says what they were for.
    op.execute("UPDATE org_ai_usage SET feature = 'qa' WHERE service IN ('question', 'answers')")
    op.execute("UPDATE org_ai_usage SET feature = 'indexing' WHERE service = 'embeddings'")
    op.execute("UPDATE org_ai_usage SET estimated = true WHERE meta ? 'estimated'")

    # Already configured by the Master Admin: keep AI on (computed from the configuration, never assumed).
    op.execute("""
        INSERT INTO org_ai_settings (id, organization_id, is_active, activated_at)
        SELECT gen_random_uuid(), o.id, true, now()
        FROM organizations o
        WHERE o.deleted_at IS NULL
          AND EXISTS (
            SELECT 1 FROM org_ai_models m
            JOIN org_api_keys k ON k.organization_id = m.organization_id AND k.provider = m.provider
            WHERE m.organization_id = o.id AND m.service = 'answers' AND m.is_enabled
              AND k.kind = 'api' AND k.is_active AND k.validation_status = 'valid')
    """)


def downgrade() -> None:
    op.drop_index("ix_org_ai_usage_created", table_name="org_ai_usage")
    op.drop_constraint(op.f("fk_org_ai_usage_branch_id_branches"), "org_ai_usage", type_="foreignkey")
    op.drop_column("org_ai_usage", "request_id")
    op.drop_column("org_ai_usage", "estimated")
    op.drop_column("org_ai_usage", "feature")
    op.drop_column("org_ai_usage", "branch_id")
    op.drop_table("org_ai_settings")
