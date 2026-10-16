import uuid

from sqlalchemy import ForeignKey, Index, String
from sqlalchemy import text as sql
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin


class Branch(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "branches"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), index=True
    )
    name: Mapped[str] = mapped_column(String(200))
    code: Mapped[str] = mapped_column(String(50))
    # Where the branch is. Required for a new branch (schema.BranchAddress); branches made
    # before these existed have none until edited.
    street_address: Mapped[str | None] = mapped_column(String(300))
    state: Mapped[str | None] = mapped_column(String(100))
    postal_code: Mapped[str | None] = mapped_column(String(20))
    is_active: Mapped[bool] = mapped_column(default=True)

    __table_args__ = (
        # A deleted branch frees its code.
        Index(
            "uq_branches_organization_id_code", "organization_id", "code", unique=True,
            postgresql_where=sql("deleted_at IS NULL"),
        ),
    )
