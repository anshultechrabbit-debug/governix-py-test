from __future__ import annotations

import re
import uuid

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.core.database import translate_unique_violation
from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.modules.audit.service import record_event
from app.modules.auth.permissions import Principal
from app.modules.auth.scope import tenant_id
from app.modules.categories.defaults import TYPE_AUTHORITY_RANK
from app.modules.categories.model import Category, CategoryType
from app.modules.categories.repository import CategoryRepository
from app.modules.categories.schema import CategoryCreate, CategorySummary, CategoryTreeNode, CategoryUpdate
from app.modules.categories import contents
from app.modules.organizations.model import Organization
from app.modules.organizations.repository import bump_knowledge_version


def _clean_keywords(keywords: list[str]) -> list[str]:
    return sorted({k.strip().lower() for k in keywords if k.strip()})


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:90].strip("-")
    return slug if len(slug) >= 2 else "category"


def _kind(category: Category | None) -> str:
    return "sub-policy" if category is not None and category.parent_id else "main policy"


class CategoryService:
    """Policy folders: main policies (top-level categories) and their sub-policies (one level)."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.repository = CategoryRepository(session)

    def list(self, principal: Principal, *, include_inactive: bool) -> list[Category]:
        return self.repository.list(tenant_id(principal), include_inactive=include_inactive)

    def get(self, principal: Principal, category_id: uuid.UUID) -> Category:
        category = self.repository.get_in_org(tenant_id(principal), category_id)
        if category is None:
            raise NotFoundError("Category not found.")
        return category

    def overview(self, principal: Principal, *, include_inactive: bool) -> list[CategorySummary]:
        return contents.summaries(
            self.session, principal, self.list(principal, include_inactive=include_inactive),
            include_inactive=include_inactive,
        )

    def summary(self, principal: Principal, category_id: uuid.UUID) -> CategorySummary:
        [summary] = contents.summaries(self.session, principal, [self.get(principal, category_id)])
        return summary

    def tree(self, principal: Principal, *, include_inactive: bool, search: str | None,
             include_empty_system: bool = False) -> list[CategoryTreeNode]:
        return contents.tree(
            self.session, principal, tenant_id(principal), include_inactive=include_inactive, search=search,
            include_empty_system=include_empty_system,
        )

    # --- create and edit ----------------------------------------------------------------

    def create(self, principal: Principal, data: CategoryCreate) -> Category:
        organization_id = tenant_id(principal)
        parent = self._parent(organization_id, data.parent_id) if data.parent_id else None
        self._ensure_name_free(organization_id, data.name, parent.id if parent else None)
        if data.slug is not None and self.repository.slug_taken(organization_id, data.slug):
            raise ConflictError("A category with this slug already exists.")
        # A sub-policy holds the same kind of documents as its main policy unless told otherwise.
        category_type = data.category_type or (CategoryType(parent.category_type) if parent else CategoryType.OTHER)
        if data.authority_rank is not None:
            rank = data.authority_rank
        elif parent is not None and data.category_type is None:
            rank = parent.authority_rank
        else:
            rank = TYPE_AUTHORITY_RANK[category_type]
        base_slug = _slugify(f"{parent.slug}-{data.name}" if parent else data.name)
        category = Category(
            organization_id=organization_id,
            parent_id=parent.id if parent else None,
            name=data.name,
            slug=data.slug or self._free_slug(organization_id, base_slug),
            description=(data.description or "").strip() or None,
            category_type=category_type,
            authority_rank=rank,
            keywords=_clean_keywords(data.keywords),
            is_active=data.is_active,
        )
        with translate_unique_violation(self.session, self._taken_message(data.name, parent), "DUPLICATE_CATEGORY"):
            self.session.add(category)
            self.session.flush()
            record_event(
                self.session, "category.created", actor=principal, resource_type="category",
                resource_id=category.id,
                details={"name": category.name, "slug": category.slug, "type": category.category_type,
                         "level": _kind(category), "parent_id": str(parent.id) if parent else None,
                         "parent": parent.name if parent else None},
            )
            self.session.commit()
        return category

    def update(self, principal: Principal, category_id: uuid.UUID, data: CategoryUpdate) -> Category:
        category = self.get(principal, category_id)
        changes = data.model_dump(exclude_unset=True)
        # Only the description may be cleared; a null elsewhere means "unchanged".
        changes = {k: v for k, v in changes.items() if v is not None or k == "description"}
        if "description" in changes:
            changes["description"] = (changes["description"] or "").strip() or None
        old_name = category.name
        if "name" in changes and changes["name"].lower() != category.name.lower():
            self._ensure_name_free(category.organization_id, changes["name"], category.parent_id, exclude=category.id)
        if "keywords" in changes:
            changes["keywords"] = _clean_keywords(changes["keywords"])
        cascaded: list[str] = []
        if changes.get("is_active") is True and category.parent_id:
            parent = self.session.get(Category, category.parent_id)
            if parent is not None and not parent.is_active:
                raise ValidationError(
                    f'"{parent.name}" is archived. Restore the main policy before its sub-policies.',
                    code="PARENT_ARCHIVED",
                )
        if changes.get("is_active") is False and category.parent_id is None:
            # Archiving a main policy archives its sub-policies: nothing new is filed under it.
            for child in self.repository.children(category.id):
                if child.is_active:
                    child.is_active = False
                    cascaded.append(child.name)
        for field, value in changes.items():
            setattr(category, field, value)
        with translate_unique_violation(self.session, self._taken_message(changes.get("name", ""), self._parent_of(category)),
                                        "DUPLICATE_CATEGORY"):
            details = {"changes": {k: str(v) for k, v in changes.items()}, "level": _kind(category)}
            if "name" in changes and changes["name"] != old_name:
                details["renamed_from"] = old_name
            if cascaded:
                details["archived_sub_policies"] = cascaded
            if "name" in changes:
                bump_knowledge_version(self.session, category.organization_id)  # citations name the folder
            record_event(
                self.session, "category.updated", actor=principal, resource_type="category",
                resource_id=category.id, details=details,
            )
            self.session.commit()
        return category

    # --- hierarchy ------------------------------------------------------------------

    def move(self, principal: Principal, category_id: uuid.UUID, parent_id: uuid.UUID | None, *, confirm: bool) -> Category:
        """Put a sub-policy under another main policy, make it a main policy (parent None), or make a
        main policy without sub-policies a sub-policy. Its documents and their versions go with it."""
        category = self.get(principal, category_id)
        organization_id = category.organization_id
        if parent_id == category.parent_id:
            return category
        if parent_id == category.id:
            raise ValidationError("A policy folder cannot be placed inside itself.")
        parent = self._parent(organization_id, parent_id) if parent_id else None
        # Lock the folder and both parents so two moves cannot nest one folder inside another.
        lock_ids = sorted({i for i in (category.id, category.parent_id, parent_id) if i}, key=str)
        self.session.scalars(select(Category.id).where(Category.id.in_(lock_ids)).order_by(Category.id).with_for_update()).all()
        if parent is not None and self.repository.children(category.id):
            raise ConflictError(
                f'"{category.name}" has sub-policies of its own, so it cannot become a sub-policy. '
                "Move its sub-policies first.", code="HAS_SUB_POLICIES",
            )
        self._ensure_name_free(organization_id, category.name, parent.id if parent else None, exclude=category.id)
        old_parent = self._parent_of(category)
        if not confirm:
            where = f'under "{parent.name}"' if parent else "to the top level as a main policy"
            raise ConflictError(
                f'Moving "{category.name}" {where} changes where its documents are filed and which main policy '
                "a search or question covers. Confirm to continue.",
                code="MOVE_REQUIRES_CONFIRMATION",
                details={"from_parent_id": str(old_parent.id) if old_parent else None,
                         "to_parent_id": str(parent.id) if parent else None},
            )
        category.parent_id = parent.id if parent else None
        category.display_order = None  # positions belong to the old siblings; it goes after the arranged ones
        with translate_unique_violation(self.session, self._taken_message(category.name, parent), "DUPLICATE_CATEGORY"):
            self.session.flush()
        bump_knowledge_version(self.session, organization_id)  # what a main-policy filter covers has changed
        record_event(
            self.session, "category.moved", actor=principal, resource_type="category", resource_id=category.id,
            details={"name": category.name,
                     "from_parent_id": str(old_parent.id) if old_parent else None,
                     "from_parent": old_parent.name if old_parent else None,
                     "to_parent_id": str(parent.id) if parent else None,
                     "to_parent": parent.name if parent else None},
        )
        self.session.commit()
        return category

    def position(self, principal: Principal, category_id: uuid.UUID, *, after_id: uuid.UUID | None,
                 before_id: uuid.UUID | None) -> Category:
        """Place a folder after/before a sibling (neither: first) among the main policies, or among the
        sub-policies of its main policy, and keep that order."""
        category = self.get(principal, category_id)
        # One arrangement at a time per set of siblings: lock their parent (the organisation for main policies).
        if category.parent_id:
            self.session.scalar(select(Category.id).where(Category.id == category.parent_id).with_for_update())
            siblings_of = Category.parent_id == category.parent_id
        else:
            self.session.scalar(select(Organization.id).where(Organization.id == category.organization_id).with_for_update())
            siblings_of = Category.parent_id.is_(None)
        rows = self.session.execute(
            select(Category.id, Category.display_order)
            .where(Category.organization_id == category.organization_id, siblings_of)
            .order_by(*contents.sibling_order())
        ).all()
        current = {row.id: row.display_order for row in rows}
        ordered = [row.id for row in rows if row.id != category.id]
        anchor = after_id or before_id
        if anchor is not None:
            if anchor == category.id or anchor not in current:
                raise ValidationError("The other folder is not at the same level of this main policy.")
            index = ordered.index(anchor) + (1 if after_id else 0)
        else:
            index = 0
        ordered.insert(index, category.id)
        changed = [{"id": cid, "display_order": pos} for pos, cid in enumerate(ordered) if current[cid] != pos]
        if changed:
            self.session.execute(update(Category), changed)
        record_event(
            self.session, "category.reordered", actor=principal, resource_type="category", resource_id=category.id,
            details={"name": category.name, "level": _kind(category), "position": index,
                     "parent_id": str(category.parent_id) if category.parent_id else None},
        )
        self.session.commit()
        self.session.refresh(category)
        return category

    # --- helpers ----------------------------------------------------------------------

    def _parent(self, organization_id: uuid.UUID, parent_id: uuid.UUID) -> Category:
        """The main policy a sub-policy goes under: of this organisation, active, and itself top-level."""
        parent = self.repository.get_in_org(organization_id, parent_id)
        if parent is None:
            raise ValidationError("Main policy not found.", code="PARENT_NOT_FOUND")
        if parent.parent_id is not None:
            raise ValidationError(
                f'"{parent.name}" is a sub-policy. Sub-policies go directly under a main policy (one level).',
                code="NESTING_NOT_ALLOWED",
            )
        if not parent.is_active:
            raise ValidationError(f'"{parent.name}" is archived.', code="PARENT_ARCHIVED")
        return parent

    def _parent_of(self, category: Category) -> Category | None:
        return self.session.get(Category, category.parent_id) if category.parent_id else None

    @staticmethod
    def _taken_message(name: str, parent: Category | None) -> str:
        if parent is not None:
            return f'"{parent.name}" already has a sub-policy named "{name}".'
        return f'A main policy named "{name}" already exists.'

    def _ensure_name_free(self, organization_id: uuid.UUID, name: str, parent_id: uuid.UUID | None,
                          exclude: uuid.UUID | None = None) -> None:
        """Names are unique among siblings, ignoring case: main policies among main policies, sub-policies
        within their main policy. The same sub-policy name under two main policies is allowed."""
        query = select(Category.id).where(
            Category.organization_id == organization_id, func.lower(Category.name) == name.lower(),
            Category.parent_id == parent_id if parent_id else Category.parent_id.is_(None),
        )
        if exclude is not None:
            query = query.where(Category.id != exclude)
        if self.session.scalar(query.limit(1)) is not None:
            parent = self.session.get(Category, parent_id) if parent_id else None
            raise ConflictError(self._taken_message(name, parent), code="DUPLICATE_CATEGORY")

    def _free_slug(self, organization_id: uuid.UUID, base: str) -> str:
        slug, suffix = base, 2
        while self.repository.slug_taken(organization_id, slug):
            slug, suffix = f"{base}-{suffix}", suffix + 1
        return slug
