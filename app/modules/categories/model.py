import uuid
from enum import StrEnum

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, Text, UniqueConstraint, func
from sqlalchemy import text as sql
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, TimestampMixin, UUIDPrimaryKeyMixin


class CategoryType(StrEnum):
    """What kind of documents a category holds. Drives the default authority rank."""

    POLICY = "policy"
    CIRCULAR = "circular"
    SOP = "sop"
    GUIDELINE = "guideline"
    MANUAL = "manual"
    NOTICE = "notice"
    PROCESS_DOCUMENT = "process_document"
    FORM = "form"
    FAQ = "faq"
    REGULATORY_DOCUMENT = "regulatory_document"
    OTHER = "other"


# Stands in for "no parent" in the per-parent name index (NULLs never collide in a unique index).
NO_PARENT = "00000000-0000-0000-0000-000000000000"


class Category(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A policy folder. A top-level category is a Main Policy ("Loan Policy"); one with a parent is a
    Sub-Policy ("Gold Loan"). Only one level: a sub-policy never has sub-policies of its own (enforced
    here and by the `categories_one_level` trigger). Policy documents (`Policy`) sit in either.
    """

    __tablename__ = "categories"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), index=True
    )
    # The main policy this sub-policy belongs to; NULL for a main policy.
    parent_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("categories.id", ondelete="RESTRICT"))
    # Position among its siblings (drag and drop). NULL until arranged; arranged ones come first.
    display_order: Mapped[int | None]
    name: Mapped[str] = mapped_column(String(100))
    slug: Mapped[str] = mapped_column(String(100))
    description: Mapped[str | None] = mapped_column(Text)
    category_type: Mapped[str] = mapped_column(String(30), default=CategoryType.OTHER, server_default="other")
    # Higher wins when sources conflict and no amendment/effective-date rule decides.
    authority_rank: Mapped[int] = mapped_column(default=50)
    # Phrases that indicate this category during classification (admin-editable).
    keywords: Mapped[list[str]] = mapped_column(ARRAY(String(100)), default=list)
    is_active: Mapped[bool] = mapped_column(default=True)
    is_system: Mapped[bool] = mapped_column(default=False)

    __table_args__ = (
        UniqueConstraint("organization_id", "slug", name="uq_categories_organization_id_slug"),
        # Categories are organisation-wide, so a name is unique across every branch, within its parent:
        # two main policies never share a name, nor two sub-policies of one main policy; "Gold Loan"
        # may exist under both "Loan Policy" and "Priority Sector".
        Index(
            "uq_categories_org_parent_name",
            "organization_id",
            func.coalesce(parent_id, sql(f"'{NO_PARENT}'::uuid")),
            func.lower(name),
            unique=True,
        ),
        Index("ix_categories_parent_display_order", "parent_id", "display_order"),
        CheckConstraint("parent_id IS NULL OR parent_id <> id", name="not_own_parent"),
    )

    @property
    def is_main(self) -> bool:
        return self.parent_id is None
