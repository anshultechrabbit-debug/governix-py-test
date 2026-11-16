import uuid
from datetime import date, datetime
from typing import Annotated, Any

from pydantic import BaseModel, Field
from app.core.inputs import AtMost, Line, MaybeLine, MaybeText, Name


class AnalysisRead(BaseModel):
    document_id: uuid.UUID
    # Everything below is DETECTED/SUGGESTED by the system, not confirmed.
    detected: dict[str, Any]
    suggested_name: str | None
    name_confidence: float
    suggested_category_id: uuid.UUID | None
    suggested_category_name: str | None
    category_confidence: float
    category_ranking: list[dict[str, Any]]
    decision: str
    confidence: float
    matched_policy: dict[str, Any] | None
    duplicate_of_document_id: uuid.UUID | None
    signals: list[dict[str, Any]]
    candidates: list[dict[str, Any]]
    amendment_targets: list[dict[str, Any]]
    conflict: dict[str, Any] | None
    missing_fields: list[str]
    review_status: str
    reviewed_by_id: uuid.UUID | None
    reviewed_at: datetime | None
    resolution: dict[str, Any] | None
    suggested_initial_version: dict[str, Any] | None


class RelationshipInput(BaseModel):
    relation_type: str = Field(max_length=50)
    target_policy_id: uuid.UUID
    target_version_id: uuid.UUID | None = None
    clauses: list[Annotated[Line, Field(max_length=200)]] = Field(default_factory=list, max_length=100)
    evidence: Annotated[MaybeText, AtMost(2000)] = None


class PolicyInput(BaseModel):
    name: Name = Field(min_length=2, max_length=500)
    category_id: uuid.UUID
    policy_number: Annotated[MaybeLine, AtMost(100)] = None
    document_number: Annotated[MaybeLine, AtMost(100)] = None
    issuer: Annotated[MaybeLine, AtMost(200)] = None
    issuing_department: Annotated[MaybeLine, AtMost(200)] = None
    owner: Annotated[MaybeLine, AtMost(200)] = None
    description: Annotated[MaybeText, AtMost(5000)] = None
    branch_id: uuid.UUID | None = None
    department_id: uuid.UUID | None = None


class VersionInput(BaseModel):
    version_label: Annotated[MaybeLine, AtMost(50)] = None
    revision_number: int | None = Field(default=None, ge=0, le=100_000)
    # Optional. When omitted: the date the document states, else the upload date.
    effective_from: date | None = None
    effective_to: date | None = None
