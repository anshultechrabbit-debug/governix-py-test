import uuid
from datetime import datetime
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from app.modules.categories.model import CategoryType
from app.modules.policies.schema import VersionRead
from app.core.inputs import AtMost, MaybeText, Name, Tag

SLUG_PATTERN = r"^[a-z0-9][a-z0-9-]{0,98}[a-z0-9]$"


def _clean_name(value: str) -> str:
    value = " ".join(value.split())
    if len(value) < 2:
        raise ValueError("The name must have at least 2 characters.")
    return value


# Whitespace is collapsed; names are unique per organisation ignoring case.
CategoryName = Annotated[Name, Field(min_length=2, max_length=100), AfterValidator(_clean_name)]


class CategoryCreate(BaseModel):
    """A main policy (no parent) or a sub-policy of the main policy `parent_id`."""

    name: CategoryName
    # Required for a main policy; a sub-policy takes its main policy's type when omitted.
    category_type: CategoryType | None = None
    parent_id: uuid.UUID | None = None
    description: Annotated[MaybeText, AtMost(2000)] = None
    is_active: bool = True
    # Derived from the name when omitted.
    slug: str | None = Field(default=None, pattern=SLUG_PATTERN)
    # Defaults to the type's rank (see categories.defaults.TYPE_AUTHORITY_RANK).
    authority_rank: int | None = Field(default=None, ge=0, le=1000)
    keywords: list[Tag] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def type_for_main_policy(self) -> "CategoryCreate":
        if self.parent_id is None and self.category_type is None:
            raise ValueError("Choose the type of documents this main policy holds (category_type).")
        return self


class CategoryUpdate(BaseModel):
    name: CategoryName | None = None
    category_type: CategoryType | None = None
    description: Annotated[MaybeText, AtMost(2000)] = None  # null or blank clears it
    authority_rank: int | None = Field(default=None, ge=0, le=1000)
    keywords: list[Tag] | None = Field(default=None, max_length=100)
    is_active: bool | None = None


class CategoryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    slug: str
    description: str | None
    category_type: CategoryType
    authority_rank: int
    keywords: list[str]
    is_active: bool
    is_system: bool
    # The main policy of a sub-policy; null for a main policy.
    parent_id: uuid.UUID | None = None
    display_order: int | None = None
    created_at: datetime
    updated_at: datetime


class CategorySummary(CategoryRead):
    """A category with what it holds, counted over the documents the caller can see.

    For a main policy the totals include its sub-policies; `document_count` is always the
    documents directly in this category.
    """

    document_count: int = 0
    version_count: int = 0  # active versions of the documents directly in it
    sub_policy_count: int = 0
    total_document_count: int = 0
    total_version_count: int = 0
    last_updated: datetime


class CategoryTreeNode(CategorySummary):
    """A main policy and its sub-policies, in display order."""

    sub_policies: list[CategorySummary] = Field(default_factory=list)


class CategoryMove(BaseModel):
    """Put a sub-policy under another main policy (`parent_id`), or make it a main policy (null)."""

    parent_id: uuid.UUID | None = None
    # Moving changes where its documents are filed and what a main-policy search covers.
    confirm: bool = False


class CategoryPosition(BaseModel):
    """Place a policy folder directly after or before a sibling; neither = first."""

    after_id: uuid.UUID | None = None
    before_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def one_anchor(self) -> "CategoryPosition":
        if self.after_id and self.before_id:
            raise ValueError("Give after_id or before_id, not both.")
        return self


def lifecycle(status: str, timeline_state: str | None) -> str:
    """A registered version as an administrator reads it: published (in force or scheduled),
    superseded (a later version replaced it) or archived (withdrawn). Drafts are uploads not yet
    registered (`PendingUpload.draft`)."""
    if status == "withdrawn" or timeline_state == "withdrawn":
        return "archived"
    if timeline_state == "historical":
        return "superseded"
    return "published"


class CategoryVersion(VersionRead):
    """A version with the file behind it."""

    lifecycle: str = "published"
    # What changed from the previous version, in words (AI-generated from the deterministic diff).
    ai_change_summary: str | None = None

    filename: str
    title: str | None
    content_type: str
    size_bytes: int
    page_count: int | None
    document_status: str
    uploaded_at: datetime
    uploaded_by_id: uuid.UUID | None
    uploaded_by_name: str | None


class CategoryDocument(BaseModel):
    """A document (business identity) in the category and its versions, in display order."""

    id: uuid.UUID
    name: str
    policy_number: str | None
    description: str | None
    status: str
    branch_id: uuid.UUID | None
    department_id: uuid.UUID | None
    display_order: int | None
    created_at: datetime
    updated_at: datetime
    version_count: int  # active versions
    current_version_id: uuid.UUID | None
    latest_upload_at: datetime | None
    versions: list[CategoryVersion]


class PendingUpload(BaseModel):
    """A file uploaded into the category that is not yet registered as a document version."""

    document_id: uuid.UUID
    filename: str
    title: str | None
    status: str
    size_bytes: int
    uploaded_at: datetime
    uploaded_by_name: str | None
    error: str | None
    # Planned by an upload: the document it becomes a version of (null: a new document) and its batch.
    batch_id: uuid.UUID | None = None
    target_policy_id: uuid.UUID | None = None
    target_policy_name: str | None = None
    version_label: str | None = None
    # Saved as a draft: waits until the upload is published or the file is confirmed in review.
    draft: bool = False
