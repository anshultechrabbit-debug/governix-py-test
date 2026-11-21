import uuid
from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator
from app.core.inputs import AtMost, Line, MaybeLine, MaybeName, MaybeText

MAX_GROUPS = 200
MAX_ITEMS = 500


class BatchItemIn(BaseModel):
    filename: Line = Field(min_length=1, max_length=500)
    version_label: Annotated[MaybeLine, AtMost(50)] = None
    # Optional: without it the document's own date, else the arranged order, is used.
    effective_from: date | None = None


class ClassificationChoice(BaseModel):
    """What the AI classification suggested for a group, sent back with the plan.

    The administrator's own choice (`policy_id` / `category_id` of the group) is what is filed; when it
    differs from this suggestion the override is recorded in the audit log.
    """

    category_id: uuid.UUID | None = None  # the suggested folder (a sub-policy or a main policy)
    policy_id: uuid.UUID | None = None  # the suggested existing document (a new version of it)
    new_sub_policy_name: Annotated[MaybeName, AtMost(100)] = None  # a suggested new sub-policy ...
    main_policy_id: uuid.UUID | None = None  # ... under this main policy
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    reason: Annotated[MaybeText, AtMost(1000)] = None
    source: Literal["existing_document", "ai", "keywords", "none"] | None = None


class BatchGroupIn(BaseModel):
    """One policy document and its versions, oldest first."""

    policy_id: uuid.UUID | None = None  # add versions to this existing policy ...
    new_policy_name: Annotated[MaybeName, AtMost(500)] = Field(default=None)  # ... or create one (name optional)
    # For a new policy: its main policy or sub-policy; the detected category when omitted.
    category_id: uuid.UUID | None = None
    items: list[BatchItemIn] = Field(min_length=1, max_length=100)
    classification: ClassificationChoice | None = None

    @model_validator(mode="after")
    def one_target(self) -> "BatchGroupIn":
        if self.policy_id and (self.new_policy_name or self.category_id):
            raise ValueError("A group either adds versions to an existing policy or creates a new one, not both.")
        return self


class BatchCreate(BaseModel):
    branch_id: uuid.UUID | None = None
    department_id: uuid.UUID | None = None
    groups: list[BatchGroupIn] = Field(min_length=1, max_length=MAX_GROUPS)
    # False = "Save as draft": every file is uploaded and analysed, nothing is registered or searchable
    # until the batch is published (POST /uploads/batches/{id}/publish) or each file is confirmed in review.
    publish: bool = True

    @model_validator(mode="after")
    def bounded(self) -> "BatchCreate":
        if sum(len(g.items) for g in self.groups) > MAX_ITEMS:
            raise ValueError(f"A bulk upload holds at most {MAX_ITEMS} files.")
        return self


class BatchItemRead(BaseModel):
    id: uuid.UUID
    position: int
    original_filename: str
    document_id: uuid.UUID | None
    document_status: str | None = None
    version_label: str | None
    effective_from: date | None
    status: str
    message: str | None
    policy_version_id: uuid.UUID | None


class BatchGroupRead(BaseModel):
    id: uuid.UUID
    position: int
    policy_id: uuid.UUID | None
    policy_name: str | None = None
    new_policy_name: str | None
    category_id: uuid.UUID | None
    status: str
    message: str | None
    items: list[BatchItemRead]


class BatchRead(BaseModel):
    id: uuid.UUID
    status: str
    publish: bool = True
    created_at: datetime
    completed_at: datetime | None
    created_by_id: uuid.UUID | None
    counts: dict[str, int]
    groups: list[BatchGroupRead]


class BatchSummary(BaseModel):
    id: uuid.UUID
    status: str
    publish: bool = True
    created_at: datetime
    completed_at: datetime | None
    counts: dict[str, int]


class ExistingPolicyMatch(BaseModel):
    """An existing policy the file most likely is a version of."""

    policy_id: uuid.UUID
    name: str
    category_id: uuid.UUID
    category_name: str | None
    current_version_label: str | None
    current_effective_from: date | None
    version_count: int
    reasons: list[str]
    # False when the policy is visible but outside the scope this person manages.
    can_add_version: bool


class IdenticalDocument(BaseModel):
    """A document already uploaded with exactly the same file."""

    document_id: uuid.UUID
    title: str | None
    original_filename: str
    policy_id: uuid.UUID | None
    policy_name: str | None


class HierarchySuggestion(BaseModel):
    """Where the AI classification would file the document. Nothing is created or moved: an
    administrator confirms it (or chooses otherwise) in the upload dialog."""

    main_policy_id: uuid.UUID | None = None
    main_policy_name: str | None = None
    sub_policy_id: uuid.UUID | None = None
    sub_policy_name: str | None = None
    # No sub-policy of the main policy fits: a name for a new one (created only if the person agrees).
    new_sub_policy_name: str | None = None
    # The existing document the file is a version of (its family), when there is one.
    existing_policy_id: uuid.UUID | None = None
    existing_policy_name: str | None = None
    document_name: str | None = None
    confidence: float = 0.0
    reason: str | None = None
    # existing_document: matched a document already in the library; ai: the model read it;
    # keywords: the folders' words (no model); none: nothing fitted.
    source: Literal["existing_document", "ai", "keywords", "none"] = "none"
    requires_confirmation: bool = True


class PolicySuggestion(BaseModel):
    """What a file's opening pages say about it, read before it is uploaded for real."""

    # False for scanned or unreadable files: their name is only known after OCR.
    readable: bool
    name: str | None = None
    # What the content covers, to say why a file seems to belong elsewhere.
    about: str | None = None
    name_source: str | None = None
    name_confidence: float = 0.0
    category_id: uuid.UUID | None = None
    category_name: str | None = None
    version_label: str | None = None
    effective_date: date | None = None
    existing_policy: ExistingPolicyMatch | None = None
    identical_document: IdenticalDocument | None = None
    # Only when asked for (classify=true): main policy, sub-policy and document, with reason and confidence.
    classification: HierarchySuggestion | None = None


class PlanDocument(BaseModel):
    """One document of an upload being arranged (a group of versions), as read from its newest file."""

    key: Line = Field(min_length=1, max_length=64)
    name: Annotated[MaybeLine, AtMost(500)] = None
    about: Annotated[MaybeText, AtMost(500)] = None
    # The existing document it is a new version of: it stays in that document's folder.
    policy_id: uuid.UUID | None = None


class HierarchyPlanRequest(BaseModel):
    documents: list[PlanDocument] = Field(min_length=1, max_length=MAX_GROUPS)


class PlannedPlacement(BaseModel):
    """Where one document goes. A folder that does not exist yet is named, not created (new_* true)."""

    key: str
    main_policy_id: uuid.UUID | None = None
    main_policy_name: str | None = None
    new_main_policy: bool = False
    # The kind of documents a new main policy holds (categories.CategoryType).
    main_category_type: str | None = None
    sub_policy_id: uuid.UUID | None = None
    sub_policy_name: str | None = None
    new_sub_policy: bool = False
    confidence: float = 0.0
    reason: str | None = None


class HierarchyPlan(BaseModel):
    placements: list[PlannedPlacement]
    # ai: the model grouped the documents; keywords: existing folders' words (no model); none: no suggestion.
    source: Literal["ai", "keywords", "none"]
    # False: the AI was needed but did not answer (no model, an invalid API key, no credit). The placements are
    # then only existing folders matched by their words, and no new main policy or sub-policy is proposed.
    ai_available: bool = True
    requires_confirmation: bool = True
