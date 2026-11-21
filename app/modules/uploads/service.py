"""Bulk uploads with version management.

    create  the person arranges files into policies (new or existing), each
            policy's files in version order, optionally with labels and dates
    upload  each file enters the normal pipeline (validation, duplicate check,
            extraction, analysis) linked to its place in the plan
    advance once every file of a group has been analysed, the group is
            confirmed oldest-first, exactly as a reviewer would confirm it

Nothing is forced through: a file the analysis flags (duplicate, version
conflict, a match with a different existing policy) or that the confirmation
refuses is left for a person to review, and the rest of its group continues.
Undated versions keep the arranged order (see `plan_dates`).
"""

import logging
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import BinaryIO

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.core.exceptions import AppError, ConflictError, NotFoundError, PermissionDeniedError, ValidationError
from app.infrastructure.queue.base import Queue
from app.infrastructure.storage.base import Storage
from app.modules.audit.service import record_event
from app.modules.auth.acl import can_see, can_write_scope, ensure_can_write_scope
from app.modules.auth.permissions import Permission, Principal, Role
from app.modules.auth.scope import tenant_id
from app.modules.categories.model import Category
from app.modules.categories.repository import CategoryRepository
from app.modules.categories.tree import path_names
from app.modules.documents.model import Document, DocumentStatus
from app.modules.documents.service import DocumentService
from app.modules.ingestion.confirmation import ConfirmationService, ConfirmRequest, detected_effective_date
from app.modules.ingestion.model import Decision, DocumentAnalysis
from app.modules.ingestion.schema import PolicyInput, VersionInput
from app.modules.policies.model import DateSource, Policy, PolicyStatus, PolicyVersion, VersionStatus
from app.modules.uploads.model import (
    BatchStatus,
    GroupStatus,
    ItemStatus,
    UploadBatch,
    UploadBatchGroup,
    UploadBatchItem,
)
from app.modules.uploads.schema import (
    BatchCreate,
    BatchGroupIn,
    BatchGroupRead,
    BatchItemRead,
    BatchRead,
    BatchSummary,
)

logger = logging.getLogger(__name__)

# The analysis warns about these; a bulk upload never confirms them unattended.
FLAGGED_DECISIONS = {
    Decision.EXACT_DUPLICATE: "The same content already exists",
    Decision.CONTENT_DUPLICATE: "Near-identical content already exists",
    Decision.VERSION_CONFLICT: "The detected version clashes with an existing version",
}
# The analysis tied this upload to an existing policy, so where it is filed matters.
CROSS_POLICY_DECISIONS = {
    Decision.EXISTING_POLICY_NEW_VERSION,
    Decision.POSSIBLE_MATCH_REQUIRES_REVIEW,
}
SETTLED_DOCUMENT_STATES = {
    DocumentStatus.AWAITING_CONFIRMATION, DocumentStatus.FAILED, DocumentStatus.REJECTED,
    DocumentStatus.INDEXING, DocumentStatus.READY, DocumentStatus.ARCHIVED,
}
TERMINAL_ITEM_STATES = {ItemStatus.CONFIRMED, ItemStatus.NEEDS_REVIEW, ItemStatus.FAILED, ItemStatus.CANCELLED}
# Finished without anything left for a person to do.
DONE_ITEM_STATES = {ItemStatus.CONFIRMED, ItemStatus.CANCELLED}
DRAFT_MESSAGE = "Saved as a draft: publish the upload to register these versions."
# "V2", "v 2", "Version 2" and "2" are the same version label.
_LABEL_PREFIX = re.compile(r"^\s*(?:version|ver\.?|v)\s*(?=\d)", re.IGNORECASE)


def clean_label(label: str | None) -> str | None:
    """A version label as entered, without a "v"/"version" prefix before its number."""
    text = _LABEL_PREFIX.sub("", (label or "").strip()).strip()
    return text[:50] or None


def _same_label(label: str) -> str:
    return clean_label(label).lower() if clean_label(label) else ""


class UploadBatchService:
    def __init__(self, session: Session, storage: Storage, queue: Queue, settings: Settings) -> None:
        self.session = session
        self.storage = storage
        self.queue = queue
        self.settings = settings
        self.categories = CategoryRepository(session)

    # --- plan --------------------------------------------------------------------

    def create(self, principal: Principal, request: BatchCreate) -> UploadBatch:
        if not principal.has(Permission.POLICIES_MANAGE):
            # A bulk upload registers versions without a per-file review.
            raise PermissionDeniedError("Bulk upload with version management requires policy management rights.")
        organization_id = tenant_id(principal)
        documents = DocumentService(self.session, self.storage, self.queue, self.settings)
        branch_id, department_id = documents._resolve_scope(  # noqa: SLF001 - same scope rules as a single upload
            principal, request.branch_id, request.department_id, use_default=True
        )
        batch = UploadBatch(
            organization_id=organization_id, created_by_id=principal.user_id,
            branch_id=branch_id, department_id=department_id, status=BatchStatus.UPLOADING,
            publish=request.publish,
        )
        self.session.add(batch)
        self.session.flush()
        overridden = 0
        for group_position, group_in in enumerate(request.groups):
            policy = self._target_policy(principal, group_in.policy_id, for_approval=True) if group_in.policy_id else None
            if group_in.category_id is not None:
                category = self.categories.get_in_org(organization_id, group_in.category_id)
                if category is None or not category.is_active:
                    raise ValidationError("Category not found.")
            name = " ".join((group_in.new_policy_name or "").split()) or None
            self._check_labels(group_in, policy, name)
            group = UploadBatchGroup(
                organization_id=organization_id, batch_id=batch.id, position=group_position,
                policy_id=policy.id if policy else None, new_policy_name=name,
                category_id=policy.category_id if policy else group_in.category_id, status=GroupStatus.PENDING,
            )
            self.session.add(group)
            self.session.flush()
            for position, item_in in enumerate(group_in.items):
                self.session.add(UploadBatchItem(
                    organization_id=organization_id, batch_id=batch.id, group_id=group.id, position=position,
                    original_filename=item_in.filename[:500],
                    version_label=clean_label(item_in.version_label),
                    effective_from=item_in.effective_from, status=ItemStatus.AWAITING_FILE,
                ))
            overridden += self._record_classification(principal, batch, group, group_in, policy)
        record_event(
            self.session, "upload_batch.created", actor=principal, resource_type="upload_batch",
            resource_id=batch.id,
            details={"groups": len(request.groups), "files": sum(len(g.items) for g in request.groups),
                     "publish": request.publish,
                     "ai_classified": sum(1 for g in request.groups if g.classification is not None),
                     "classification_overridden": overridden},
        )
        self.session.commit()
        return batch

    def _check_labels(self, group_in: BatchGroupIn, policy: Policy | None, name: str | None) -> None:
        """Version labels are unique within one document: refuse a plan that repeats one, or that gives an
        existing document a label it already has, before any file is sent."""
        what = f'"{policy.name}"' if policy else (f'"{name}"' if name else "this document")
        seen: dict[str, str] = {}
        for item in group_in.items:
            if not (key := _same_label(item.version_label)):
                continue
            if key in seen:
                raise ValidationError(
                    f"Version {clean_label(item.version_label)} is given twice for {what} "
                    f"({seen[key]} and {item.filename}).", code="DUPLICATE_VERSION_LABEL",
                )
            seen[key] = item.filename
        if policy is None or not seen:
            return
        taken = {
            _same_label(label): label
            for label in self.session.scalars(select(PolicyVersion.version_label).where(
                PolicyVersion.policy_id == policy.id, PolicyVersion.status == VersionStatus.ACTIVE,
            ))
        }
        if clash := next((key for key in seen if key in taken), None):
            raise ConflictError(
                f"{what} already has version {taken[clash]} ({seen[clash]}). Choose another version number; "
                "an earlier version is never overwritten.",
                code="VERSION_CONFLICT", details={"type": "VERSION_LABEL_EXISTS", "label": taken[clash]},
            )

    def _record_classification(self, principal: Principal, batch: UploadBatch, group: UploadBatchGroup,
                               group_in: BatchGroupIn, policy: Policy | None) -> int:
        """Record in the audit log when the administrator filed a group elsewhere than the AI suggested."""
        suggestion = group_in.classification
        if suggestion is None or suggestion.source in (None, "none"):
            return 0
        chosen_category = self.session.get(Category, group.category_id) if group.category_id else None
        suggested_category = (self.categories.get_in_org(batch.organization_id, suggestion.category_id)
                              if suggestion.category_id else None)
        if suggestion.policy_id is not None:
            agreed = policy is not None and policy.id == suggestion.policy_id
        elif suggested_category is not None:
            agreed = policy is None and chosen_category is not None and chosen_category.id == suggested_category.id
        elif suggestion.new_sub_policy_name:
            # Accepting a new sub-policy means it was created under the suggested main policy and chosen.
            agreed = (policy is None and chosen_category is not None
                      and chosen_category.name.lower() == suggestion.new_sub_policy_name.lower()
                      and chosen_category.parent_id == suggestion.main_policy_id)
        else:
            return 0
        if agreed:
            return 0
        names = path_names(self.session, [c for c in (chosen_category, suggested_category) if c is not None])
        suggested = (
            f"new version of {self._policy_name(suggestion.policy_id, batch.organization_id)}" if suggestion.policy_id
            else names.get(suggested_category.id) if suggested_category is not None
            else f"new sub-policy {suggestion.new_sub_policy_name}"
        )
        chosen = (f"new version of {policy.name}" if policy is not None
                  else names.get(chosen_category.id) if chosen_category is not None else "the detected folder")
        record_event(
            self.session, "classification.overridden", actor=principal, resource_type="upload_batch",
            resource_id=batch.id,
            details={"filename": group_in.items[0].filename, "files": len(group_in.items),
                     "suggested": suggested, "chosen": chosen, "source": suggestion.source,
                     "confidence": suggestion.confidence, "reason": suggestion.reason,
                     "suggested_category_id": str(suggestion.category_id) if suggestion.category_id else None,
                     "suggested_policy_id": str(suggestion.policy_id) if suggestion.policy_id else None,
                     "chosen_category_id": str(group.category_id) if group.category_id else None,
                     "chosen_policy_id": str(policy.id) if policy else None},
        )
        return 1

    def _policy_name(self, policy_id: uuid.UUID, organization_id: uuid.UUID) -> str:
        """The suggested document's name for the audit log; never another organisation's."""
        policy = self.session.get(Policy, policy_id)
        return policy.name if policy is not None and policy.organization_id == organization_id else "another document"

    def _target_policy(self, principal: Principal, policy_id: uuid.UUID, *, for_approval: bool = False) -> Policy:
        """The existing policy a group adds versions to. With for_approval, one this person can see but does not
        manage (a Branch Manager and a bank-wide policy) is accepted too: its files are uploaded and read, and an
        Org Admin approves them before they are registered (_confirm_group). Nothing is changed in it meanwhile."""
        policy = self.session.get(Policy, policy_id)
        if policy is None or not can_see(principal, policy.organization_id, policy.branch_id, policy.department_id, policy.id):
            raise NotFoundError("Policy not found.")
        if policy.status != PolicyStatus.ACTIVE:
            raise ConflictError("The policy is archived.", code="INVALID_STATE")
        if not (for_approval and not can_write_scope(principal, policy.branch_id, policy.department_id)):
            ensure_can_write_scope(principal, policy.branch_id, policy.department_id)
        return policy

    # --- files -------------------------------------------------------------------

    def upload_item(
        self, principal: Principal, batch_id: uuid.UUID, item_id: uuid.UUID, *, file: BinaryIO, filename: str,
        content_type: str | None, allow_duplicate: bool = False, duplicate_reason: str | None = None,
    ) -> UploadBatchItem:
        batch = self._visible_batch(principal, batch_id)
        item = self.session.get(UploadBatchItem, item_id)
        if item is None or item.batch_id != batch.id:
            raise NotFoundError("Upload item not found.")
        if item.status != ItemStatus.AWAITING_FILE:
            raise ConflictError("This file has already been received.", code="INVALID_STATE")
        group = self.session.get(UploadBatchGroup, item.group_id)
        documents = DocumentService(self.session, self.storage, self.queue, self.settings)
        try:
            document = documents.upload(
                principal, file=file, filename=filename or item.original_filename, content_type=content_type,
                branch_id=batch.branch_id, department_id=batch.department_id, category_id=group.category_id,
                allow_duplicate=allow_duplicate, duplicate_reason=duplicate_reason,
            )
        except AppError as exc:
            # One bad file (not a PDF, identical to an existing upload, too large)
            # must not stop the rest of the upload.
            self.session.rollback()
            item = self.session.get(UploadBatchItem, item_id)
            item.status, item.message = ItemStatus.FAILED, exc.message
            self.session.commit()
            return item
        item = self.session.get(UploadBatchItem, item_id)
        item.document_id, item.status, item.message = document.id, ItemStatus.PROCESSING, None
        batch = self.session.get(UploadBatch, batch_id)
        if not self.session.scalar(select(UploadBatchItem.id).where(
            UploadBatchItem.batch_id == batch.id, UploadBatchItem.status == ItemStatus.AWAITING_FILE
        ).limit(1)):
            batch.status = BatchStatus.PROCESSING
        self.session.commit()
        return item

    def cancel_item(
        self, principal: Principal, batch_id: uuid.UUID, item_id: uuid.UUID, session_factory: sessionmaker[Session]
    ) -> UploadBatchItem:
        """Drop a planned file that has not been received. A received file is already in the pipeline."""
        batch = self._visible_batch(principal, batch_id)
        item = self.session.get(UploadBatchItem, item_id)
        if item is None or item.batch_id != batch.id:
            raise NotFoundError("Upload item not found.")
        if item.status == ItemStatus.CANCELLED:
            return item
        if item.status != ItemStatus.AWAITING_FILE:
            raise ConflictError("This file has already been received.", code="INVALID_STATE")
        item.status, item.message = ItemStatus.CANCELLED, "Cancelled before upload."
        self.session.flush()
        refresh_statuses(self.session, batch.id)
        self.session.commit()
        # The rest of its group may have been waiting only for this file.
        advance_group(session_factory, self.queue, item.group_id)
        self.session.expire_all()
        return self.session.get(UploadBatchItem, item_id)

    def start(self, principal: Principal, batch_id: uuid.UUID, session_factory: sessionmaker[Session]) -> UploadBatch:
        """All files have been sent: stop waiting for any that never arrived, then advance every group."""
        batch = self._visible_batch(principal, batch_id)
        for item in self.session.scalars(select(UploadBatchItem).where(
            UploadBatchItem.batch_id == batch.id, UploadBatchItem.status == ItemStatus.AWAITING_FILE
        )):
            item.status, item.message = ItemStatus.FAILED, "The file was not received."
        if batch.status == BatchStatus.UPLOADING:
            batch.status = BatchStatus.PROCESSING
        self.session.commit()
        group_ids = list(self.session.scalars(select(UploadBatchGroup.id).where(UploadBatchGroup.batch_id == batch.id)))
        for group_id in group_ids:
            advance_group(session_factory, self.queue, group_id)
        self.session.expire_all()
        return self.session.get(UploadBatch, batch_id)

    def publish(self, principal: Principal, batch_id: uuid.UUID, session_factory: sessionmaker[Session]) -> UploadBatch:
        """Publish a draft upload: register every analysed file as planned, as a published upload would.

        Files the analysis flagged still wait for a person; the versions are registered by the publisher.
        """
        if not principal.has(Permission.POLICIES_MANAGE):
            raise PermissionDeniedError("Publishing versions requires policy management rights.")
        batch = self._visible_batch(principal, batch_id)
        if batch.publish:
            return batch
        batch.publish = True
        if batch.status == BatchStatus.DRAFT:
            batch.status = BatchStatus.PROCESSING
        groups = list(self.session.scalars(select(UploadBatchGroup).where(UploadBatchGroup.batch_id == batch.id)))
        for group in groups:
            if group.message == DRAFT_MESSAGE:
                group.message = None
        files = self.session.scalar(select(func.count()).select_from(UploadBatchItem).where(UploadBatchItem.batch_id == batch.id))
        record_event(
            self.session, "upload_batch.published", actor=principal, resource_type="upload_batch",
            resource_id=batch.id, details={"files": files, "groups": len(groups)},
        )
        self.session.commit()
        for group in groups:
            advance_group(session_factory, self.queue, group.id, actor_id=principal.user_id)
        self.session.expire_all()
        return self.session.get(UploadBatch, batch_id)

    # --- reading -------------------------------------------------------------------

    def _visible_batch(self, principal: Principal, batch_id: uuid.UUID) -> UploadBatch:
        batch = self.session.get(UploadBatch, batch_id)
        if batch is None or batch.organization_id != principal.organization_id:
            raise NotFoundError("Upload not found.")
        if principal.role is not Role.ORG_ADMIN and batch.created_by_id != principal.user_id:
            raise NotFoundError("Upload not found.")
        return batch

    def get(self, principal: Principal, batch_id: uuid.UUID) -> BatchRead:
        batch = self._visible_batch(principal, batch_id)
        groups = self.session.scalars(
            select(UploadBatchGroup).where(UploadBatchGroup.batch_id == batch.id).order_by(UploadBatchGroup.position)
        ).all()
        items = self.session.scalars(
            select(UploadBatchItem).where(UploadBatchItem.batch_id == batch.id).order_by(UploadBatchItem.position)
        ).all()
        document_states = dict(self.session.execute(
            select(Document.id, Document.status).where(Document.id.in_([i.document_id for i in items if i.document_id]))
        ).all()) if items else {}
        policy_names = dict(self.session.execute(
            select(Policy.id, Policy.name).where(Policy.id.in_([g.policy_id for g in groups if g.policy_id]))
        ).all()) if groups else {}
        by_group: dict[uuid.UUID, list[BatchItemRead]] = {}
        for item in items:
            by_group.setdefault(item.group_id, []).append(BatchItemRead(
                id=item.id, position=item.position, original_filename=item.original_filename,
                document_id=item.document_id, document_status=document_states.get(item.document_id),
                version_label=item.version_label, effective_from=item.effective_from, status=item.status,
                message=item.message, policy_version_id=item.policy_version_id,
            ))
        return BatchRead(
            id=batch.id, status=batch.status, publish=batch.publish, created_at=batch.created_at, completed_at=batch.completed_at,
            created_by_id=batch.created_by_id, counts=_counts(items),
            groups=[BatchGroupRead(
                id=g.id, position=g.position, policy_id=g.policy_id, policy_name=policy_names.get(g.policy_id),
                new_policy_name=g.new_policy_name, category_id=g.category_id, status=g.status, message=g.message,
                items=by_group.get(g.id, []),
            ) for g in groups],
        )

    def list(self, principal: Principal, limit: int = 20) -> list[BatchSummary]:
        query = select(UploadBatch).where(UploadBatch.organization_id == principal.organization_id)
        if principal.role is not Role.ORG_ADMIN:
            query = query.where(UploadBatch.created_by_id == principal.user_id)
        batches = self.session.scalars(query.order_by(UploadBatch.created_at.desc()).limit(limit)).all()
        items = self.session.scalars(
            select(UploadBatchItem).where(UploadBatchItem.batch_id.in_([b.id for b in batches]))
        ).all() if batches else []
        grouped: dict[uuid.UUID, list[UploadBatchItem]] = {}
        for item in items:
            grouped.setdefault(item.batch_id, []).append(item)
        return [BatchSummary(id=b.id, status=b.status, publish=b.publish, created_at=b.created_at, completed_at=b.completed_at,
                             counts=_counts(grouped.get(b.id, []))) for b in batches]


def _counts(items) -> dict[str, int]:
    counts = {status.value: 0 for status in ItemStatus}
    for item in items:
        counts[item.status] = counts.get(item.status, 0) + 1
    counts["total"] = len(items)
    return counts


# --- carrying out the plan -----------------------------------------------------------


@dataclass
class PlannedDate:
    effective_from: date
    source: str
    note: str | None = None


def plan_dates(entered: list[date | None], stated: list[date | None], newest_default: date) -> list[PlannedDate]:
    """Effective dates for versions given oldest-first, keeping the arranged order.

    Entered dates are used as they are. Otherwise the date the document states
    is used when it fits the order; the newest undated version gets
    `newest_default` (its upload date) and each older undated version the day
    before its successor, so the timeline follows the order the person chose.
    """
    planned: list[PlannedDate | None] = [None] * len(entered)
    following: date | None = None
    for index in range(len(entered) - 1, -1, -1):
        if entered[index] is not None:
            planned[index] = PlannedDate(entered[index], DateSource.ENTERED)
        elif stated[index] is not None and (following is None or stated[index] < following):
            planned[index] = PlannedDate(stated[index], DateSource.DETECTED)
        elif following is None:
            planned[index] = PlannedDate(newest_default, DateSource.UPLOAD_DATE)
        else:
            note = ("The document's own date is not earlier than the next version's; "
                    "ordered as arranged.") if stated[index] is not None else None
            planned[index] = PlannedDate(following - timedelta(days=1), DateSource.INFERRED, note)
        following = planned[index].effective_from
    return planned  # type: ignore[return-value]


def advance_for_document(session_factory: sessionmaker[Session], queue: Queue, document_id: uuid.UUID) -> None:
    """Called when a document's analysis finished or it failed: advance its group, if it has one."""
    with session_factory() as session:
        group_id = session.scalar(
            select(UploadBatchItem.group_id).where(UploadBatchItem.document_id == document_id)
        )
    if group_id is not None:
        advance_group(session_factory, queue, group_id)


def advance_group(
    session_factory: sessionmaker[Session], queue: Queue, group_id: uuid.UUID, *, actor_id: uuid.UUID | None = None,
) -> None:
    """Confirm a group once all of its files have settled. Safe to call any number of times.

    A draft batch stops here: its files stay analysed and waiting, nothing is registered, until it is
    published. `actor_id` is who registers the versions (the publisher); the batch's creator otherwise.
    """
    with session_factory() as session:
        # One transaction; the group row lock serialises concurrent callers (two
        # analyses finishing at once), and each version runs in its own savepoint.
        group = session.scalar(select(UploadBatchGroup).where(UploadBatchGroup.id == group_id).with_for_update())
        if group is None or group.status != GroupStatus.PENDING:
            return
        items = session.scalars(
            select(UploadBatchItem).where(UploadBatchItem.group_id == group.id).order_by(UploadBatchItem.position)
        ).all()
        if any(item.status == ItemStatus.AWAITING_FILE for item in items):
            return  # files still arriving
        documents = {d.id: d for d in session.scalars(
            select(Document).where(Document.id.in_([i.document_id for i in items if i.document_id]))
        )}
        pending = [i for i in items if i.status == ItemStatus.PROCESSING]
        if any(documents[i.document_id].status not in SETTLED_DOCUMENT_STATES for i in pending):
            return  # still in the pipeline
        batch = session.get(UploadBatch, group.batch_id)
        if not batch.publish:
            for item in pending:
                document = documents[item.document_id]
                if document.status == DocumentStatus.FAILED:
                    item.status, item.message = ItemStatus.FAILED, (document.error or {}).get("message") or "Processing failed."
            group.message = DRAFT_MESSAGE
            session.flush()
            refresh_statuses(session, group.batch_id)
            _mark_draft(session, batch)
            session.commit()
            return
        _confirm_group(session, queue, group, pending, documents, actor_id=actor_id)
        refresh_statuses(session, group.batch_id)
        session.commit()


def _mark_draft(session: Session, batch: UploadBatch) -> None:
    """A draft batch whose files have all arrived and been analysed is ready to publish."""
    if batch.status in (BatchStatus.COMPLETED, BatchStatus.ATTENTION):
        return
    items = session.scalars(select(UploadBatchItem).where(UploadBatchItem.batch_id == batch.id)).all()
    if any(item.status == ItemStatus.AWAITING_FILE for item in items):
        return
    waiting = [item.document_id for item in items if item.status == ItemStatus.PROCESSING and item.document_id]
    states = set(session.scalars(select(Document.status).where(Document.id.in_(waiting)))) if waiting else set()
    if states <= SETTLED_DOCUMENT_STATES:
        batch.status = BatchStatus.DRAFT


def _confirm_group(session, queue, group, pending, documents, actor_id: uuid.UUID | None = None) -> None:
    from app.modules.auth.dependencies import principal_from_user
    from app.modules.users.model import User

    batch = session.get(UploadBatch, group.batch_id)
    user_id = actor_id or batch.created_by_id
    user = session.get(User, user_id) if user_id else None
    principal = principal_from_user(user, session) if user is not None and user.is_active else None
    analyses = {a.document_id: a for a in session.scalars(
        select(DocumentAnalysis).where(DocumentAnalysis.document_id.in_([i.document_id for i in pending]))
    )}

    confirmable = []
    for item in pending:
        document, analysis = documents[item.document_id], analyses.get(item.document_id)
        if document.status == DocumentStatus.FAILED:
            item.status, item.message = ItemStatus.FAILED, (document.error or {}).get("message") or "Processing failed."
        elif document.status != DocumentStatus.AWAITING_CONFIRMATION or analysis is None:
            item.status = ItemStatus.NEEDS_REVIEW
            item.message = f"The document is {document.status.replace('_', ' ')}."
        elif principal is None or not principal.has(Permission.POLICIES_MANAGE):
            item.status, item.message = ItemStatus.NEEDS_REVIEW, "Needs confirmation by a policy manager."
        elif (target := session.get(Policy, group.policy_id) if group.policy_id else None) is not None and not can_write_scope(
                principal, target.branch_id, target.department_id):
            # A new version of a policy this person does not manage (a bank-wide policy): an Org Admin approves it.
            item.status = ItemStatus.NEEDS_REVIEW
            item.message = f"Sent to your Org Admin for approval as a new version of “{target.name}”."
            _request_approval(session, principal, document, target)
        elif (flag := _flag(analysis, group)) is not None:
            item.status, item.message = ItemStatus.NEEDS_REVIEW, flag
        else:
            confirmable.append((item, document, analysis))
    if not confirmable:
        return

    today = datetime.now(UTC).date()
    newest_document = confirmable[-1][1]
    planned = plan_dates(
        [item.effective_from for item, _, _ in confirmable],
        [detected_effective_date(analysis) for _, _, analysis in confirmable],
        newest_document.created_at.date() if newest_document.created_at else today,
    )
    service = ConfirmationService(session, queue)
    newest_analysis = confirmable[-1][2]
    for (item, document, analysis), dated in zip(confirmable, planned, strict=True):
        label = item.version_label or (analysis.detected or {}).get("version_label", {}).get("value") or None
        version = VersionInput(version_label=label, effective_from=dated.effective_from)
        if group.policy_id is None:
            name = group.new_policy_name or newest_analysis.suggested_name or analysis.suggested_name
            category_id = group.category_id or newest_analysis.suggested_category_id or analysis.suggested_category_id
            if not name or not category_id:
                item.status = ItemStatus.NEEDS_REVIEW
                item.message = "Choose a policy name and category for this file." if not name else "Choose a category."
                continue
            request = ConfirmRequest(action="create_policy", version=version, policy=PolicyInput(
                name=name, category_id=category_id,
                policy_number=(analysis.detected or {}).get("policy_number", {}).get("value"),
                branch_id=batch.branch_id, department_id=batch.department_id,
            ))
        else:
            request = ConfirmRequest(action="add_version", policy_id=group.policy_id, version=version)
        savepoint = session.begin_nested()
        try:
            confirmed = service.confirm(principal, document.id, request, effective_date_source=dated.source, commit=False)
            savepoint.commit()
        except AppError as exc:
            savepoint.rollback()
            item.status, item.message = ItemStatus.NEEDS_REVIEW, exc.message
            continue
        group.policy_id = confirmed.policy_id
        item.status, item.policy_version_id = ItemStatus.CONFIRMED, confirmed.policy_version_id
        item.message = dated.note
        if item.version_label is None:
            item.version_label = label
        if item.effective_from is None:
            item.effective_from = dated.effective_from


def _request_approval(session: Session, principal, document: Document, policy: Policy) -> None:
    """Tell the organisation's Org Admins that a new version waits for them, once per file."""
    from app.modules.notifications import service as notifications
    from app.modules.notifications.model import NotificationType
    from app.modules.users.model import User

    admins = session.scalars(select(User.id).where(
        User.organization_id == policy.organization_id, User.role == Role.ORG_ADMIN, User.is_active,
    )).all()
    uploader = session.get(User, principal.user_id) if principal.user_id else None
    who = uploader.full_name if uploader else "A branch manager"
    notifications.notify(
        session, admins, NotificationType.VERSION_APPROVAL,
        f"New version of {policy.name} waiting for your approval",
        organization_id=policy.organization_id,
        body=f"{who} uploaded {document.original_filename} as a new version of {policy.name}. Review it to register it.",
        link=f"/documents/{document.id}",
        data={"document_id": str(document.id), "policy_id": str(policy.id)},
        dedupe_key=f"approval:{document.id}",
    )
    record_event(
        session, "document.approval_requested", actor=principal, resource_type="document", resource_id=document.id,
        details={"filename": document.original_filename, "policy": policy.name, "policy_id": str(policy.id)},
    )


def _flag(analysis: DocumentAnalysis, group: UploadBatchGroup) -> str | None:
    if analysis.decision in FLAGGED_DECISIONS:
        return f"{FLAGGED_DECISIONS[analysis.decision]}: review it before it is registered."
    if analysis.matched_policy_id is None or analysis.decision not in CROSS_POLICY_DECISIONS:
        return None
    if group.policy_id is None:
        # Creating a second policy for the same document family would split its history.
        return "This looks like a new version of an existing policy: review it, or move it to that policy."
    if analysis.matched_policy_id != group.policy_id:
        # Arranged under one policy, but the content is a version of another: filing it
        # silently would attach a document to the wrong policy's history.
        return f"Its content looks like it belongs to {matched_name(analysis)}: review it, or add it to this policy anyway."
    return None


def matched_name(analysis: DocumentAnalysis) -> str:
    """The policy the analysis matched, named for the message that points at it."""
    matched = str(analysis.matched_policy_id)
    for candidate in analysis.candidates or []:
        if candidate.get("policy_id") == matched and candidate.get("name"):
            return f"“{candidate['name']}”"
    return "another existing policy"


def refresh_statuses(session: Session, batch_id: uuid.UUID) -> None:
    """Derive group and batch status from their items."""
    batch = session.get(UploadBatch, batch_id)
    groups = session.scalars(select(UploadBatchGroup).where(UploadBatchGroup.batch_id == batch_id)).all()
    items = session.scalars(select(UploadBatchItem).where(UploadBatchItem.batch_id == batch_id)).all()
    by_group: dict[uuid.UUID, list[UploadBatchItem]] = {}
    for item in items:
        by_group.setdefault(item.group_id, []).append(item)
    for group in groups:
        states = {item.status for item in by_group.get(group.id, [])}
        if states and states <= TERMINAL_ITEM_STATES:
            group.status = GroupStatus.CONFIRMED if states <= DONE_ITEM_STATES else GroupStatus.ATTENTION
    if all(g.status != GroupStatus.PENDING for g in groups):
        states = {item.status for item in items}
        batch.status = BatchStatus.COMPLETED if states <= DONE_ITEM_STATES else BatchStatus.ATTENTION
        batch.completed_at = batch.completed_at or datetime.now(UTC)
    elif batch.status == BatchStatus.UPLOADING and not any(i.status == ItemStatus.AWAITING_FILE for i in items):
        batch.status = BatchStatus.PROCESSING
