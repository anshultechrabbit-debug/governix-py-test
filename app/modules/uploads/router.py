import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.database import get_db
from app.core.dependencies import get_app_settings, get_queue, get_storage
from app.core.responses import ApiResponse, ok
from app.infrastructure.queue.base import Queue
from app.infrastructure.storage.base import Storage
from app.modules.auth.dependencies import require
from app.modules.auth.permissions import Permission, Principal
from app.infrastructure.ai.llm.base import LLMUnavailableError
from app.modules.ai_management.runtime import organization_llm
from app.modules.uploads.schema import (
    BatchCreate,
    BatchItemRead,
    BatchRead,
    BatchSummary,
    HierarchyPlan,
    HierarchyPlanRequest,
    PlannedPlacement,
    PolicySuggestion,
)
from app.modules.uploads.classify import PlanInput, plan_hierarchy
from app.modules.auth.scope import tenant_id
from app.modules.categories.model import Category
from app.modules.policies.model import Policy
from app.modules.policies.repository import policy_visible
from sqlalchemy import select
from app.modules.uploads.service import UploadBatchService
from app.modules.uploads.suggest import suggest_policy
from app.workers.runtime import get_runtime

router = APIRouter(prefix="/uploads", tags=["bulk upload"])

Uploader = Annotated[Principal, Depends(require(Permission.DOCUMENTS_UPLOAD))]


def get_service(
    db: Annotated[Session, Depends(get_db)],
    storage: Annotated[Storage, Depends(get_storage)],
    queue: Annotated[Queue, Depends(get_queue)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> UploadBatchService:
    return UploadBatchService(db, storage, queue, settings)


Service = Annotated[UploadBatchService, Depends(get_service)]


@router.post("/suggestions", response_model=ApiResponse[PolicySuggestion])
def suggest(
    principal: Uploader, db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_app_settings)], file: Annotated[UploadFile, File()],
    policy_id: Annotated[uuid.UUID | None, Form()] = None,
    classify: Annotated[bool, Form()] = False,
):
    """Read a file's opening pages while uploads are being arranged: the policy name the
    document states, its category, and an existing policy it most likely is a version of.

    `policy_id` is the policy the file is meant for: when the file matches it, it is the
    one reported, even if another policy matches too. `classify=true` adds the AI
    classification: the main policy, sub-policy (or a new sub-policy) and document it belongs
    to, with the reason and a confidence. Nothing is stored or created; the file is uploaded
    for real once the arrangement is sent, after the person confirmed where it goes.
    """
    runtime = get_runtime()
    try:
        llm = (organization_llm(db, runtime.session_factory, settings, principal.organization_id,
                                fallback=lambda: runtime.llm, user_id=principal.user_id)
               if principal.organization_id else runtime.llm)
    except LLMUnavailableError:  # no model the organization may use: the layout alone suggests the name
        llm = None
    return ok(suggest_policy(
        db, principal, settings, file.file, intended_policy_id=policy_id,
        llm_factory=(lambda: llm) if llm is not None else None, classify_hierarchy=classify,
    ))


@router.post("/hierarchy-plan", response_model=ApiResponse[HierarchyPlan])
def hierarchy_plan(
    body: HierarchyPlanRequest, principal: Uploader, db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_app_settings)],
):
    """Suggest main policies and sub-policies for every document of an upload at once ("Loan Policy" with
    "Mortgage" and "Personal Loan"), reusing existing folders by name. Nothing is created: the person applies
    the plan in the upload screen, which creates the new folders it names."""
    runtime = get_runtime()
    try:
        llm = (organization_llm(db, runtime.session_factory, settings, principal.organization_id,
                                fallback=lambda: runtime.llm, user_id=principal.user_id)
               if principal.organization_id else runtime.llm)
    except LLMUnavailableError:
        llm = None
    organization_id = tenant_id(principal)
    categories = list(db.scalars(select(Category).where(Category.organization_id == organization_id)))
    # An existing document stays where it is filed; only documents the caller can see count.
    policy_ids = [d.policy_id for d in body.documents if d.policy_id]
    folders = dict(db.execute(
        select(Policy.id, Policy.category_id).where(Policy.id.in_(policy_ids), policy_visible(principal))
    ).all()) if policy_ids else {}
    placements, source, ai_reached = plan_hierarchy(
        categories,
        [PlanInput(d.key, d.name, d.about, folders.get(d.policy_id)) for d in body.documents],
        (lambda: llm) if llm is not None else None,
    )
    return ok(HierarchyPlan(placements=[PlannedPlacement(**p) for p in placements], source=source, ai_available=ai_reached))


@router.post("/batches", response_model=ApiResponse[BatchRead], status_code=201)
def create_batch(body: BatchCreate, principal: Uploader, service: Service):
    """Plan a bulk upload: policies (new or existing) and each one's files in version order, oldest first."""
    batch = service.create(principal, body)
    return ok(service.get(principal, batch.id))


@router.post("/batches/{batch_id}/items/{item_id}/file", response_model=ApiResponse[BatchItemRead])
def upload_item(
    batch_id: uuid.UUID, item_id: uuid.UUID, principal: Uploader, service: Service,
    file: Annotated[UploadFile, File()],
    allow_duplicate: Annotated[bool, Form()] = False,
    duplicate_reason: Annotated[str | None, Form(max_length=1000)] = None,
):
    """Send one planned file. A file that cannot be accepted is marked failed; the upload continues.

    A file identical to an existing document is refused unless `allow_duplicate`
    is set with a reason (recorded in the audit log).
    """
    item = service.upload_item(
        principal, batch_id, item_id, file=file.file, filename=file.filename or "", content_type=file.content_type,
        allow_duplicate=allow_duplicate, duplicate_reason=duplicate_reason,
    )
    return ok(_item_read(service, principal, batch_id, item.id))


@router.post("/batches/{batch_id}/items/{item_id}/cancel", response_model=ApiResponse[BatchItemRead])
def cancel_item(batch_id: uuid.UUID, item_id: uuid.UUID, request: Request, principal: Uploader, service: Service):
    """Cancel a planned file that has not been sent yet."""
    item = service.cancel_item(principal, batch_id, item_id, request.app.state.session_factory)
    return ok(_item_read(service, principal, batch_id, item.id))


def _item_read(service: UploadBatchService, principal: Principal, batch_id: uuid.UUID, item_id: uuid.UUID) -> BatchItemRead:
    return next(i for g in service.get(principal, batch_id).groups for i in g.items if i.id == item_id)


@router.post("/batches/{batch_id}/start", response_model=ApiResponse[BatchRead])
def start_batch(batch_id: uuid.UUID, request: Request, principal: Uploader, service: Service):
    """All files sent: stop waiting for missing ones and confirm every group whose files are analysed."""
    service.start(principal, batch_id, request.app.state.session_factory)
    return ok(service.get(principal, batch_id))


@router.post("/batches/{batch_id}/publish", response_model=ApiResponse[BatchRead])
def publish_batch(batch_id: uuid.UUID, request: Request, principal: Uploader, service: Service):
    """Publish an upload saved as a draft: its analysed files are registered as planned (files the
    analysis flagged still wait for a review). Publishing a published upload changes nothing."""
    service.publish(principal, batch_id, request.app.state.session_factory)
    return ok(service.get(principal, batch_id))


@router.get("/batches", response_model=ApiResponse[list[BatchSummary]])
def list_batches(principal: Uploader, service: Service):
    return ok(service.list(principal))


@router.get("/batches/{batch_id}", response_model=ApiResponse[BatchRead])
def get_batch(batch_id: uuid.UUID, principal: Uploader, service: Service):
    return ok(service.get(principal, batch_id))
