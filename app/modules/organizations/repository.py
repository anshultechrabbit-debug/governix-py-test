import uuid

from sqlalchemy import String, func, select, update
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

    def by_domain(self, domain: str) -> Organization | None:
        return self.session.scalar(select(Organization).where(Organization.custom_domain == domain))

    def with_ports(self) -> list[Organization]:
        return list(self.session.scalars(
            select(Organization).where(Organization.allowed_port.is_not(None)).order_by(Organization.allowed_port)
        ))

    SORTS = {
        "name": lambda: func.lower(Organization.name),
        "created": lambda: Organization.created_at,
        "port": lambda: Organization.allowed_port,
        "status": lambda: Organization.status,
    }

    def list(self, *, limit: int, offset: int, search: str | None = None, status: str | None = None,
             sort: str = "name") -> tuple[list[Organization], int]:
        """Live organizations, searched (name, slug, contact email, port), filtered and sorted in the database."""
        query = select(Organization).where(Organization.deleted_at.is_(None))
        if status:
            query = query.where(Organization.status == status)
        if search:
            pattern = f"%{search.lower()}%"
            query = query.where(
                func.lower(Organization.name).like(pattern) | func.lower(Organization.slug).like(pattern)
                | func.lower(func.coalesce(Organization.contact_email, "")).like(pattern)
                | func.cast(Organization.allowed_port, String).like(pattern)
            )
        total = self.session.scalar(select(func.count()).select_from(query.subquery()))
        column = self.SORTS.get(sort.lstrip("-"), self.SORTS["name"])()
        order = column.desc().nulls_last() if sort.startswith("-") else column.asc().nulls_last()
        items = self.session.scalars(query.order_by(order, Organization.name).limit(limit).offset(offset)).all()
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
