import re
import uuid
from datetime import UTC, datetime

from sqlalchemy import delete, update
from sqlalchemy.orm import Session

from app.core.database import translate_unique_violation
from app.core.exceptions import ConflictError, NotFoundError, PermissionDeniedError, ValidationError
from app.core.security import generate_temporary_password, hash_password
from app.modules.audit.service import record_event
from app.modules.auth.model import RefreshToken
from app.modules.auth.permissions import ASSIGNABLE_ROLES, Permission, Principal, Role
from app.modules.branches.model import Branch
from app.modules.branches.schema import NewBranch
from app.modules.branches.repository import BranchRepository
from app.modules.departments.repository import DepartmentRepository
from app.modules.notifications.model import Notification
from app.modules.organizations.repository import OrganizationRepository
from app.modules.users.model import User
from app.modules.users.repository import UserRepository
from app.modules.users.schema import UserCreate, UserStatus, UserUpdate

EMAIL_TAKEN = "A user with this email already exists."


class UserService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.repository = UserRepository(session)
        self.organizations = OrganizationRepository(session)
        self.branches = BranchRepository(session)
        self.departments = DepartmentRepository(session)

    # --- scope rules -------------------------------------------------------

    def _can_manage(self, principal: Principal, target: User) -> bool:
        if Role(target.role) not in ASSIGNABLE_ROLES[principal.role]:
            return False
        if principal.role is Role.MASTER_ADMIN:
            return True
        if target.organization_id != principal.organization_id:
            return False
        if principal.role is Role.BRANCH_MANAGER:
            return target.branch_id == principal.branch_id
        return True

    def _resolve_scope(
        self,
        principal: Principal,
        role: Role,
        organization_id: uuid.UUID | None,
        branch_id: uuid.UUID | None,
        department_id: uuid.UUID | None,
    ) -> tuple[uuid.UUID | None, uuid.UUID | None, uuid.UUID | None]:
        """Validate and normalise the org/branch/department a user with `role` belongs to."""
        if role not in ASSIGNABLE_ROLES[principal.role]:
            raise PermissionDeniedError(f"You cannot assign the role {role}.")

        if principal.role is Role.MASTER_ADMIN:
            if organization_id is None or self.organizations.get(organization_id) is None:
                raise ValidationError("A valid organization_id is required.")
        else:
            organization_id = principal.organization_id
        if principal.role is Role.BRANCH_MANAGER:
            branch_id = principal.branch_id

        if role in (Role.ORG_ADMIN,):
            return organization_id, None, None

        if branch_id is None or self.branches.get_in_org(organization_id, branch_id) is None:
            raise ValidationError("A valid branch_id in this organization is required.")
        if role is Role.BRANCH_MANAGER:
            return organization_id, branch_id, None

        # A User belongs to the branch; a department is optional.
        if department_id is None:
            return organization_id, branch_id, None
        department = self.departments.get_in_org(organization_id, department_id)
        if department is None or department.branch_id != branch_id:
            raise ValidationError("The department does not belong to the selected branch.")
        return organization_id, branch_id, department_id

    # --- operations --------------------------------------------------------

    def create(self, principal: Principal, data: UserCreate) -> tuple[User, str | None]:
        """The new user, and its temporary password when the server generated one."""
        if data.role not in ASSIGNABLE_ROLES[principal.role]:  # before anything that reveals data
            raise PermissionDeniedError(f"You cannot assign the role {data.role}.")
        if self.repository.email_exists(data.email):
            raise ValidationError(EMAIL_TAKEN, code="EMAIL_TAKEN")
        branch_id = data.branch_id
        if data.role is Role.BRANCH_MANAGER and branch_id is None and data.new_branch is not None:
            branch_id = self._new_branch(principal, data.new_branch)
        organization_id, branch_id, department_id = self._resolve_scope(
            principal, data.role, data.organization_id, branch_id, data.department_id
        )
        generated = None if data.password else generate_temporary_password()
        user = User(
            email=data.email.lower(),
            full_name=data.full_name,
            phone=data.phone,
            password_hash=hash_password(data.password or generated),
            role=data.role,
            organization_id=organization_id,
            branch_id=branch_id,
            department_id=department_id,
        )
        with translate_unique_violation(self.session, EMAIL_TAKEN, code="EMAIL_TAKEN"):
            self.session.add(user)
            self.session.flush()
            record_event(
                self.session,
                "user.created",
                actor=principal,
                organization_id=organization_id,
                resource_type="user",
                resource_id=user.id,
                details={"email": user.email, "role": data.role},
            )
            self.session.commit()
        return user, generated

    def _new_branch(self, principal: Principal, details: NewBranch) -> uuid.UUID:
        """The branch a new Branch Manager is created with (Add user > new branch), in their transaction."""
        if not principal.has(Permission.BRANCHES_MANAGE) or principal.organization_id is None:
            raise PermissionDeniedError("You cannot create branches.")
        organization_id = principal.organization_id
        name = " ".join(details.name.split())
        if self.branches.named(organization_id, name) is not None:
            raise ValidationError(
                f"A branch named “{name}” already exists. Choose it from the list.", code="BRANCH_EXISTS"
            )
        # A code like the ones admins type on the Branches page: "Downtown Main" -> DOWNTOWN-MAIN.
        base = re.sub(r"[^A-Z0-9]+", "-", name.upper()).strip("-")[:40] or "BRANCH"
        code, suffix = base, 2
        while self.branches.code_taken(organization_id, code):
            code, suffix = f"{base}-{suffix}", suffix + 1
        address = details.model_dump(exclude={"name"})
        branch = Branch(organization_id=organization_id, name=name, code=code, **address)
        self.session.add(branch)
        self.session.flush()
        record_event(
            self.session, "branch.created", actor=principal, resource_type="branch",
            resource_id=branch.id, details={"name": name, "code": code, **address},
        )
        return branch.id

    def get(self, principal: Principal, user_id: uuid.UUID) -> User:
        user = self.repository.get(user_id)
        if user is None or (user.id != principal.user_id and not self._can_manage(principal, user)):
            raise NotFoundError("User not found.")
        return user

    @staticmethod
    def _list_scope(principal: Principal) -> dict:
        if principal.role is Role.MASTER_ADMIN:
            # Every organization's admins, and the platform administrators themselves.
            return {"organization_id": None, "branch_id": None,
                    "roles": frozenset({Role.ORG_ADMIN, Role.MASTER_ADMIN})}
        # The people the principal manages: an Organization Admin's Branch Managers, a Branch
        # Manager's Users.
        return {
            "organization_id": principal.organization_id,
            "branch_id": principal.branch_id if principal.role is Role.BRANCH_MANAGER else None,
            "roles": frozenset(ASSIGNABLE_ROLES[principal.role]),
        }

    def list(
        self, principal: Principal, *, search: str | None, status: UserStatus | None = None,
        limit: int, offset: int,
    ) -> tuple[list[tuple[User, str | None, str | None]], int]:
        return self.repository.list(
            **self._list_scope(principal),
            search=search,
            is_active=None if status is None else status == "active",
            limit=limit,
            offset=offset,
        )

    def stats(self, principal: Principal) -> tuple[int, int]:
        """(active, inactive) users the principal can list, ignoring search and filters."""
        return self.repository.count_by_status(**self._list_scope(principal))

    def names(self, user: User) -> tuple[str | None, str | None]:
        """The user's organization and branch names."""
        organization = self.organizations.get(user.organization_id) if user.organization_id else None
        branch = (
            self.branches.get_in_org(user.organization_id, user.branch_id)
            if user.organization_id and user.branch_id else None
        )
        return (organization.name if organization else None), (branch.name if branch else None)

    def update(self, principal: Principal, user_id: uuid.UUID, data: UserUpdate) -> User:
        user = self.repository.get(user_id)
        if user is None or not self._can_manage(principal, user):
            raise NotFoundError("User not found.")
        changes = data.model_dump(exclude_unset=True)
        if user.id == principal.user_id and (
            changes.get("is_active") is False or "role" in changes
        ):
            raise ValidationError("You cannot deactivate yourself or change your own role.")
        if "organization_id" in changes:
            if changes["organization_id"] == user.organization_id:
                del changes["organization_id"]
            elif principal.role is not Role.MASTER_ADMIN:
                raise PermissionDeniedError("Only the Master Admin can move a user to another organization.")
            elif user.last_login_at is not None:
                # Their notifications and history belong to the organization they worked in.
                raise ConflictError(
                    f"{user.full_name} has signed in before, so the account stays with its organization."
                    " Add a new account in the other organization instead.",
                    code="USER_HAS_SIGNED_IN",
                )

        scope_fields = {"role", "organization_id", "branch_id", "department_id"}
        if scope_fields & changes.keys():
            previous_organization_id = user.organization_id
            role = Role(changes.get("role") or user.role)
            organization_id, branch_id, department_id = self._resolve_scope(
                principal,
                role,
                changes.get("organization_id", user.organization_id),
                changes.get("branch_id", user.branch_id),
                changes.get("department_id", user.department_id),
            )
            user.role, user.organization_id = role, organization_id
            user.branch_id, user.department_id = branch_id, department_id
            user.token_version += 1
            if organization_id != previous_organization_id:
                # Notifications name the old organization's policies and documents.
                self.session.execute(
                    delete(Notification).where(
                        Notification.user_id == user.id, Notification.organization_id == previous_organization_id
                    )
                )
        if changes.get("full_name"):
            user.full_name = changes["full_name"]
        if changes.get("email") and changes["email"].lower() != user.email:
            if self.repository.email_exists(changes["email"]):
                raise ValidationError(EMAIL_TAKEN, code="EMAIL_TAKEN")
            user.email = changes["email"].lower()
        if "phone" in changes:
            user.phone = changes["phone"]
        if changes.get("is_active") is not None and changes["is_active"] != user.is_active:
            user.is_active = changes["is_active"]
            user.token_version += 1
            if not user.is_active:
                self._revoke_refresh_tokens(user.id)

        record_event(
            self.session, "user.updated", actor=principal, organization_id=user.organization_id,
            resource_type="user", resource_id=user.id,
            details={"changes": {k: str(v) for k, v in changes.items()}},
        )
        with translate_unique_violation(self.session, EMAIL_TAKEN, code="EMAIL_TAKEN"):
            self.session.commit()
        return user

    def reset_password(
        self, principal: Principal, user_id: uuid.UUID, new_password: str | None
    ) -> str | None:
        """Set the given password, or a generated one, which is returned."""
        user = self.repository.get(user_id)
        if user is None or not self._can_manage(principal, user):
            raise NotFoundError("User not found.")
        generated = None if new_password else generate_temporary_password()
        user.password_hash = hash_password(new_password or generated)
        user.token_version += 1
        user.failed_login_attempts = 0
        user.locked_until = None
        self._revoke_refresh_tokens(user.id)
        record_event(
            self.session, "user.password_reset", actor=principal,
            organization_id=user.organization_id, resource_type="user", resource_id=user.id,
        )
        self.session.commit()
        return generated

    def delete(self, principal: Principal, user_id: uuid.UUID) -> None:
        """Soft-delete the account: it leaves every list and its sessions end, but the row stays, so
        audit entries keep the name. Its email may be used for a new account.
        """
        user = self.repository.get(user_id)
        if user is None or not self._can_manage(principal, user):
            raise NotFoundError("User not found.")
        if user.id == principal.user_id:
            raise ValidationError("You cannot delete yourself.")
        user.deleted_at, user.deleted_by_id = datetime.now(UTC), principal.user_id
        user.is_active = False
        user.token_version += 1
        self._revoke_refresh_tokens(user.id)
        record_event(
            self.session, "user.deleted", actor=principal, organization_id=user.organization_id,
            resource_type="user", resource_id=user.id, details={"email": user.email, "role": user.role},
        )
        self.session.commit()

    def _revoke_refresh_tokens(self, user_id: uuid.UUID) -> None:
        self.session.execute(
            update(RefreshToken)
            .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=datetime.now(UTC))
        )


def create_master_admin(session: Session, email: str, full_name: str, password: str) -> User:
    """Bootstrap the first platform administrator (CLI only; no API can create one)."""
    repository = UserRepository(session)
    if repository.email_exists(email):
        raise ValidationError(EMAIL_TAKEN, code="EMAIL_TAKEN")
    user = User(
        email=email.lower(),
        full_name=full_name,
        password_hash=hash_password(password),
        role=Role.MASTER_ADMIN,
    )
    session.add(user)
    session.flush()
    record_event(session, "user.master_admin_bootstrapped", resource_type="user", resource_id=user.id)
    session.commit()
    return user
