"""Organization-wise AI management

Revision ID: a3f2e8b1c9d7
Revises: 7d1e4a2c9b58
Create Date: 2026-11-17 08:00:00.000000

Per-organization AI configuration, set by the Master Admin (app/modules/ai_management):
  * org_api_keys          API key vault (Fernet ciphertext + a display hint; one active API key and one active
                          Admin key per provider)
  * org_ai_models         models assigned to the organization, one default per service
  * org_quota_configs     shared question limit
  * org_token_configs     shared input / output / embedding / total token limits
  * org_credit_configs    monetary budget, monthly reset, rollover
  * org_ai_notif_configs  alert thresholds per resource
  * org_ai_usage          ledger of every AI call (reserved, then settled with actual usage)
  * org_ai_notif_sent     alerts already sent per quota period
New tables only: nothing existing changes. An organization with nothing configured keeps using the platform's
model and key, without limits.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a3f2e8b1c9d7"
down_revision: str | Sequence[str] | None = "7d1e4a2c9b58"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    ]


def _organization(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE",
                                   name=op.f(f"fk_{table}_organization_id_organizations"))


def upgrade() -> None:
    op.create_table(
        "org_api_keys",
        sa.Column("id", sa.Uuid(), nullable=False),
        *_timestamps(),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(40), nullable=False),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("label", sa.String(100), nullable=True),
        sa.Column("key_encrypted", sa.Text(), nullable=False),
        sa.Column("key_hint", sa.String(40), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("validation_status", sa.String(20), nullable=True),
        sa.Column("validation_message", sa.String(300), nullable=True),
        sa.Column("validated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rotated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_by_id", sa.Uuid(), nullable=True),
        _organization("org_api_keys"),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"], ondelete="SET NULL",
                                name=op.f("fk_org_api_keys_created_by_id_users")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_org_api_keys")),
    )
    op.create_index("ix_org_api_keys_org", "org_api_keys", ["organization_id"])
    op.create_index("uq_org_api_keys_active_provider", "org_api_keys", ["organization_id", "provider", "kind"],
                    unique=True, postgresql_where=sa.text("is_active"))

    op.create_table(
        "org_ai_models",
        sa.Column("id", sa.Uuid(), nullable=False),
        *_timestamps(),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(40), nullable=False),
        sa.Column("model_id", sa.String(200), nullable=False),
        sa.Column("service", sa.String(20), nullable=False),
        sa.Column("display_name", sa.String(200), nullable=True),
        sa.Column("is_default", sa.Boolean(), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), nullable=False),
        sa.Column("input_price_per_million", sa.Numeric(12, 6), nullable=True),
        sa.Column("output_price_per_million", sa.Numeric(12, 6), nullable=True),
        _organization("org_ai_models"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_org_ai_models")),
        sa.UniqueConstraint("organization_id", "service", "provider", "model_id", name="uq_org_ai_models_org_model"),
    )
    op.create_index("ix_org_ai_models_org", "org_ai_models", ["organization_id"])
    op.create_index("uq_org_ai_models_default", "org_ai_models", ["organization_id", "service"], unique=True,
                    postgresql_where=sa.text("is_default"))

    for table, columns in (
        ("org_quota_configs", [
            sa.Column("question_limit", sa.BigInteger(), nullable=True),
        ]),
        ("org_token_configs", [
            sa.Column("input_token_limit", sa.BigInteger(), nullable=True),
            sa.Column("output_token_limit", sa.BigInteger(), nullable=True),
            sa.Column("embedding_token_limit", sa.BigInteger(), nullable=True),
            sa.Column("total_token_limit", sa.BigInteger(), nullable=True),
        ]),
    ):
        op.create_table(
            table,
            sa.Column("id", sa.Uuid(), nullable=False),
            *_timestamps(),
            sa.Column("organization_id", sa.Uuid(), nullable=False),
            sa.Column("is_enabled", sa.Boolean(), nullable=False),
            sa.Column("period", sa.String(20), nullable=False),
            sa.Column("reset_days", sa.Integer(), nullable=True),
            *columns,
            sa.Column("period_anchor", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            _organization(table),
            sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{table}")),
            sa.UniqueConstraint("organization_id", name=op.f(f"uq_{table}_organization_id")),
        )

    op.create_table(
        "org_credit_configs",
        sa.Column("id", sa.Uuid(), nullable=False),
        *_timestamps(),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), nullable=False),
        sa.Column("budget_usd", sa.Numeric(14, 6), nullable=True),
        sa.Column("monthly_reset", sa.Boolean(), nullable=False),
        sa.Column("rollover", sa.Boolean(), nullable=False),
        sa.Column("carried_over_usd", sa.Numeric(14, 6), nullable=False),
        sa.Column("carried_for", sa.DateTime(timezone=True), nullable=True),
        sa.Column("period_anchor", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        _organization("org_credit_configs"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_org_credit_configs")),
        sa.UniqueConstraint("organization_id", name=op.f("uq_org_credit_configs_organization_id")),
    )

    op.create_table(
        "org_ai_notif_configs",
        sa.Column("id", sa.Uuid(), nullable=False),
        *_timestamps(),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("resource", sa.String(30), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), nullable=False),
        sa.Column("thresholds", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        _organization("org_ai_notif_configs"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_org_ai_notif_configs")),
        sa.UniqueConstraint("organization_id", "resource", name="uq_org_ai_notif_configs_org_resource"),
    )

    op.create_table(
        "org_ai_usage",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("service", sa.String(40), nullable=False),
        sa.Column("provider", sa.String(40), nullable=False),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("input_tokens", sa.BigInteger(), nullable=False),
        sa.Column("output_tokens", sa.BigInteger(), nullable=False),
        sa.Column("embedding_tokens", sa.BigInteger(), nullable=False),
        sa.Column("cost_usd", sa.Numeric(14, 8), nullable=False),
        sa.Column("error", sa.String(80), nullable=True),
        sa.Column("meta", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        _organization("org_ai_usage"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL",
                                name=op.f("fk_org_ai_usage_user_id_users")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_org_ai_usage")),
    )
    op.create_index("ix_org_ai_usage_org_created", "org_ai_usage", ["organization_id", "created_at"])

    op.create_table(
        "org_ai_notif_sent",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("resource", sa.String(30), nullable=False),
        sa.Column("threshold", sa.Integer(), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        _organization("org_ai_notif_sent"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_org_ai_notif_sent")),
        sa.UniqueConstraint("organization_id", "resource", "threshold", "period_start", name="uq_org_ai_notif_sent"),
    )


def downgrade() -> None:
    for table in ("org_ai_notif_sent", "org_ai_usage", "org_ai_notif_configs", "org_credit_configs",
                  "org_token_configs", "org_quota_configs", "org_ai_models", "org_api_keys"):
        op.drop_table(table)
