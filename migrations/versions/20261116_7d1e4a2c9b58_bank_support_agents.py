"""Bank support agents

Revision ID: 7d1e4a2c9b58
Revises: 9b3e5f1c7a20
Create Date: 2026-11-16 10:00:00.000000

* users: a support_agent may belong to a bank (Head Office Support staff) or to a bank and one of its branches
  (Branch Support staff), so a bank's support staff are their own accounts instead of existing users put on
  duty. Governix's own agents (Technical Support) still have no organization. Never a department, never a
  branch without its bank.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "7d1e4a2c9b58"
down_revision: str | Sequence[str] | None = "9b3e5f1c7a20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROLE_SCOPE_OLD = (
    "(role IN ('master_admin', 'support_agent') AND organization_id IS NULL AND branch_id IS NULL"
    " AND department_id IS NULL)"
    " OR (role = 'org_admin' AND organization_id IS NOT NULL AND branch_id IS NULL AND department_id IS NULL)"
    " OR (role = 'branch_manager' AND organization_id IS NOT NULL AND branch_id IS NOT NULL AND department_id IS NULL)"
    " OR (role = 'department_user' AND organization_id IS NOT NULL AND branch_id IS NOT NULL)"
)
ROLE_SCOPE_NEW = (
    "(role = 'master_admin' AND organization_id IS NULL AND branch_id IS NULL AND department_id IS NULL)"
    " OR (role = 'support_agent' AND department_id IS NULL AND (organization_id IS NOT NULL OR branch_id IS NULL))"
    " OR (role = 'org_admin' AND organization_id IS NOT NULL AND branch_id IS NULL AND department_id IS NULL)"
    " OR (role = 'branch_manager' AND organization_id IS NOT NULL AND branch_id IS NOT NULL AND department_id IS NULL)"
    " OR (role = 'department_user' AND organization_id IS NOT NULL AND branch_id IS NOT NULL)"
)


def upgrade() -> None:
    op.drop_constraint(op.f("ck_users_role_scope"), "users", type_="check")
    op.create_check_constraint(op.f("ck_users_role_scope"), "users", ROLE_SCOPE_NEW)


def downgrade() -> None:
    # Fails while a bank's support agent account exists (live or soft-deleted): delete those rows first.
    op.drop_constraint(op.f("ck_users_role_scope"), "users", type_="check")
    op.create_check_constraint(op.f("ck_users_role_scope"), "users", ROLE_SCOPE_OLD)
