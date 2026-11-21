from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.categories.model import Category


class CategoryRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get_in_org(self, organization_id: uuid.UUID, category_id: uuid.UUID) -> Category | None:
        return self.session.scalar(
            select(Category).where(
                Category.id == category_id, Category.organization_id == organization_id
            )
        )

    def slug_taken(self, organization_id: uuid.UUID, slug: str) -> bool:
        return self.session.scalar(
            select(Category.id).where(Category.organization_id == organization_id, Category.slug == slug).limit(1)
        ) is not None

    def list(self, organization_id: uuid.UUID, *, include_inactive: bool = False) -> list[Category]:
        """Main policies and sub-policies alike; each sub-policy names its main policy (parent_id).

        Not yet arranged folders keep the old order (authority, then name), so nothing moves until a
        person arranges them.
        """
        query = select(Category).where(Category.organization_id == organization_id)
        if not include_inactive:
            query = query.where(Category.is_active.is_(True))
        return list(self.session.scalars(query.order_by(
            Category.display_order.asc().nulls_last(), Category.authority_rank.desc(), Category.name,
        )))

    def children(self, parent_id: uuid.UUID) -> list[Category]:
        return list(self.session.scalars(select(Category).where(Category.parent_id == parent_id)))
