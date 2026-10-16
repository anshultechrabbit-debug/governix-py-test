"""Soft delete

Revision ID: e4b7d2a9c615
Revises: c2e8f4a1b937
Create Date: 2026-10-16 10:00:00.000000

* organizations, branches, departments, users: deleted_at and deleted_by_id. Deleting keeps the row
  (Organizations/Branches/Departments/Users > Delete, which cascades down the hierarchy).
* A deleted user's email, and a deleted branch's or department's code, may be used again: the
  unique indexes cover live rows only.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e4b7d2a9c615"
down_revision: str | Sequence[str] | None = "c2e8f4a1b937"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("organizations", "branches", "departments", "users")


def upgrade() -> None:
    for table in TABLES:
        op.add_column(table, sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
        op.add_column(table, sa.Column("deleted_by_id", sa.Uuid(), nullable=True))
        op.create_foreign_key(
            f"fk_{table}_deleted_by_id_users", table, "users", ["deleted_by_id"], ["id"], ondelete="SET NULL"
        )

    op.drop_index("uq_users_email_lower", table_name="users")
    op.create_index(
        "uq_users_email_lower", "users", [sa.literal_column("lower(email)")], unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.drop_constraint("uq_branches_organization_id_code", "branches", type_="unique")
    op.create_index(
        "uq_branches_organization_id_code", "branches", ["organization_id", "code"], unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.drop_constraint("uq_departments_branch_id_code", "departments", type_="unique")
    op.create_index(
        "uq_departments_branch_id_code", "departments", ["branch_id", "code"], unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )


def downgrade() -> None:
    # Fails if a deleted row's email or code has been reused since; resolve those first.
    op.drop_index("uq_departments_branch_id_code", table_name="departments")
    op.create_unique_constraint("uq_departments_branch_id_code", "departments", ["branch_id", "code"])
    op.drop_index("uq_branches_organization_id_code", table_name="branches")
    op.create_unique_constraint("uq_branches_organization_id_code", "branches", ["organization_id", "code"])
    op.drop_index("uq_users_email_lower", table_name="users")
    op.create_index("uq_users_email_lower", "users", [sa.literal_column("lower(email)")], unique=True)

    for table in reversed(TABLES):
        op.drop_constraint(f"fk_{table}_deleted_by_id_users", table, type_="foreignkey")
        op.drop_column(table, "deleted_by_id")
        op.drop_column(table, "deleted_at")
