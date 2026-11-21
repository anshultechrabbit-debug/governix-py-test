"""What a category holds: documents (business identities), their versions, and uploads in progress.

Counts and listings only ever cover what the caller can see (branch and
department scope), so a category's numbers differ between people.
"""

import uuid
from datetime import date

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.modules.auth.acl import visible_clause
from app.modules.auth.permissions import Principal
from app.modules.categories.model import Category
from app.modules.categories.schema import (
    CategoryDocument,
    CategoryRead,
    CategorySummary,
    CategoryTreeNode,
    CategoryVersion,
    PendingUpload,
    lifecycle,
)
from app.modules.documents.model import Document, DocumentStatus
from app.modules.policies.model import Policy, PolicyVersion, VersionStatus
from app.modules.policies.repository import arranged_policy_order, policy_visible
from app.modules.policies.schema import VersionRead
from app.modules.uploads.model import ItemStatus, UploadBatch, UploadBatchGroup, UploadBatchItem
from app.modules.users.model import User
from app.modules.versions.timeline import effective_on, timeline_state

VERSION_STATES = ("current", "historical", "scheduled", "withdrawn")
SORTS = ("custom", "name", "uploaded", "version")
# Uploaded into the category but not (yet) a version of a document.
PENDING_EXCLUDED = (DocumentStatus.REJECTED, DocumentStatus.ARCHIVED)
MAX_PENDING = 100


def _stats(session: Session, principal: Principal, ids: list[uuid.UUID]) -> dict[uuid.UUID, tuple]:
    """Per category: (documents, active versions, last policy change, last version change), over what the caller sees."""
    if not ids:
        return {}
    rows = session.execute(
        select(
            Policy.category_id,
            func.count(func.distinct(Policy.id)),
            func.count(func.distinct(PolicyVersion.id)).filter(PolicyVersion.status == VersionStatus.ACTIVE),
            func.max(Policy.updated_at),
            func.max(PolicyVersion.updated_at),
        )
        .outerjoin(PolicyVersion, PolicyVersion.policy_id == Policy.id)
        .where(Policy.category_id.in_(ids), policy_visible(principal))
        .group_by(Policy.category_id)
    ).all()
    return {row[0]: tuple(row[1:]) for row in rows}


def _summary(category: Category, stats: tuple, children: list[tuple[Category, tuple]] = ()) -> CategorySummary:
    documents, versions, policy_activity, version_activity = stats
    times = [category.updated_at, policy_activity, version_activity]
    for child, (_, _, child_policy, child_version) in children:
        times += [child.updated_at, child_policy, child_version]
    return CategorySummary(
        **CategoryRead.model_validate(category).model_dump(),
        document_count=documents,
        version_count=versions,
        sub_policy_count=len(children),
        total_document_count=documents + sum(c[1][0] for c in children),
        total_version_count=versions + sum(c[1][1] for c in children),
        last_updated=max(t for t in times if t is not None),
    )


def summaries(
    session: Session, principal: Principal, categories: list[Category], *, include_inactive: bool = True,
) -> list[CategorySummary]:
    """Each category with what it holds; a main policy's totals include its sub-policies."""
    if not categories:
        return []
    mains = [c.id for c in categories if c.parent_id is None]
    query = select(Category).where(Category.parent_id.in_(mains)) if mains else None
    if query is not None and not include_inactive:
        query = query.where(Category.is_active.is_(True))
    children = list(session.scalars(query)) if query is not None else []
    stats = _stats(session, principal, list({c.id for c in [*categories, *children]}))
    by_parent: dict[uuid.UUID, list[tuple[Category, tuple]]] = {}
    for child in children:
        by_parent.setdefault(child.parent_id, []).append((child, stats.get(child.id, _EMPTY)))
    return [_summary(c, stats.get(c.id, _EMPTY), by_parent.get(c.id, [])) for c in categories]


_EMPTY = (0, 0, None, None)


def sibling_order():
    """Policy folders as arranged (drag and drop); not yet arranged ones after them, by authority then name."""
    return (Category.display_order.asc().nulls_last(), Category.authority_rank.desc(), func.lower(Category.name), Category.id)


def tree(
    session: Session, principal: Principal, organization_id: uuid.UUID, *, include_inactive: bool, search: str | None,
    include_empty_system: bool = False,
) -> list[CategoryTreeNode]:
    """Main policies with their sub-policies, in display order, with counts over what the caller sees.

    `search` keeps the main policies whose name, or a sub-policy's name, contains it; a matching main
    policy keeps all its sub-policies. The folders every organisation starts with, named after a kind of
    document ("Policies", "Circulars", "SOPs" ...), are left out while they hold nothing (no document, no
    sub-policy, no upload in progress): they are type buckets, not subjects. One that holds something is shown.
    """
    query = select(Category).where(Category.organization_id == organization_id)
    if not include_inactive:
        query = query.where(Category.is_active.is_(True))
    every = list(session.scalars(query.order_by(*sibling_order())))
    stats = _stats(session, principal, [c.id for c in every])
    mains = [c for c in every if c.parent_id is None]
    subs: dict[uuid.UUID, list[Category]] = {}
    for category in every:
        if category.parent_id is not None:
            subs.setdefault(category.parent_id, []).append(category)
    if not include_empty_system:
        holding = _holding_uploads(session, [c.id for c in mains if c.is_system])
        mains = [
            c for c in mains
            if not c.is_system or subs.get(c.id) or stats.get(c.id, _EMPTY)[0] or c.id in holding
        ]
    term = (search or "").strip().lower()
    nodes = []
    for main in mains:
        children = subs.get(main.id, [])
        if term and term not in main.name.lower():
            children = [c for c in children if term in c.name.lower()]
            if not children:
                continue
        node = CategoryTreeNode(
            **_summary(main, stats.get(main.id, _EMPTY), [(c, stats.get(c.id, _EMPTY)) for c in subs.get(main.id, [])]).model_dump(),
            sub_policies=[_summary(c, stats.get(c.id, _EMPTY)) for c in children],
        )
        nodes.append(node)
    return nodes


def _holding_uploads(session: Session, ids: list[uuid.UUID]) -> set[uuid.UUID]:
    """Folders with a file uploaded into them that is not a version yet (processing, waiting, a draft)."""
    if not ids:
        return set()
    return set(session.scalars(
        select(Document.category_id).where(
            Document.category_id.in_(ids), Document.policy_id.is_(None), Document.status.not_in(PENDING_EXCLUDED),
        ).distinct()
    ))


def _state_clause(state: str, as_of: date):
    """SQL mirror of versions.timeline.timeline_state."""
    if state == "withdrawn":
        return PolicyVersion.status == VersionStatus.WITHDRAWN
    active = PolicyVersion.status == VersionStatus.ACTIVE
    if state == "scheduled":
        return and_(active, PolicyVersion.effective_from > as_of)
    if state == "historical":
        return and_(active, PolicyVersion.effective_to.is_not(None), PolicyVersion.effective_to <= as_of)
    return and_(active, effective_on(as_of))


def _contains(term: str) -> str:
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def documents(
    session: Session,
    principal: Principal,
    category: Category,
    *,
    as_of: date,
    search: str | None,
    status: str | None,
    version_state: str | None,
    sort: str,
    limit: int,
    offset: int,
) -> tuple[list[CategoryDocument], int]:
    query = select(Policy).where(Policy.category_id == category.id, policy_visible(principal))
    if status:
        query = query.where(Policy.status == status)
    if search and search.strip():
        pattern = _contains(search.strip())
        file_matches = (
            select(PolicyVersion.id)
            .join(Document, Document.id == PolicyVersion.document_id)
            .where(
                PolicyVersion.policy_id == Policy.id,
                or_(Document.original_filename.ilike(pattern, escape="\\"), Document.title.ilike(pattern, escape="\\")),
            )
            .exists()
        )
        query = query.where(or_(
            Policy.name.ilike(pattern, escape="\\"), Policy.policy_number.ilike(pattern, escape="\\"), file_matches,
        ))
    if version_state:
        query = query.where(
            select(PolicyVersion.id).where(PolicyVersion.policy_id == Policy.id, _state_clause(version_state, as_of)).exists()
        )
    total = session.scalar(select(func.count()).select_from(query.subquery())) or 0

    latest_upload = (
        select(func.max(Document.created_at))
        .join(PolicyVersion, PolicyVersion.document_id == Document.id)
        .where(PolicyVersion.policy_id == Policy.id)
        .scalar_subquery()
    )
    highest_version = select(func.max(PolicyVersion.version_number)).where(PolicyVersion.policy_id == Policy.id).scalar_subquery()
    order = {
        "custom": arranged_policy_order(),
        "name": (func.lower(Policy.name), Policy.id),
        "uploaded": (latest_upload.desc().nulls_last(), Policy.id),
        "version": (highest_version.desc().nulls_last(), func.lower(Policy.name), Policy.id),
    }[sort]
    policies = session.scalars(query.order_by(*order).limit(limit).offset(offset)).all()
    if not policies:
        return [], total
    by_policy = versions_with_files(session, [p.id for p in policies], as_of)
    return _policy_items(policies, by_policy, sort, version_state), total


def versions_with_files(session: Session, policy_ids: list[uuid.UUID], as_of: date) -> dict[uuid.UUID, list[CategoryVersion]]:
    """Every version of these policies (newest first) with the file behind it and who uploaded it."""
    rows = session.execute(
        select(PolicyVersion, Document, User.full_name)
        .join(Document, Document.id == PolicyVersion.document_id)
        .outerjoin(User, User.id == Document.uploaded_by_id)
        .where(PolicyVersion.policy_id.in_(policy_ids))
        .order_by(PolicyVersion.policy_id, PolicyVersion.effective_from.desc(), PolicyVersion.version_number.desc())
    ).all()
    by_policy: dict[uuid.UUID, list[CategoryVersion]] = {}
    for version, document, uploader in rows:
        state = timeline_state(version, as_of)
        read = CategoryVersion(
            **VersionRead.model_validate(version).model_dump(exclude={"timeline_state"}),
            timeline_state=state, lifecycle=lifecycle(version.status, state), ai_change_summary=version.ai_change_summary,
            filename=document.original_filename, title=document.title, content_type=document.content_type,
            size_bytes=document.size_bytes, page_count=document.page_count, document_status=document.status,
            uploaded_at=document.created_at, uploaded_by_id=document.uploaded_by_id, uploaded_by_name=uploader,
        )
        by_policy.setdefault(version.policy_id, []).append(read)
    return by_policy


def _policy_items(policies, by_policy, sort: str, version_state: str | None) -> list[CategoryDocument]:
    items = []
    for policy in policies:
        versions = by_policy.get(policy.id, [])
        if sort == "uploaded":
            versions.sort(key=lambda v: v.uploaded_at, reverse=True)
        elif sort == "version":
            versions.sort(key=lambda v: v.version_number, reverse=True)
        items.append(CategoryDocument(
            id=policy.id, name=policy.name, policy_number=policy.policy_number, description=policy.description,
            status=policy.status, branch_id=policy.branch_id, department_id=policy.department_id,
            display_order=policy.display_order, created_at=policy.created_at, updated_at=policy.updated_at,
            version_count=sum(1 for v in versions if v.status == VersionStatus.ACTIVE),
            current_version_id=next((v.id for v in versions if v.timeline_state == "current"), None),
            latest_upload_at=max((v.uploaded_at for v in versions), default=None),
            # With a version filter, show only the versions that match it.
            versions=[v for v in versions if version_state is None or v.timeline_state == version_state],
        ))
    return items


def pending_uploads(session: Session, principal: Principal, category: Category) -> list[PendingUpload]:
    """Files uploaded into the category and not yet a version: processing, waiting for review, or drafts.

    A file planned by an upload names the document it is meant for (its group's policy), so a draft
    shows under that document until it is published.
    """
    rows = session.execute(
        select(Document, User.full_name, UploadBatchItem, UploadBatch.publish, Policy.id, Policy.name)
        .outerjoin(User, User.id == Document.uploaded_by_id)
        .outerjoin(UploadBatchItem, UploadBatchItem.document_id == Document.id)
        .outerjoin(UploadBatchGroup, UploadBatchGroup.id == UploadBatchItem.group_id)
        .outerjoin(UploadBatch, UploadBatch.id == UploadBatchItem.batch_id)
        .outerjoin(Policy, Policy.id == UploadBatchGroup.policy_id)
        .where(
            Document.category_id == category.id,
            Document.policy_id.is_(None),
            Document.status.not_in(PENDING_EXCLUDED),
            visible_clause(principal, Document.organization_id, Document.branch_id, Document.department_id, Document.policy_id),
        )
        .order_by(Document.created_at.desc())
        .limit(MAX_PENDING)
    ).all()
    return [
        PendingUpload(
            document_id=document.id, filename=document.original_filename, title=document.title,
            status=document.status, size_bytes=document.size_bytes, uploaded_at=document.created_at,
            uploaded_by_name=uploader, error=(document.error or {}).get("message"),
            batch_id=item.batch_id if item else None, target_policy_id=policy_id, target_policy_name=policy_name,
            version_label=item.version_label if item else None,
            draft=bool(item is not None and publish is False and item.status == ItemStatus.PROCESSING),
        )
        for document, uploader, item, publish, policy_id, policy_name in rows
    ]
