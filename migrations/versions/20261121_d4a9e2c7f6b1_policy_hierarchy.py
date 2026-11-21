"""Policy hierarchy: main policies, sub-policies, draft uploads

Revision ID: d4a9e2c7f6b1
Revises: c8e1f4a7b2d5
Create Date: 2026-11-21 10:00:00.000000

Main Policy -> optional Sub-Policy -> Policy Document (policies) -> Versions (policy_versions).

* categories.parent_id: a category with a parent is a sub-policy of that main policy. NULL = a main
  policy. Every existing category becomes a main policy and every existing policy document stays where
  it is (directly under it), so no row moves and no chunk, embedding or citation changes.
* categories.display_order: drag-and-drop position among siblings; NULL keeps today's order.
* uq_categories_org_name (organization, lower(name)) becomes uq_categories_org_parent_name
  (organization, parent, lower(name)): "Gold Loan" may exist under two main policies, never twice
  under one. Every existing name is unique per organisation, so the new index always builds.
* ck_categories_not_own_parent and the categories_one_level trigger: a parent is a main policy of the
  same organisation, and a category with sub-policies never becomes a sub-policy (one level only).
* upload_batches.publish: false = "Save as draft" (files analysed, nothing registered until published).
  Existing batches are true, as they behaved.

Before upgrading production: back up the database (pg_dump -Fc), then
    .venv/bin/alembic upgrade head
Verify: SELECT count(*) FROM categories WHERE parent_id IS NOT NULL;  -- 0 right after the upgrade
        SELECT count(*) FROM policies;  SELECT count(*) FROM chunks;  -- unchanged
Downgrade (alembic downgrade c8e1f4a7b2d5) turns every sub-policy back into a top-level category,
renaming one whose name another top-level category already has to "<name> (<main policy>)", so no
category, document or version is lost.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d4a9e2c7f6b1"
down_revision: str | Sequence[str] | None = "c8e1f4a7b2d5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NO_PARENT = "'00000000-0000-0000-0000-000000000000'::uuid"

ONE_LEVEL = """
CREATE OR REPLACE FUNCTION categories_one_level() RETURNS trigger AS $$
DECLARE
    parent_org uuid;
    grandparent uuid;
BEGIN
    IF NEW.parent_id IS NULL THEN
        RETURN NEW;
    END IF;
    SELECT organization_id, parent_id INTO parent_org, grandparent FROM categories WHERE id = NEW.parent_id;
    IF parent_org IS DISTINCT FROM NEW.organization_id THEN
        RAISE EXCEPTION 'A sub-policy belongs to a main policy of its own organization.'
            USING ERRCODE = 'check_violation', CONSTRAINT = 'ck_categories_parent_same_org';
    END IF;
    IF grandparent IS NOT NULL THEN
        RAISE EXCEPTION 'A sub-policy cannot have sub-policies of its own.'
            USING ERRCODE = 'check_violation', CONSTRAINT = 'ck_categories_one_level';
    END IF;
    IF EXISTS (SELECT 1 FROM categories WHERE parent_id = NEW.id) THEN
        RAISE EXCEPTION 'A main policy with sub-policies cannot become a sub-policy.'
            USING ERRCODE = 'check_violation', CONSTRAINT = 'ck_categories_one_level';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""


def upgrade() -> None:
    op.add_column("categories", sa.Column("parent_id", sa.Uuid(), nullable=True))
    op.add_column("categories", sa.Column("display_order", sa.Integer(), nullable=True))
    op.create_foreign_key(
        op.f("fk_categories_parent_id_categories"), "categories", "categories", ["parent_id"], ["id"], ondelete="RESTRICT",
    )
    op.create_check_constraint(op.f("ck_categories_not_own_parent"), "categories", "parent_id IS NULL OR parent_id <> id")
    op.create_index("ix_categories_parent_display_order", "categories", ["parent_id", "display_order"])
    op.create_index(
        "uq_categories_org_parent_name", "categories",
        ["organization_id", sa.text(f"coalesce(parent_id, {NO_PARENT})"), sa.text("lower(name)")], unique=True,
    )
    op.drop_index("uq_categories_org_name", table_name="categories")
    op.execute(ONE_LEVEL)
    op.execute(
        "CREATE TRIGGER categories_one_level BEFORE INSERT OR UPDATE OF parent_id, organization_id ON categories "
        "FOR EACH ROW EXECUTE FUNCTION categories_one_level()"
    )

    op.add_column("upload_batches", sa.Column("publish", sa.Boolean(), server_default=sa.text("true"), nullable=False))


def downgrade() -> None:
    op.drop_column("upload_batches", "publish")

    op.execute("DROP TRIGGER IF EXISTS categories_one_level ON categories")
    op.execute("DROP FUNCTION IF EXISTS categories_one_level()")
    # Flatten: a sub-policy whose name a top-level category of its organisation already has (or another
    # sub-policy took first) is renamed "<name> (<main policy>)"; its documents stay with it.
    op.execute("""
        WITH ranked AS (
            SELECT c.id, c.name, p.name AS parent_name,
                   row_number() OVER (PARTITION BY c.organization_id, lower(c.name) ORDER BY c.created_at, c.id) AS n,
                   EXISTS (SELECT 1 FROM categories t WHERE t.organization_id = c.organization_id
                           AND t.parent_id IS NULL AND lower(t.name) = lower(c.name)) AS taken
            FROM categories c JOIN categories p ON p.id = c.parent_id
        )
        UPDATE categories SET name = left(ranked.name || ' (' || ranked.parent_name || ')', 100)
        FROM ranked WHERE categories.id = ranked.id AND (ranked.taken OR ranked.n > 1)
    """)
    op.execute("UPDATE categories SET parent_id = NULL WHERE parent_id IS NOT NULL")
    op.create_index(
        "uq_categories_org_name", "categories", ["organization_id", sa.text("lower(name)")], unique=True,
    )
    op.drop_index("uq_categories_org_parent_name", table_name="categories")
    op.drop_index("ix_categories_parent_display_order", table_name="categories")
    op.drop_constraint(op.f("ck_categories_not_own_parent"), "categories", type_="check")
    op.drop_constraint(op.f("fk_categories_parent_id_categories"), "categories", type_="foreignkey")
    op.drop_column("categories", "display_order")
    op.drop_column("categories", "parent_id")
