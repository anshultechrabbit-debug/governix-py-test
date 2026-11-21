"""Old model calls: feature "not recorded" instead of "qa"

Revision ID: f1d3b7e9a2c4
Revises: e6c2a9d4b8f3
Create Date: 2026-11-21 18:00:00.000000

e6c2a9d4b8f3 labelled every model call written before features were recorded as "qa". Those calls were also
document analysis, upload classification, summaries and ticket triage, which can no longer be told apart. They
go back to NULL ("Not recorded" in the usage breakdown). Only those rows are touched: every call recorded since
carries a request id (HTTP) or a feature other than "qa" (background work); the backfilled ones have neither.
Question rows ("question") really are questions and keep "qa".
"""

from collections.abc import Sequence

from alembic import op

revision: str = "f1d3b7e9a2c4"
down_revision: str | Sequence[str] | None = "e6c2a9d4b8f3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("UPDATE org_ai_usage SET feature = NULL "
               "WHERE service = 'answers' AND feature = 'qa' AND request_id IS NULL")


def downgrade() -> None:
    op.execute("UPDATE org_ai_usage SET feature = 'qa' WHERE service = 'answers' AND feature IS NULL")
