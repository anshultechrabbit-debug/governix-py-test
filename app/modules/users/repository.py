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
        user = self.session.get(User, user_id)
        return user if user is not None and user.deleted_at is None else None

    def email_exists(self, email: str) -> bool:
        """Whether a live account uses the email; a deleted one's may be reused."""
        return (
            self.session.scalar(
                select(User.id).where(func.lower(User.email) == email.lower(), User.deleted_at.is_(None))
            )
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
        query = query.where(User.deleted_at.is_(None))
        if organization_id is not None:
            query = query.where(User.organization_id == organization_id)
        if branch_id is not None:
            query = query.where(User.branch_id == branch_id)
        if roles is not None:
            query = query.where(User.role.in_(roles))
        return query

    # How a list may be ordered: the API's `sort` values (a leading "-" reverses).
    SORTS = {
        "name": lambda: func.lower(User.full_name),
        "email": lambda: func.lower(User.email),
        "created": lambda: User.created_at,
        "last_login": lambda: User.last_login_at,
        "organization": lambda: func.lower(func.coalesce(Organization.name, "")),
        "role": lambda: User.role,
    }

    def list(
        self,
        *,
        organization_id: uuid.UUID | None,
        branch_id: uuid.UUID | None,
        roles: frozenset[str] | None,
        search: str | None,
        is_active: bool | None = None,
        only_roles: frozenset[str] | None = None,
        in_organization: uuid.UUID | None = None,
        in_branch: uuid.UUID | None = None,
        sort: str = "name",
        limit: int,
        offset: int,
    ) -> tuple[list[tuple[User, str | None, str | None]], int]:
        """Users in scope, each with its organization's and branch's names (searchable too), filtered and sorted
        in the database: the page shows exactly what the server counted."""
        query = self._scoped(
            select(User, Organization.name.label("organization_name"), Branch.name.label("branch_name"))
            .outerjoin(Organization, Organization.id == User.organization_id)
            .outerjoin(Branch, Branch.id == User.branch_id),
            organization_id=organization_id, branch_id=branch_id, roles=roles,
        )
        if is_active is not None:
            query = query.where(User.is_active.is_(is_active))
        if only_roles is not None:
            query = query.where(User.role.in_(only_roles))
        if in_organization is not None:
            query = query.where(User.organization_id == in_organization)
        if in_branch is not None:
            query = query.where(User.branch_id == in_branch)
        if search:
            pattern = f"%{search.lower()}%"
            query = query.where(
                func.lower(User.email).like(pattern)
                | func.lower(User.full_name).like(pattern)
                | func.lower(func.coalesce(User.phone, "")).like(pattern)
                | func.lower(Organization.name).like(pattern)
                | func.lower(Branch.name).like(pattern)
            )
        total = self.session.scalar(select(func.count()).select_from(query.subquery()))
        key = sort.lstrip("-")
        column = self.SORTS.get(key, self.SORTS["name"])()
        order = column.desc().nulls_last() if sort.startswith("-") else column.asc().nulls_last()
        rows = self.session.execute(query.order_by(order, User.email).limit(limit).offset(offset)).all()
        return [(user, organization, branch) for user, organization, branch in rows], total or 0

    def counts(
        self, *, organization_id: uuid.UUID | None, branch_id: uuid.UUID | None, roles: frozenset[str] | None,
    ) -> tuple[dict[tuple[str, bool], int], int]:
        """({(role, is_active): users}, organizations those users belong to), in scope."""
        rows = self.session.execute(self._scoped(
            select(User.role, User.is_active, func.count()).group_by(User.role, User.is_active),
            organization_id=organization_id, branch_id=branch_id, roles=roles,
        )).all()
        organizations = self.session.scalar(self._scoped(
            select(func.count(func.distinct(User.organization_id))),
            organization_id=organization_id, branch_id=branch_id, roles=roles,
        )) or 0
        return {(role, active): n for role, active, n in rows}, organizations

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
