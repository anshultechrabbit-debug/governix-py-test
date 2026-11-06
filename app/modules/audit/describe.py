"""Audit events in plain words, for the audit log screen.

Each event gets the names behind its ids (who did it, the organization, what it was done to), a label, a
category, a severity, one sentence saying what happened, and its details as readable facts. Names are looked
up in batches, deleted rows included, so history keeps its names; when a row is gone, the event's own details
name it.
"""

import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.modules.audit.model import AuditEvent
from app.modules.audit.schema import AuditEventRead, AuditFact
from app.modules.branches.model import Branch
from app.modules.categories.model import Category
from app.modules.departments.model import Department
from app.modules.documents.model import Document
from app.modules.organizations.model import Organization
from app.modules.policies.model import Policy, PolicyVersion
from app.modules.tickets.model import SupportCategory, Ticket
from app.modules.users.model import User

ROLE_LABELS = {
    "master_admin": "Master Admin",
    "org_admin": "Organization Admin",
    "branch_manager": "Branch Manager",
    "department_user": "User",
    "support_agent": "Support Agent",
}

# What each kind of resource is called, and the column that names it.
_NAMED = {
    "organization": (Organization, Organization.name),
    "branch": (Branch, Branch.name),
    "department": (Department, Department.name),
    "category": (Category, Category.name),
    "policy": (Policy, Policy.name),
    "document": (Document, func.coalesce(Document.title, Document.original_filename)),
    "ticket": (Ticket, Ticket.subject),
    "support_category": (SupportCategory, SupportCategory.name),
}
# Details keys that hold the id of a policy or a user the sentence names.
_POLICY_KEYS = ("policy_id", "from_policy_id", "to_policy_id", "target_policy_id", "matched_policy_id")
_USER_KEYS = ("user_id", "assigned_to")

# action: (label, category). Unknown actions get a label made from their name.
ACTIONS: dict[str, tuple[str, str]] = {
    "auth.login": ("Signed in", "Sign-in & security"),
    "auth.login_failed": ("Failed sign-in", "Sign-in & security"),
    "auth.refresh_token_reuse": ("Suspicious session reuse", "Sign-in & security"),
    "auth.password_changed": ("Password changed", "Sign-in & security"),
    "auth.password_reset_requested": ("Password reset requested", "Sign-in & security"),
    "auth.password_reset_completed": ("Password reset completed", "Sign-in & security"),
    "auth.mfa_enabled": ("Two-step sign-in turned on", "Sign-in & security"),
    "auth.mfa_disabled": ("Two-step sign-in turned off", "Sign-in & security"),
    "auth.mfa_recovery_used": ("Backup code used", "Sign-in & security"),
    "auth.mfa_recovery_codes_renewed": ("New backup codes", "Sign-in & security"),
    "auth.step_up_failed": ("Wrong code for an important action", "Sign-in & security"),
    "user.mfa_reset": ("Two-step sign-in reset by admin", "Users"),
    "user.created": ("User added", "Users"),
    "user.updated": ("User updated", "Users"),
    "user.deleted": ("User deleted", "Users"),
    "user.password_reset": ("Password reset by admin", "Users"),
    "user.master_admin_bootstrapped": ("Master Admin created", "Users"),
    "organization.created": ("Organization created", "Organizations"),
    "organization.updated": ("Organization updated", "Organizations"),
    "organization.deleted": ("Organization deleted", "Organizations"),
    "organization.logo_updated": ("Logo changed", "Organizations"),
    "organization.nginx_deployed": ("Portal published", "Organizations"),
    "organization.nginx_disabled": ("Portal taken offline", "Organizations"),
    "branch.created": ("Branch created", "Branches & departments"),
    "branch.updated": ("Branch updated", "Branches & departments"),
    "branch.deleted": ("Branch deleted", "Branches & departments"),
    "department.created": ("Department created", "Branches & departments"),
    "department.updated": ("Department updated", "Branches & departments"),
    "department.deleted": ("Department deleted", "Branches & departments"),
    "category.created": ("Category created", "Policies"),
    "category.updated": ("Category updated", "Policies"),
    "policy.created": ("Policy created", "Policies"),
    "policy.updated": ("Policy updated", "Policies"),
    "policy.deleted": ("Policy deleted", "Policies"),
    "policy.moved": ("Policy moved", "Policies"),
    "policy.assigned": ("Access given", "Policies"),
    "policy.unassigned": ("Access removed", "Policies"),
    "policy.versions_reordered": ("Versions reordered", "Policies"),
    "policy.latest_version_changed": ("Current version changed", "Policies"),
    "policy_version.created": ("Version added", "Policies"),
    "policy_version.withdrawn": ("Version withdrawn", "Policies"),
    "policy_version.restored": ("Version restored", "Policies"),
    "relationship.created": ("Documents linked", "Policies"),
    "upload_batch.created": ("Bulk upload started", "Documents"),
    "document.uploaded": ("Document uploaded", "Documents"),
    "document.duplicate_override": ("Duplicate uploaded anyway", "Documents"),
    "document.analyzed": ("Document read", "Documents"),
    "document.confirmed": ("Document confirmed", "Documents"),
    "document.rejected": ("Document rejected", "Documents"),
    "document.ready": ("Document ready", "Documents"),
    "document.updated": ("Document updated", "Documents"),
    "document.moved": ("Document moved", "Documents"),
    "document.archived": ("Document archived", "Documents"),
    "document.deleted": ("Document deleted", "Documents"),
    "document.retry": ("Processing retried", "Documents"),
    "document.viewed": ("Document page viewed", "Documents"),
    "document.accessed": ("Document opened", "Documents"),
    "ai.query": ("Question asked", "Assistant"),
    "search.query": ("Search", "Assistant"),
    "ticket.created": ("Ticket raised", "Support"),
    "ticket.routed": ("Ticket routing confirmed", "Support"),
    "ticket.status_changed": ("Ticket status changed", "Support"),
    "ticket.priority_changed": ("Ticket priority changed", "Support"),
    "ticket.assigned": ("Ticket assigned", "Support"),
    "ticket.forwarded": ("Ticket forwarded", "Support"),
    "ticket.escalated": ("Ticket escalated", "Support"),
    "ticket.reopened": ("Ticket reopened", "Support"),
    "support_staff.added": ("Support staff added", "Support"),
    "support_staff.updated": ("Support staff updated", "Support"),
    "support_staff.removed": ("Support staff removed", "Support"),
    "support_category.created": ("Ticket category added", "Support"),
    "support_category.updated": ("Ticket category updated", "Support"),
}
_CRITICAL = frozenset({"auth.refresh_token_reuse"})
_WARNING = frozenset({
    "auth.login_failed", "user.deleted", "organization.deleted", "branch.deleted", "department.deleted",
    "organization.nginx_disabled", "policy.deleted", "policy_version.withdrawn", "document.deleted",
    "document.rejected", "document.duplicate_override", "user.password_reset",
    "auth.mfa_disabled", "auth.mfa_recovery_used", "user.mfa_reset", "auth.step_up_failed",
})

# Why the assistant gave no answer, as the reader would put it.
_NO_ANSWER = {
    "KEY_TERMS_NOT_FOUND": "the documents don't cover it",
    "LOW_RELEVANCE": "the documents don't cover it",
    "NO_RELEVANT_DOCUMENTS": "the documents don't cover it",
    "INSUFFICIENT_EVIDENCE": "the documents don't state it",
    "ANSWER_FAILED_VALIDATION": "nothing could be quoted reliably",
    "ANSWER_OFF_TOPIC": "the closest passages were about something else",
    "CLAIM_NOT_CONFIRMED": "the documents don't confirm the claim",
    "NOT_ASSIGNED": "the policy is not assigned to them",
    "NO_ASSIGNED_POLICIES": "no policies are assigned to them",
    "INSTRUCTIONS_IGNORED": "it only contained instructions to the assistant",
}

_LABELS = {
    "is_active": "Active", "allowed_port": "Port", "size_bytes": "Size", "locked": "Account locked",
    "full_name": "Name", "postal_code": "Postal code", "street_address": "Street address",
    "policy_number": "Policy number", "version_label": "Version", "effective_from": "Effective from",
    "suggested_decision": "Suggested decision", "suggested_name": "Suggested name", "from_stage": "From stage",
    "previous_error": "Previous error", "automatic": "Automatic", "no_answer_reason": "Reason",
    "latency_ms": "Time taken (ms)", "cache_hit": "From cache", "old_label": "Old version", "new_label": "New version",
    "from_policy": "From policy", "to_policy": "To policy", "old_version": "Previous version",
    "new_version": "New version", "released_port": "Port released", "chunks": "Passages", "status": "Status",
}
# Details that are internal (ids, hashes, retrieval traces) or shown by the sentence itself.
_HIDDEN = frozenset({
    "sha256", "permission_scope", "plan", "retrieved_chunk_ids", "cited_chunk_ids", "citations", "usage",
    "file", "archived_policy_ids", "manual", "order_requested",
})
MAX_TEXT = 140


@dataclass
class _Names:
    users: dict[uuid.UUID, tuple[str, str, str]] = field(default_factory=dict)  # name, email, role
    things: dict[tuple[str, uuid.UUID], str] = field(default_factory=dict)  # (type, id) -> name
    versions: dict[uuid.UUID, tuple[str, uuid.UUID]] = field(default_factory=dict)  # label, policy id


def _uuid(value: Any) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value)) if value else None
    except ValueError:
        return None


def _load(session: Session, events: list[AuditEvent]) -> _Names:
    wanted: dict[str, set[uuid.UUID]] = {kind: set() for kind in _NAMED}
    users: set[uuid.UUID] = set()
    versions: set[uuid.UUID] = set()
    for event in events:
        details = event.details or {}
        if event.organization_id:
            wanted["organization"].add(event.organization_id)
        if event.actor_user_id:
            users.add(event.actor_user_id)
        if event.resource_id:
            if event.resource_type == "user":
                users.add(event.resource_id)
            elif event.resource_type == "policy_version":
                versions.add(event.resource_id)
            elif event.resource_type in wanted:
                wanted[event.resource_type].add(event.resource_id)
        wanted["policy"] |= {i for key in _POLICY_KEYS if (i := _uuid(details.get(key)))}
        users |= {i for key in _USER_KEYS if (i := _uuid(details.get(key)))}

    names = _Names()
    if versions:
        rows = session.execute(select(PolicyVersion.id, PolicyVersion.version_label, PolicyVersion.policy_id)
                               .where(PolicyVersion.id.in_(versions)))
        for version_id, label, policy_id in rows:
            names.versions[version_id] = (label, policy_id)
            wanted["policy"].add(policy_id)
    if users:
        rows = session.execute(select(User.id, User.full_name, User.email, User.role).where(User.id.in_(users)))
        names.users = {user_id: (name, email, role) for user_id, name, email, role in rows}
    for kind, ids in wanted.items():
        if ids:
            model, column = _NAMED[kind]
            for row_id, name in session.execute(select(model.id, column).where(model.id.in_(ids))):
                names.things[(kind, row_id)] = name
    return names


def _quote(text: Any) -> str:
    text = " ".join(str(text or "").split())
    return f"“{text[:MAX_TEXT - 1]}…”" if len(text) > MAX_TEXT else f"“{text}”"


def _role(value: Any) -> str:
    return ROLE_LABELS.get(str(value), str(value or "").replace("_", " ").title())


def _label(key: str) -> str:
    return _LABELS.get(key, key.replace("_", " ").capitalize())


def _value(key: str, value: Any) -> str:
    if key == "role":
        return _role(value)
    if key == "size_bytes" and isinstance(value, int | float):
        return f"{value / 1024 / 1024:.1f} MB" if value >= 1024 * 1024 else f"{value / 1024:.0f} KB"
    if isinstance(value, bool) or str(value) in ("True", "False"):
        return "Yes" if str(value) == "True" else "No"
    if isinstance(value, list):
        return ", ".join(str(v) for v in value[:10]) + (f" and {len(value) - 10} more" if len(value) > 10 else "")
    if isinstance(value, dict):
        return ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in value.items() if v) or "none"
    if value is None or value == "":
        return "—"
    return str(value)


def _changes(details: dict) -> str:
    """ "status to suspended and name to Acme Bank", from {"changes": {...}}."""
    changes = details.get("changes") or {}
    parts = [f"{_label(key).lower()} to {_value(key, value)}" for key, value in changes.items()]
    if not parts:
        return ""
    return parts[0] if len(parts) == 1 else f"{', '.join(parts[:-1])} and {parts[-1]}"


@dataclass
class _Context:
    event: AuditEvent
    names: _Names
    actor: str
    target: str

    @property
    def d(self) -> dict:
        return self.event.details or {}

    def policy(self, key: str = "policy_id", fallback: str = "policy") -> str:
        policy_id = _uuid(self.d.get(key))
        name = self.names.things.get(("policy", policy_id)) if policy_id else None
        return _quote(name or self.d.get(fallback) or "a policy")

    def user(self, key: str) -> str:
        found = self.names.users.get(_uuid(self.d.get(key)))
        return found[0] if found else str(self.d.get("user") or "a user")

    def edited(self, what: str) -> str:
        changed = _changes(self.d)
        return f"{self.actor} updated {what} {self.target}" + (f": changed {changed}." if changed else ".")


def _user_updated(c: _Context) -> str:
    changes = c.d.get("changes") or {}
    if "is_active" in changes and len(changes) == 1:
        verb = "reactivated" if str(changes["is_active"]) == "True" else "deactivated"
        return f"{c.actor} {verb} {c.target}." + ("" if verb == "reactivated" else " They can no longer sign in.")
    return c.edited("the user")


def _organization_updated(c: _Context) -> str:
    status = (c.d.get("changes") or {}).get("status")
    if status == "suspended" and len(c.d["changes"]) == 1:
        return f"{c.actor} suspended {c.target}. Its users can no longer sign in."
    if status == "active" and len(c.d["changes"]) == 1:
        return f"{c.actor} reactivated {c.target}. Its users can sign in again."
    return c.edited("the organization")


def _deleted(kind: str) -> Callable[[_Context], str]:
    def sentence(c: _Context) -> str:
        counts = {k: v for k, v in (c.d.get("counts") or {}).items() if v}
        below = f" Also removed: {_value('counts', counts)}." if counts else ""
        return f"{c.actor} deleted the {kind} {c.target}.{below}"
    return sentence


def _ai_query(c: _Context) -> str:
    asked = f"{c.actor} asked the assistant {_quote(c.d.get('question'))}"
    if c.d.get("status") == "answered":
        return f"{asked}. It was answered."
    reason = c.d.get("no_answer_reason")
    return f"{asked}. No answer was given" + (f": {_NO_ANSWER.get(reason, str(reason).lower().replace('_', ' '))}."
                                               if reason else ".")


_STEP_UP_ACTION = {
    "user.create": "adding a user", "user.update": "changing a user's access", "user.delete": "deleting a user",
    "user.password_reset": "resetting a password", "user.mfa_reset": "resetting two-step sign-in",
    "organization.status": "suspending or reactivating an organization", "organization.delete": "deleting an organization",
    "branch.delete": "deleting a branch", "department.delete": "deleting a department",
    "organization.nginx_deployed": "publishing a portal", "organization.nginx_disabled": "taking a portal offline",
    "policy_version.withdrawn": "withdrawing a policy version", "policy_version.restored": "restoring a policy version",
    "policy.deleted": "deleting a policy", "document.deleted": "deleting a document",
    "support_staff.added": "adding support staff",
}


def _step_up_failed(c: _Context) -> str:
    action = _STEP_UP_ACTION.get(c.d.get("action"), str(c.d.get("action") or "an important action"))
    how = "an authenticator code that was already used" if c.d.get("reused") else "a wrong authenticator code"
    return f"{c.actor} tried {action} with {how}. Nothing was changed."


SENTENCES: dict[str, Callable[[_Context], str]] = {
    "auth.login": lambda c: f"{c.actor} signed in"
    + {"app": " with their authenticator app.", "recovery": " with a backup code."}.get(c.d.get("two_step"), "."),
    "auth.login_failed": lambda c: f"Someone tried to sign in as {c.target} with a wrong "
    + ("authenticator code (the password was right)." if c.d.get("step") else "password.")
    + (" The account is now locked for a while." if c.d.get("locked") else ""),
    "auth.mfa_enabled": lambda c: f"{c.actor} turned on two-step sign-in with an authenticator app.",
    "auth.mfa_disabled": lambda c: f"{c.actor} turned off two-step sign-in.",
    "auth.mfa_recovery_used": lambda c: f"A backup code of {c.target} was used to sign in. "
    f"{c.d.get('left', 'Some')} backup codes are left.",
    "auth.mfa_recovery_codes_renewed": lambda c: f"{c.actor} replaced their backup codes. The old ones no longer work.",
    "auth.step_up_failed": lambda c: _step_up_failed(c),
    "user.mfa_reset": lambda c: f"{c.actor} reset the two-step sign-in of {c.target}. They were signed out everywhere "
    "and can set it up again.",
    "auth.refresh_token_reuse": lambda c: f"An old session token of {c.target} was used again, which can mean it was "
    "stolen. All of their sessions were signed out as a precaution.",
    "auth.password_changed": lambda c: f"{c.actor} changed their password.",
    "auth.password_reset_requested": lambda c: f"A password reset link was emailed to {c.target}.",
    "auth.password_reset_completed": lambda c: f"{c.actor} set a new password using a reset link. "
    "All of their sessions were signed out.",
    "user.created": lambda c: f"{c.actor} added {c.target} as {_role(c.d.get('role'))}.",
    "user.updated": _user_updated,
    "user.deleted": lambda c: f"{c.actor} deleted the {_role(c.d.get('role'))} {c.target}.",
    "user.password_reset": lambda c: f"{c.actor} reset the password of {c.target}. They were signed out everywhere.",
    "user.master_admin_bootstrapped": lambda c: f"The Master Admin account {c.target} was created during setup.",
    "organization.created": lambda c: f"{c.actor} created the organization {c.target}.",
    "organization.updated": _organization_updated,
    "organization.deleted": _deleted("organization"),
    "organization.logo_updated": lambda c: f"{c.actor} changed the logo of {c.target}.",
    "organization.nginx_deployed": lambda c: f"{c.actor} published the portal of {c.target} on port {c.d.get('port')}.",
    "organization.nginx_disabled": lambda c: f"{c.actor} took the portal of {c.target} offline "
    f"(port {c.d.get('port')}).",
    "branch.created": lambda c: f"{c.actor} created the branch {c.target}"
    + (f" (code {c.d['code']})." if c.d.get("code") else "."),
    "branch.updated": lambda c: c.edited("the branch"),
    "branch.deleted": _deleted("branch"),
    "department.created": lambda c: f"{c.actor} created the department {c.target}.",
    "department.updated": lambda c: c.edited("the department"),
    "department.deleted": _deleted("department"),
    "category.created": lambda c: f"{c.actor} created the policy category {c.target}.",
    "category.updated": lambda c: c.edited("the category"),
    "policy.created": lambda c: f"{c.actor} created the policy {c.target}"
    + (f" in {_quote(c.d['category'])}." if c.d.get("category") else "."),
    "policy.updated": lambda c: c.edited("the policy"),
    "policy.deleted": lambda c: f"{c.actor} deleted the policy {c.target}"
    + (f" with {len(c.d['versions'])} version(s)." if c.d.get("versions") else "."),
    "policy.moved": lambda c: f"{c.actor} moved the policy {c.target} to another position.",
    "policy.assigned": lambda c: f"{c.actor} gave {c.user('user_id')} access to {c.target}.",
    "policy.unassigned": lambda c: f"{c.actor} removed {c.user('user_id')}'s access to {c.target}.",
    "policy.versions_reordered": lambda c: f"{c.actor} changed the order of the versions of {c.target}.",
    "policy.latest_version_changed": lambda c: f"The current version of {c.target} changed from "
    f"{c.d.get('old_version') or 'none'} to {c.d.get('new_version') or 'none'}.",
    "policy_version.created": lambda c: f"{c.actor} added version {c.d.get('label') or c.target} to {c.policy()}.",
    "policy_version.withdrawn": lambda c: f"{c.actor} withdrew version {c.target} of {c.policy()}"
    + (f": {_quote(c.d['reason'])}." if c.d.get("reason") else "."),
    "policy_version.restored": lambda c: f"{c.actor} restored version {c.d.get('label') or c.target} of {c.policy()}.",
    "relationship.created": lambda c: f"{c.actor} linked a document to {c.policy('target_policy_id')}"
    + (f" ({str(c.d['type']).replace('_', ' ')})." if c.d.get("type") else "."),
    "upload_batch.created": lambda c: f"{c.actor} started a bulk upload of {c.d.get('files')} file(s) "
    f"in {c.d.get('groups')} group(s).",
    "document.uploaded": lambda c: f"{c.actor} uploaded {_quote(c.d.get('filename')) if c.d.get('filename') else c.target}.",
    "document.duplicate_override": lambda c: f"{c.actor} uploaded {c.target} although the same file already exists"
    + (f": {_quote(c.d['reason'])}." if c.d.get("reason") else "."),
    "document.analyzed": lambda c: f"Governix finished reading {c.target}"
    + (f" and suggests {_quote(c.d['suggested_name'])}." if c.d.get("suggested_name") else "."),
    "document.confirmed": lambda c: f"{c.actor} confirmed {c.target} as version {c.d.get('version_label')} "
    f"of {c.policy()}.",
    "document.rejected": lambda c: f"{c.actor} rejected {c.target}"
    + (f": {_quote(c.d['reason'])}." if c.d.get("reason") else "."),
    "document.ready": lambda c: f"{c.target} is ready to search and ask about"
    + (f" ({c.d['chunks']} passages)." if c.d.get("chunks") else "."),
    "document.updated": lambda c: c.edited("the document"),
    "document.moved": lambda c: f"{c.actor} moved {c.target} from {c.policy('from_policy_id', 'from_policy')} "
    f"to {c.policy('to_policy_id', 'to_policy')}.",
    "document.archived": lambda c: f"{c.actor} archived {c.target}.",
    "document.deleted": lambda c: f"{c.actor} deleted {c.target}"
    + (f" (version {c.d['version']} of {_quote(c.d['policy'])})." if c.d.get("version") and c.d.get("policy") else "."),
    "document.retry": lambda c: ("Governix automatically retried" if c.d.get("automatic") else f"{c.actor} retried")
    + f" processing {c.target}" + (f" from the {c.d['from_stage']} step." if c.d.get("from_stage") else "."),
    "document.viewed": lambda c: f"{c.actor} viewed page {c.d.get('page')} of {c.target}.",
    "document.accessed": lambda c: f"{c.actor} opened {c.target}.",
    "ai.query": _ai_query,
    "search.query": lambda c: f"{c.actor} searched for {_quote(c.d.get('query'))} and got {c.d.get('results', 0)} "
    "result(s).",
    "ticket.created": lambda c: f"{c.actor} raised the ticket {c.target}"
    + (f" ({c.d['reference']})." if c.d.get("reference") else "."),
    "ticket.routed": lambda c: f"{c.actor} sent the ticket {c.target} to {(c.d.get('to') or {}).get('queue', 'support')}"
    + (" (switched from the recommendation)." if c.d.get("switched") else "."),
    "ticket.forwarded": lambda c: f"{c.actor} forwarded the ticket {c.target} from "
    f"{(c.d.get('from') or {}).get('queue', '?')} to {(c.d.get('to') or {}).get('queue', '?')}.",
    "ticket.escalated": lambda c: f"{c.actor} escalated the ticket {c.target} to {(c.d.get('to') or {}).get('queue', '?')}.",
    "ticket.reopened": lambda c: f"{c.actor} reopened the ticket {c.target}.",
    "ticket.priority_changed": lambda c: f"{c.actor} changed the priority of {c.target} from {c.d.get('from')} "
    f"to {c.d.get('to')}.",
    "support_staff.added": lambda c: f"{c.actor} put {c.user('user_id')} on {c.d.get('level', '')} support duty.",
    "support_staff.removed": lambda c: f"{c.actor} took {c.user('user_id')} off support duty.",
    "ticket.status_changed": lambda c: f"{c.actor} moved the ticket {c.target} from {c.d.get('from')} "
    f"to {c.d.get('to')}.",
    "ticket.assigned": lambda c: f"{c.actor} assigned the ticket {c.target} to {c.user('assigned_to')}"
    + (" (automatically, by workload)." if c.d.get("auto") else ".")
    if c.d.get("assigned_to") else f"{c.actor} unassigned the ticket {c.target}.",
}


def _actor(event: AuditEvent, names: _Names) -> tuple[str, str | None, str | None, str | None]:
    """(how the sentence names the actor, name, email, role label)."""
    if event.actor_user_id and (found := names.users.get(event.actor_user_id)):
        name, email, role = found
        return name or email, name, email, _role(role)
    if event.actor_user_id:
        return "A deleted user", None, None, None
    return ("Governix" if event.action.startswith(("document.", "policy.")) else "Someone"), None, None, None


def _target(event: AuditEvent, names: _Names) -> str | None:
    """The name of what the event was done to, or None when nothing names it."""
    details = event.details or {}
    if event.resource_type == "user":
        found = names.users.get(event.resource_id) if event.resource_id else None
        if found:
            name, email, _role_name = found
            return f"{name} ({email})" if name and name != email else email
        return details.get("email")
    if event.resource_type == "policy_version" and event.resource_id in names.versions:
        return names.versions[event.resource_id][0]
    name = names.things.get((event.resource_type or "", event.resource_id)) if event.resource_id else None
    return name or details.get("name") or details.get("title") or details.get("filename") or details.get("subject")


def _facts(event: AuditEvent) -> list[AuditFact]:
    details = dict(event.details or {})
    if event.action == "ai.query":
        keep = ("question", "status", "no_answer_reason", "model", "latency_ms", "warnings")
        details = {k: details.get(k) for k in keep if details.get(k) not in (None, [], "")}
        if (reason := details.get("no_answer_reason")) in _NO_ANSWER:
            details["no_answer_reason"] = _NO_ANSWER[reason]
        if "status" in details:
            details["status"] = "Answered" if details["status"] == "answered" else "Not answered"
    facts = []
    for key, value in details.items():
        if key in _HIDDEN or key.endswith("_id") or key in ("duplicate_of", "supersedes"):
            continue
        if key == "changes" and isinstance(value, dict):
            facts += [AuditFact(label=f"{_label(k)} (now)", value=_value(k, v)) for k, v in value.items()]
            continue
        facts.append(AuditFact(label=_label(key), value=_value(key, value)))
    return facts


def describe(session: Session, events: Iterable[AuditEvent]) -> list[AuditEventRead]:
    events = list(events)
    names = _load(session, events)
    described = []
    for event in events:
        actor, actor_name, actor_email, actor_role = _actor(event, names)
        target = _target(event, names)
        label, category = ACTIONS.get(event.action, (event.action.replace(".", " ").replace("_", " ").capitalize(),
                                                     (event.resource_type or "Other").replace("_", " ").title()))
        kind = (event.resource_type or "record").replace("_", " ")
        unnamed = f"{'an' if kind[0] in 'aeio' else 'a'} {kind}"  # "a user", "an organization"
        context = _Context(event, names, actor, _quote(target) if target and event.resource_type != "user"
                           else target or unnamed)
        try:
            summary = SENTENCES[event.action](context) if event.action in SENTENCES else (
                f"{actor}: {label.lower()}" + (f" — {context.target}." if target else "."))
        except (KeyError, TypeError, ValueError):  # an older event with other details: still say what it was
            summary = f"{actor}: {label.lower()}."
        summary = summary[:1].upper() + summary[1:]
        severity = "critical" if event.action in _CRITICAL else "warning" if event.action in _WARNING else "info"
        if event.action == "auth.login_failed" and (event.details or {}).get("locked"):
            severity = "critical"
        organization = names.things.get(("organization", event.organization_id)) if event.organization_id else None
        read = AuditEventRead.model_validate(event)
        described.append(read.model_copy(update={
            "actor_name": actor_name, "actor_email": actor_email, "actor_role": actor_role,
            "organization_name": organization, "resource_name": target,
            "action_label": label, "category": category, "severity": severity,
            "summary": summary, "facts": _facts(event),
        }))
    return described
