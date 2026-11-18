import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.modules.branches.model import Branch


class BranchRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get_in_org(self, organization_id: uuid.UUID, branch_id: uuid.UUID) -> Branch | None:
        return self.session.scalar(
            select(Branch).where(
                Branch.id == branch_id, Branch.organization_id == organization_id, Branch.deleted_at.is_(None)
            )
        )

    def list(
        self,
        organization_id: uuid.UUID,
        *,
        only_branch_id: uuid.UUID | None,
        limit: int,
        offset: int,
        search: str | None = None,
        is_active: bool | None = None,
        sort: str = "name",
    ) -> tuple[list[Branch], int]:
        """The organization's live branches, searched (name, code, state, postal code), filtered and sorted."""
        query = select(Branch).where(Branch.organization_id == organization_id, Branch.deleted_at.is_(None))
        if only_branch_id is not None:
            query = query.where(Branch.id == only_branch_id)
        if is_active is not None:
            query = query.where(Branch.is_active.is_(is_active))
        if search:
            pattern = f"%{search.lower()}%"
            query = query.where(
                func.lower(Branch.name).like(pattern) | func.lower(Branch.code).like(pattern)
                | func.lower(func.coalesce(Branch.state, "")).like(pattern)
                | func.lower(func.coalesce(Branch.postal_code, "")).like(pattern)
            )
        total = self.session.scalar(select(func.count()).select_from(query.subquery()))
        columns = {"name": func.lower(Branch.name), "code": func.lower(Branch.code), "created": Branch.created_at}
        column = columns.get(sort.lstrip("-"), columns["name"])
        order = column.desc() if sort.startswith("-") else column.asc()
        items = self.session.scalars(query.order_by(order, Branch.name).limit(limit).offset(offset)).all()
        return list(items), total or 0

    def named(self, organization_id: uuid.UUID, name: str) -> Branch | None:
        return self.session.scalar(
            select(Branch).where(
                Branch.organization_id == organization_id, func.lower(Branch.name) == name.lower(),
                Branch.deleted_at.is_(None),
            )
        )

    def code_taken(self, organization_id: uuid.UUID, code: str) -> bool:
        return self.session.scalar(
            select(Branch.id).where(
                Branch.organization_id == organization_id, Branch.code == code, Branch.deleted_at.is_(None)
            )
        ) is not None
