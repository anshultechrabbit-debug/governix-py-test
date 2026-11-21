"""The policy hierarchy: Main Policy -> optional Sub-Policy -> Policy Document -> Versions.

A main policy is a top-level category; a sub-policy is a category whose `parent_id` is a main policy
(one level only). A policy document (`Policy`) sits directly in a main policy or in one of its
sub-policies, and each keeps its own version history.

Searching, filtering or asking about a main policy covers its sub-policies too (`with_sub_policies`);
asking about a sub-policy covers that sub-policy only, so "Gold Loan" is never answered from "Home
Loan" unless both are asked for.
"""

import uuid
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.categories.model import Category


def with_sub_policies(session: Session, organization_id: uuid.UUID | None, ids: Iterable[uuid.UUID]) -> list[uuid.UUID]:
    """The given categories plus the sub-policies of any main policy among them, in a stable order.

    Only categories of the organisation are expanded; ids from elsewhere are kept as given, so they
    still match nothing (the search's own tenant filter decides that).
    """
    wanted = list(dict.fromkeys(ids))
    if not wanted or organization_id is None:
        return wanted
    children = session.scalars(
        select(Category.id).where(
            Category.organization_id == organization_id, Category.parent_id.in_(wanted),
        ).order_by(Category.id)
    ).all()
    return list(dict.fromkeys([*wanted, *children]))


def path_names(session: Session, categories: Iterable[Category]) -> dict[uuid.UUID, str]:
    """Each category's place in the hierarchy as people read it: "Loan Policy › Gold Loan"."""
    categories = [c for c in categories if c is not None]
    parent_ids = {c.parent_id for c in categories if c.parent_id}
    parents = {
        c.id: c.name for c in session.scalars(select(Category).where(Category.id.in_(parent_ids)))
    } if parent_ids else {}
    return {
        c.id: f"{parents[c.parent_id]} › {c.name}" if c.parent_id in parents else c.name
        for c in categories
    }
