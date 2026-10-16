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
    ) -> tuple[list[Branch], int]:
        query = select(Branch).where(Branch.organization_id == organization_id, Branch.deleted_at.is_(None))
        if only_branch_id is not None:
            query = query.where(Branch.id == only_branch_id)
        total = self.session.scalar(select(func.count()).select_from(query.subquery()))
        items = self.session.scalars(query.order_by(Branch.name).limit(limit).offset(offset)).all()
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
