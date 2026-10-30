import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.modules.audit.model import AuditEvent


class AuditRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    @staticmethod
    def _scoped(query, organization_id: uuid.UUID | None, every_organization: bool):
        """One organization's events (None: the platform's own), or, for the master admin, all of them."""
        return query if every_organization else query.where(AuditEvent.organization_id == organization_id)

    # Before list(): below it, "list" in this class body is that method, not the type.
    def actions(self, organization_id: uuid.UUID | None, *, every_organization: bool = False) -> list[str]:
        """The event types recorded in the scope, for filtering ("ai.query", "user.created", ...)."""
        query = self._scoped(select(AuditEvent.action).distinct(), organization_id, every_organization)
        return list(self.session.scalars(query.order_by(AuditEvent.action)).all())

    def list(
        self,
        organization_id: uuid.UUID | None,
        *,
        action: str | None,
        resource_type: str | None,
        resource_id: uuid.UUID | None,
        actor_user_id: uuid.UUID | None,
        since: datetime | None,
        limit: int,
        offset: int,
        every_organization: bool = False,
    ) -> tuple[list[AuditEvent], int]:
        query = self._scoped(select(AuditEvent), organization_id, every_organization)
        if action:
            query = query.where(AuditEvent.action == action)
        if resource_type:
            query = query.where(AuditEvent.resource_type == resource_type)
        if resource_id:
            query = query.where(AuditEvent.resource_id == resource_id)
        if actor_user_id:
            query = query.where(AuditEvent.actor_user_id == actor_user_id)
        if since:
            query = query.where(AuditEvent.created_at >= since)
        total = self.session.scalar(select(func.count()).select_from(query.subquery()))
        items = self.session.scalars(
            query.order_by(AuditEvent.created_at.desc()).limit(limit).offset(offset)
        ).all()
        return list(items), total or 0
