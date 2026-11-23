"""Switching an organization's AI on and off, and telling its admins when its availability changes.

Activation is the Master Admin's explicit decision, accepted only once everything required is in place
(status.readiness: can_activate). Suspension switches AI off at once: the next AI call of the organization,
including one already being prepared, is refused (service._check_switched_on). After any change of the
configuration, `after_change` re-arms usage alerts, tells the Org Admins (and, for a rejected key, the Master
Admins) when availability changed, and resumes the AI indexing documents were waiting for.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import select

from app.core.exceptions import ConflictError
from app.modules.ai_management.model import OrgAISettings
from app.modules.ai_management.schema import ReadinessRead
from app.modules.ai_management.status import AIStatus, Readiness, readiness
from app.modules.notifications.model import NotificationType
from app.modules.notifications.service import notify

logger = logging.getLogger(__name__)


def readiness_read(state: Readiness) -> ReadinessRead:
    return ReadinessRead(
        status=state.status, ready=state.ready, code=state.code, message=state.admin_message,
        activated=state.activated, can_activate=state.can_activate, missing=state.missing,
        problems=[message for _, message in state.problems], exhausted=state.exhausted, warnings=state.warnings,
        activated_at=state.activated_at, suspended_at=state.suspended_at, suspended_reason=state.suspended_reason,
    )


def _row(service, organization_id: uuid.UUID) -> OrgAISettings:
    row = service.db.scalar(select(OrgAISettings).where(OrgAISettings.organization_id == organization_id)
                            .with_for_update())
    if row is None:
        row = OrgAISettings(organization_id=organization_id, is_active=False)
        service.db.add(row)
    return row


def activate(service, organization_id: uuid.UUID, actor_id: uuid.UUID | None) -> OrgAISettings:
    """Switch AI on. Refused (409 AI_CONFIGURATION_INCOMPLETE / _INVALID) while anything required is missing."""
    service.organization(organization_id)
    state = readiness(service, organization_id, limits=False)
    if state.problems:
        raise ConflictError("AI can't be activated: " + " ".join(m for _, m in state.problems),
                            code="AI_CONFIGURATION_INVALID")
    # `can_activate` is decided before readiness() adds the "needs activation" pseudo-item to `missing`;
    # gating on `missing` here would always refuse, because that item is present until we activate.
    if not state.can_activate:
        missing = [m for m in state.missing if m != "activation by the platform administrator"]
        raise ConflictError("AI can't be activated yet. Still required: " + "; ".join(missing) + ".",
                            code="AI_CONFIGURATION_INCOMPLETE", details={"missing": missing})
    row = _row(service, organization_id)
    row.is_active, row.activated_at, row.activated_by_id = True, datetime.now(UTC), actor_id
    row.suspended_at = row.suspended_by_id = row.suspended_reason = None
    service.db.flush()
    return row


def suspend(service, organization_id: uuid.UUID, actor_id: uuid.UUID | None, reason: str | None) -> OrgAISettings:
    """Switch AI off on purpose (SUSPENDED). Everything else of the organization keeps working."""
    service.organization(organization_id)
    row = _row(service, organization_id)
    row.is_active = False
    row.suspended_at, row.suspended_by_id = datetime.now(UTC), actor_id
    row.suspended_reason = (reason or "").strip() or None
    service.db.flush()
    return row


def _available(status: str) -> bool:
    # A used-up limit is announced by the usage alerts, not as a change of availability.
    return status in (AIStatus.ACTIVE, AIStatus.LIMIT_REACHED)


def announce(service, organization_id: uuid.UUID, state: Readiness) -> bool:
    """Notify the organization's Org Admins once when its AI becomes available or unavailable (and the Master
    Admins when a key is rejected). Remembered on the settings row, so the same status is never announced twice."""
    from app.modules.auth.permissions import Role
    from app.modules.users.model import User

    row = service.db.scalar(select(OrgAISettings).where(OrgAISettings.organization_id == organization_id))
    if row is None and state.status == AIStatus.NOT_CONFIGURED:
        return False  # nothing has happened yet; the dashboard and the login popup already say so
    row = row or _row(service, organization_id)
    previous = row.notified_status
    current = AIStatus.ACTIVE if _available(state.status) else state.status
    if previous == current or (previous is None and not _available(current) and current != AIStatus.INVALID
                               and current != AIStatus.SUSPENDED):
        row.notified_status = current
        return False
    row.notified_status = current
    org = service.organization(organization_id)
    admins = service.db.scalars(select(User.id).where(User.role == Role.ORG_ADMIN, User.is_active,
                                                      User.organization_id == organization_id,
                                                      User.deleted_at.is_(None)))
    key = f"ai-status:{organization_id}:{current}:{datetime.now(UTC):%Y%m%d%H%M}"
    data = {"organization_id": str(organization_id), "status": str(state.status)}
    if current == AIStatus.ACTIVE:
        title, body = "AI is now available", (
            f"AI-powered features are active for {org.name}: questions, summaries and document analysis work again "
            "for all branches and users.")
    elif current == AIStatus.SUSPENDED:
        title, body = "AI has been suspended", (
            f"The platform administrator has suspended AI for {org.name}"
            + (f": {state.suspended_reason}" if state.suspended_reason else "")
            + ". AI features are unavailable for all branches and users; everything else keeps working.")
    else:
        title, body = "AI configuration required", (
            f"AI-powered features are unavailable for {org.name} because its AI configuration is not complete or "
            "not valid. Please contact the Super Admin. Everything else keeps working.")
    notify(service.db, admins, NotificationType.AI_STATUS, title, organization_id=organization_id, body=body,
           link="/organizationadmin/ai-usage", data=data, dedupe_key=key)
    if current == AIStatus.INVALID:
        masters = service.db.scalars(select(User.id).where(User.role == Role.MASTER_ADMIN, User.is_active,
                                                           User.deleted_at.is_(None)))
        notify(service.db, masters, NotificationType.AI_STATUS, f"{org.name}: AI configuration invalid",
               organization_id=organization_id, body=state.admin_message,
               link=f"/masteradmin/organization/{organization_id}/ai", data=data, dedupe_key=key)
    return True


def after_change(service, organization_id: uuid.UUID) -> Readiness:
    """Everything a configuration change sets off. Commits. Never fails the change itself."""
    state = readiness(service, organization_id)
    try:
        service.rearm_alerts(organization_id)
        announce(service, organization_id, state)
        service.db.commit()
        service.evaluate_alerts(organization_id)
        service.db.commit()
    except Exception:
        service.db.rollback()
        logger.exception("Could not finish the AI change of organization %s", organization_id)
    if _available(state.status):
        resume_indexing(service, organization_id)
    return state


def resume_indexing(service, organization_id: uuid.UUID) -> int:
    """Resume the documents whose AI indexing waited for the organization's AI."""
    try:
        from app.modules.ingestion.recovery import resume_organization
        from app.workers.runtime import get_runtime

        runtime = get_runtime()
        return resume_organization(service.db, runtime.queue, organization_id, service.settings.EMBED_BATCH_SIZE)
    except Exception:
        service.db.rollback()
        logger.warning("Could not resume AI indexing for organization %s", organization_id, exc_info=True)
        return 0
