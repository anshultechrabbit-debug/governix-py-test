import uuid

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.modules.organizations.model import Organization


class OrganizationRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, organization_id: uuid.UUID) -> Organization | None:
        organization = self.session.get(Organization, organization_id)
        return organization if organization is not None and organization.deleted_at is None else None

    def by_port(self, port: int) -> Organization | None:
        return self.session.scalar(select(Organization).where(Organization.allowed_port == port))

    def with_ports(self) -> list[Organization]:
        return list(self.session.scalars(
            select(Organization).where(Organization.allowed_port.is_not(None)).order_by(Organization.allowed_port)
        ))

    def list(self, *, limit: int, offset: int) -> tuple[list[Organization], int]:
        live = Organization.deleted_at.is_(None)
        total = self.session.scalar(select(func.count()).select_from(Organization).where(live))
        items = self.session.scalars(
            select(Organization).where(live).order_by(Organization.name).limit(limit).offset(offset)
        ).all()
        return list(items), total or 0

    def add(self, organization: Organization) -> Organization:
        self.session.add(organization)
        self.session.flush()
        return organization


def bump_knowledge_version(session: Session, organization_id: uuid.UUID) -> None:
    """Invalidate every cached answer/retrieval for the organisation (part of cache keys)."""
    session.execute(
        update(Organization)
        .where(Organization.id == organization_id)
        .values(knowledge_version=Organization.knowledge_version + 1)
    )
