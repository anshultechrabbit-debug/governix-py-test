"""Deleting an organization, branch or department, and everything below it.

A delete is soft: rows are stamped (deleted_at, deleted_by_id) and deactivated, never removed, so
the audit trail keeps every name. The caller first gets the hierarchy the delete reaches (branches,
departments, users and policies) and confirms it; without confirm=true nothing changes.

Below the deleted record, users and the levels under it are deleted with it, every user's sessions
end, and its active policies are archived (status, as Policies > Archive does), which takes them out
of search and the AI Assistant.
"""

import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import ColumnElement, func, select, update
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, ValidationError
from app.modules.audit.service import record_event
from app.modules.auth.model import RefreshToken
from app.modules.auth.permissions import Principal
from app.modules.branches.model import Branch
from app.modules.departments.model import Department
from app.modules.organizations.model import Organization
from app.modules.organizations.repository import bump_knowledge_version
from app.modules.organizations.schema import (
    DeletableType,
    DeletionCounts,
    DeletionPreview,
    DeletionResult,
    HierarchyNode,
)
from app.modules.policies.model import Policy, PolicyStatus
from app.modules.users.model import User

CONFIRMATION_REQUIRED = "DELETE_CONFIRMATION_REQUIRED"
PLURALS = {"branch": "branches", "department": "departments", "user": "users", "policy": "policies"}
# What lies below each kind, in the order the messages name it.
BELOW: dict[DeletableType, tuple[str, ...]] = {
    "organization": ("branch", "department", "user"), "branch": ("department", "user"), "department": ("user",),
}


@dataclass(frozen=True)
class Target:
    """The record being deleted, as the scope it covers."""

    record: Organization | Branch | Department

    @property
    def type(self) -> DeletableType:
        if isinstance(self.record, Organization):
            return "organization"
        return "branch" if isinstance(self.record, Branch) else "department"

    @property
    def organization_id(self) -> uuid.UUID:
        return self.record.id if isinstance(self.record, Organization) else self.record.organization_id

    @property
    def branch_id(self) -> uuid.UUID | None:
        if isinstance(self.record, Branch):
            return self.record.id
        return self.record.branch_id if isinstance(self.record, Department) else None

    @property
    def department_id(self) -> uuid.UUID | None:
        return self.record.id if isinstance(self.record, Department) else None

    def covers(self, model) -> ColumnElement[bool]:
        """Rows of `model` (with organization/branch/department columns) inside this scope."""
        clause = model.organization_id == self.organization_id
        if self.branch_id is not None:
            clause &= (model.id if model is Branch else model.branch_id) == self.branch_id
        if self.department_id is not None:
            clause &= (model.id if model is Department else model.department_id) == self.department_id
        return clause


def _live(model) -> ColumnElement[bool]:
    return model.deleted_at.is_(None)


def preview(session: Session, target: Target) -> DeletionPreview:
    """The hierarchy a delete of `target` reaches, and the message the dialog shows."""
    branches = [] if target.department_id else list(session.scalars(
        select(Branch).where(target.covers(Branch), _live(Branch)).order_by(Branch.name)
    ))
    departments = list(session.scalars(
        select(Department).where(target.covers(Department), _live(Department)).order_by(Department.name)
    ))
    people = Counter({
        (branch_id, department_id, role): count
        for branch_id, department_id, role, count in session.execute(
            select(User.branch_id, User.department_id, User.role, func.count())
            .where(target.covers(User), _live(User))
            .group_by(User.branch_id, User.department_id, User.role)
        )
    })
    policies = Counter({
        (branch_id, department_id): count
        for branch_id, department_id, count in session.execute(
            select(Policy.branch_id, Policy.department_id, func.count())
            .where(target.covers(Policy), Policy.status == PolicyStatus.ACTIVE)
            .group_by(Policy.branch_id, Policy.department_id)
        )
    })

    def users_at(branch_id, department_id) -> dict[str, int]:
        return {role: n for (b, d, role), n in people.items() if (b, d) == (branch_id, department_id)}

    def department_node(department: Department) -> HierarchyNode:
        return HierarchyNode(
            type="department", id=department.id, name=department.name, code=department.code,
            users=users_at(department.branch_id, department.id),
            policies=policies[(department.branch_id, department.id)],
        )

    def branch_node(branch: Branch) -> HierarchyNode:
        return HierarchyNode(
            type="branch", id=branch.id, name=branch.name, code=branch.code,
            users=users_at(branch.id, None), policies=policies[(branch.id, None)],
            children=[department_node(d) for d in departments if d.branch_id == branch.id],
        )

    record = target.record
    if isinstance(record, Organization):
        root = HierarchyNode(
            type="organization", id=record.id, name=record.name, code=record.slug,
            users=users_at(None, None), policies=policies[(None, None)],
            children=[branch_node(b) for b in branches],
        )
    elif isinstance(record, Branch):
        root = branch_node(record)
    else:
        root = department_node(record)

    counts = DeletionCounts(
        # The deleted record itself is not "below" it.
        branches=len(branches) - (target.type == "branch"),
        departments=len(departments) - (target.type == "department"),
        users=sum(people.values()),
        policies=sum(policies.values()),
    )
    return DeletionPreview(
        resource_type=target.type, id=record.id, name=record.name,
        message=_confirmation_message(target.type, record.name, counts), counts=counts, hierarchy=root,
    )


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun if count == 1 else PLURALS[noun]}"


def _listed(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def _below(counts: DeletionCounts, kind: DeletableType) -> list[str]:
    """What lies below `kind`, as words ("3 branches", "1 department", "12 users"), leaving out none."""
    return [
        _plural(getattr(counts, PLURALS[noun]), noun) for noun in BELOW[kind] if getattr(counts, PLURALS[noun])
    ]


def _confirmation_message(kind: DeletableType, name: str, counts: DeletionCounts) -> str:
    effects = []
    if below := _below(counts, kind):
        effects.append(f"delete its {_listed(below)}")
    if counts.policies:
        effects.append(f"archive {_plural(counts.policies, 'policy')}")
    if not effects:
        return f"Delete the {kind} “{name}”? Nothing else belongs to it."
    signed_out = " and these users can no longer sign in" if counts.users else ""
    return (
        f"Deleting the {kind} “{name}” will also {', and '.join(effects)}. Nothing is erased: the records"
        f" are kept, but they leave Governix{signed_out}. Choose “Delete all” to go ahead, or Cancel to keep"
        " everything."
    )


def delete(session: Session, principal: Principal, target: Target, *, confirm: bool) -> DeletionResult:
    """Soft-delete `target` and everything below it, once the caller has confirmed the preview.

    Without confirm the preview is returned as a 409 DELETE_CONFIRMATION_REQUIRED, so a client that
    skipped the dialog still sees what it would delete.
    """
    if (target.type, target.record.id) in {
        ("organization", principal.organization_id), ("branch", principal.branch_id),
        ("department", principal.department_id),
    }:
        raise ValidationError(f"You cannot delete the {target.type} you belong to.")
    shown = preview(session, target)
    if not confirm:
        raise ConflictError(shown.message, code=CONFIRMATION_REQUIRED, details=shown.model_dump(mode="json"))

    now = datetime.now(UTC)
    stamp = {"deleted_at": now, "deleted_by_id": principal.user_id}
    bulk = {"synchronize_session": False}
    users = select(User.id).where(target.covers(User), _live(User))
    session.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id.in_(users), RefreshToken.revoked_at.is_(None))
        .values(revoked_at=now),
        execution_options=bulk,
    )
    session.execute(
        update(User).where(target.covers(User), _live(User))
        .values(**stamp, is_active=False, token_version=User.token_version + 1),
        execution_options=bulk,
    )
    session.execute(
        update(Department).where(target.covers(Department), _live(Department)).values(**stamp, is_active=False),
        execution_options=bulk,
    )
    if target.department_id is None:
        session.execute(
            update(Branch).where(target.covers(Branch), _live(Branch)).values(**stamp, is_active=False),
            execution_options=bulk,
        )
    archived = list(session.scalars(
        update(Policy).where(target.covers(Policy), Policy.status == PolicyStatus.ACTIVE)
        .values(status=PolicyStatus.ARCHIVED).returning(Policy.id),
        execution_options=bulk,
    ))
    if archived:
        bump_knowledge_version(session, target.organization_id)

    record = target.record
    record.deleted_at, record.deleted_by_id = now, principal.user_id
    released_port = None
    if isinstance(record, Organization):
        # The port goes back to the pool; the deleted organization keeps no access to it.
        released_port, record.allowed_port = record.allowed_port, None
    else:
        record.is_active = False

    record_event(
        session, f"{target.type}.deleted", actor=principal, organization_id=target.organization_id,
        resource_type=target.type, resource_id=record.id,
        details={
            "name": record.name, "counts": shown.counts.model_dump(),
            "archived_policy_ids": [str(p) for p in archived], "released_port": released_port,
        },
    )
    session.commit()
    message = f"Deleted the {target.type} “{record.name}”"
    if below := _below(shown.counts, target.type):
        message += f" with its {_listed(below)}"
    if archived:
        message += f", and archived {_plural(len(archived), 'policy')}"
    return DeletionResult(resource_type=target.type, id=record.id, message=message + ".", counts=shown.counts)
