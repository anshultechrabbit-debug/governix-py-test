import uuid

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.modules.branches.model import Branch
from app.modules.organizations.model import Organization
from app.modules.users.model import User


class UserRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, user_id: uuid.UUID) -> User | None:
        return self.session.get(User, user_id)

    def email_exists(self, email: str) -> bool:
        return (
            self.session.scalar(select(User.id).where(func.lower(User.email) == email.lower()))
            is not None
        )

    @staticmethod
    def _scoped(
        query: Select,
        *,
        organization_id: uuid.UUID | None,
        branch_id: uuid.UUID | None,
        roles: frozenset[str] | None,
    ) -> Select:
        if organization_id is not None:
            query = query.where(User.organization_id == organization_id)
        if branch_id is not None:
            query = query.where(User.branch_id == branch_id)
        if roles is not None:
            query = query.where(User.role.in_(roles))
        return query

    def list(
        self,
        *,
        organization_id: uuid.UUID | None,
        branch_id: uuid.UUID | None,
        roles: frozenset[str] | None,
        search: str | None,
        is_active: bool | None = None,
        limit: int,
        offset: int,
    ) -> tuple[list[tuple[User, str | None, str | None]], int]:
        """Users in scope, each with its organization's and branch's names (searchable too)."""
        query = self._scoped(
            select(User, Organization.name.label("organization_name"), Branch.name.label("branch_name"))
            .outerjoin(Organization, Organization.id == User.organization_id)
            .outerjoin(Branch, Branch.id == User.branch_id),
            organization_id=organization_id, branch_id=branch_id, roles=roles,
        )
        if is_active is not None:
            query = query.where(User.is_active.is_(is_active))
        if search:
            pattern = f"%{search.lower()}%"
            query = query.where(
                func.lower(User.email).like(pattern)
                | func.lower(User.full_name).like(pattern)
                | func.lower(Organization.name).like(pattern)
                | func.lower(Branch.name).like(pattern)
            )
        total = self.session.scalar(select(func.count()).select_from(query.subquery()))
        rows = self.session.execute(query.order_by(User.email).limit(limit).offset(offset)).all()
        return [(user, organization, branch) for user, organization, branch in rows], total or 0

    def count_by_status(
        self,
        *,
        organization_id: uuid.UUID | None,
        branch_id: uuid.UUID | None,
        roles: frozenset[str] | None,
    ) -> tuple[int, int]:
        """(active, inactive) users in scope."""
        query = self._scoped(
            select(User.is_active, func.count()).group_by(User.is_active),
            organization_id=organization_id, branch_id=branch_id, roles=roles,
        )
        counts = dict(self.session.execute(query).all())
        return counts.get(True, 0), counts.get(False, 0)
