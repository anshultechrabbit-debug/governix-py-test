import uuid
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Index, String, func
from sqlalchemy import text as sql
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin


class User(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(320))
    full_name: Mapped[str] = mapped_column(String(200))
    phone: Mapped[str | None] = mapped_column(String(50))
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(30))
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), index=True
    )
    branch_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("branches.id", ondelete="RESTRICT"), index=True
    )
    department_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("departments.id", ondelete="RESTRICT"), index=True
    )
    is_active: Mapped[bool] = mapped_column(default=True)
    # Incremented to invalidate every issued access token (deactivation, role change, password change).
    token_version: Mapped[int] = mapped_column(default=0)
    failed_login_attempts: Mapped[int] = mapped_column(default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Two-step sign-in (auth/mfa.py). The authenticator secret, encrypted; set but not enabled while being set up.
    mfa_secret: Mapped[str | None] = mapped_column(String(512))
    mfa_enabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The last 30-second step whose code was accepted: a code is never accepted twice.
    mfa_last_step: Mapped[int | None] = mapped_column(BigInteger)

    @property
    def mfa_enabled(self) -> bool:
        return self.mfa_enabled_at is not None

    __table_args__ = (
        # A deleted account frees its email for a new one.
        Index("uq_users_email_lower", func.lower(email), unique=True, postgresql_where=sql("deleted_at IS NULL")),
        # The role determines exactly which scope columns must be set.
        CheckConstraint(
            "(role IN ('master_admin', 'support_agent') AND organization_id IS NULL AND branch_id IS NULL"
            " AND department_id IS NULL)"
            " OR (role = 'org_admin' AND organization_id IS NOT NULL AND branch_id IS NULL AND department_id IS NULL)"
            " OR (role = 'branch_manager' AND organization_id IS NOT NULL AND branch_id IS NOT NULL AND department_id IS NULL)"
            # A User belongs to a branch; a department is optional.
            " OR (role = 'department_user' AND organization_id IS NOT NULL AND branch_id IS NOT NULL)",
            name="role_scope",
        ),
    )
