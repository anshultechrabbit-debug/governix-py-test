"""Support staff management, each level by its own manager.

    Master Admin     platform staff (Technical Support): support_agent accounts it creates here, or
                     Master Admins; and which banks each one serves
    Org Admin        Head Office staff of their bank, and the staff of any of its branches
    Branch Manager   the staff of their own branch

A profile puts an existing account on duty; bank staff keep their normal role and work. Deactivating
or marking someone away never moves their tickets: a manager reassigns them (reassign_open), and each
move is recorded and notified.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from app.core.database import translate_unique_violation
from app.core.exceptions import ConflictError, NotFoundError, PermissionDeniedError, ValidationError
from app.core.security import generate_temporary_password, hash_password
from app.modules.audit.service import record_event
from app.modules.auth.permissions import Permission, Principal, Role
from app.modules.branches.model import Branch
from app.modules.organizations.model import Organization
from app.modules.tickets.assignment import open_counts
from app.modules.tickets.model import (
    CategoryRoute, StaffAvailability, SupportCategory, SupportStaff, SupportStaffOrganization, TicketLevel,
)
from app.modules.tickets.triage import DEFAULT_CATEGORIES, CategoryOption, categories_for
from app.modules.users.model import User
from app.modules.users.repository import UserRepository
from app.modules.users.service import EMAIL_TAKEN

MAX_SKILLS = 30


@dataclass
class StaffView:
    staff: SupportStaff
    user: User
    branch_name: str | None
    organization_name: str | None
    open_tickets: int
    serves: list[tuple[uuid.UUID, str]]


def clean_skills(skills: list[str] | None) -> list[str]:
    seen: dict[str, str] = {}
    for skill in skills or []:
        tag = " ".join(str(skill).split())[:60]
        if tag and tag.lower() not in seen:
            seen[tag.lower()] = tag
    if len(seen) > MAX_SKILLS:
        raise ValidationError(f"At most {MAX_SKILLS} skills.")
    return list(seen.values())


class StaffService:
    def __init__(self, session: Session) -> None:
        self.session = session

    # --- scope ---------------------------------------------------------------------------------------------

    @staticmethod
    def _require(principal: Principal) -> None:
        if not principal.has(Permission.SUPPORT_STAFF_MANAGE):
            raise PermissionDeniedError("You do not manage support staff.")

    def _may_manage(self, principal: Principal, level: str, organization_id: uuid.UUID | None,
                    branch_id: uuid.UUID | None) -> bool:
        if level == TicketLevel.PLATFORM:
            return principal.role is Role.MASTER_ADMIN
        if principal.organization_id is None or organization_id != principal.organization_id:
            return False
        if principal.role is Role.ORG_ADMIN:
            return True
        return (principal.role is Role.BRANCH_MANAGER and level == TicketLevel.BRANCH
                and branch_id == principal.branch_id)

    def _scope_clause(self, principal: Principal):
        if principal.role is Role.MASTER_ADMIN:
            return SupportStaff.level == TicketLevel.PLATFORM
        if principal.role is Role.ORG_ADMIN:
            return SupportStaff.organization_id == principal.organization_id
        return (SupportStaff.level == TicketLevel.BRANCH) & (SupportStaff.branch_id == principal.branch_id) \
            & (SupportStaff.organization_id == principal.organization_id)

    def _get(self, principal: Principal, staff_id: uuid.UUID) -> SupportStaff:
        self._require(principal)
        staff = self.session.get(SupportStaff, staff_id)
        if staff is None or not self._may_manage(principal, staff.level, staff.organization_id, staff.branch_id):
            raise NotFoundError("Support staff member not found.")
        return staff

    def _branch(self, principal: Principal, level: str, branch_id: uuid.UUID | None) -> uuid.UUID | None:
        """The branch of a new branch-level profile, checked to be in the caller's organization."""
        if level != TicketLevel.BRANCH:
            return None
        if principal.role is Role.BRANCH_MANAGER:
            branch_id = principal.branch_id
        branch = self.session.get(Branch, branch_id) if branch_id else None
        if branch is None or branch.organization_id != principal.organization_id or branch.deleted_at is not None:
            raise ValidationError("Choose a branch of your organization.", code="BRANCH_NOT_FOUND")
        return branch.id

    # --- read ----------------------------------------------------------------------------------------------

    def list(self, principal: Principal, *, level: str | None = None, branch_id: uuid.UUID | None = None,
             search: str | None = None, include_inactive: bool = True) -> list[StaffView]:
        self._require(principal)
        query = (
            select(SupportStaff, User, Branch.name, Organization.name)
            .join(User, User.id == SupportStaff.user_id)
            .outerjoin(Branch, Branch.id == SupportStaff.branch_id)
            .outerjoin(Organization, Organization.id == SupportStaff.organization_id)
            .where(self._scope_clause(principal), User.deleted_at.is_(None))
        )
        if level:
            query = query.where(SupportStaff.level == level)
        if branch_id:
            query = query.where(SupportStaff.branch_id == branch_id)
        if not include_inactive:
            query = query.where(SupportStaff.is_active)
        if search and search.strip():
            term = f"%{search.strip().lower()}%"
            query = query.where(or_(func.lower(User.full_name).like(term), func.lower(User.email).like(term)))
        rows = self.session.execute(query.order_by(SupportStaff.level, Branch.name, User.full_name)).all()
        return self._views(rows)

    def view(self, principal: Principal, staff_id: uuid.UUID) -> StaffView:
        return self._view_of(self._get(principal, staff_id).id)

    def mine(self, principal: Principal) -> StaffView | None:
        """The caller's own duty, whoever manages it."""
        staff_id = self.session.scalar(select(SupportStaff.id).where(SupportStaff.user_id == principal.user_id))
        return self._view_of(staff_id) if staff_id else None

    def _view_of(self, staff_id: uuid.UUID) -> StaffView:
        row = self.session.execute(
            select(SupportStaff, User, Branch.name, Organization.name).join(User, User.id == SupportStaff.user_id)
            .outerjoin(Branch, Branch.id == SupportStaff.branch_id)
            .outerjoin(Organization, Organization.id == SupportStaff.organization_id)
            .where(SupportStaff.id == staff_id)
        ).one()
        return self._views([row])[0]

    def _views(self, rows) -> list[StaffView]:
        counts = open_counts(self.session, [user.id for _, user, _, _ in rows])
        platform = [staff.id for staff, *_ in rows if staff.level == TicketLevel.PLATFORM]
        serves: dict[uuid.UUID, list[tuple[uuid.UUID, str]]] = {}
        if platform:
            for staff_id, org_id, name in self.session.execute(
                select(SupportStaffOrganization.staff_id, Organization.id, Organization.name)
                .join(Organization, Organization.id == SupportStaffOrganization.organization_id)
                .where(SupportStaffOrganization.staff_id.in_(platform)).order_by(Organization.name)
            ).all():
                serves.setdefault(staff_id, []).append((org_id, name))
        return [StaffView(staff, user, branch, org, counts.get(user.id, 0), serves.get(staff.id, []))
                for staff, user, branch, org in rows]

    def candidates(self, principal: Principal, *, level: str, branch_id: uuid.UUID | None,
                   search: str | None, limit: int = 20) -> list[tuple[User, str | None]]:
        """Accounts that may be put on duty at that level (and are not on duty already)."""
        self._require(principal)
        organization_id = None if level == TicketLevel.PLATFORM else principal.organization_id
        branch_id = self._branch(principal, level, branch_id)
        if not self._may_manage(principal, level, organization_id, branch_id):
            raise PermissionDeniedError("You cannot manage support staff at that level.")
        query = (
            select(User, Branch.name).outerjoin(Branch, Branch.id == User.branch_id)
            .where(User.is_active, User.deleted_at.is_(None),
                   ~select(SupportStaff.id).where(SupportStaff.user_id == User.id).exists())
        )
        if level == TicketLevel.PLATFORM:
            query = query.where(User.role.in_([Role.SUPPORT_AGENT, Role.MASTER_ADMIN]))
        elif level == TicketLevel.ORGANIZATION:
            query = query.where(User.organization_id == organization_id,
                                User.role.in_([Role.ORG_ADMIN, Role.BRANCH_MANAGER, Role.DEPARTMENT_USER]))
        else:
            query = query.where(User.organization_id == organization_id, User.branch_id == branch_id,
                                User.role.in_([Role.BRANCH_MANAGER, Role.DEPARTMENT_USER]))
        if search and search.strip():
            term = f"%{search.strip().lower()}%"
            query = query.where(or_(func.lower(User.full_name).like(term), func.lower(User.email).like(term)))
        return list(self.session.execute(query.order_by(User.full_name).limit(limit)).all())

    # --- write ---------------------------------------------------------------------------------------------

    def create(self, principal: Principal, *, level: str, user_id: uuid.UUID | None, new_account: dict | None,
               branch_id: uuid.UUID | None, staff_role: str, skills: list[str], capacity: int,
               availability: str, serves: list[uuid.UUID] | None) -> tuple[SupportStaff, str | None]:
        """Put an account on duty (or, for the platform, create a support agent account). Returns the profile
        and, for a new account, its temporary password (shown once)."""
        self._require(principal)
        organization_id = None if level == TicketLevel.PLATFORM else principal.organization_id
        branch_id = self._branch(principal, level, branch_id)
        if not self._may_manage(principal, level, organization_id, branch_id):
            raise PermissionDeniedError("You cannot manage support staff at that level.")
        password = None
        if new_account is not None:
            if level != TicketLevel.PLATFORM:
                raise ValidationError("Add someone who already has an account in your organization.")
            user, password = self._new_agent(principal, new_account)
        else:
            if user_id is None:
                raise ValidationError("Choose a person.")
            eligible = {u.id for u, _ in self.candidates(principal, level=level, branch_id=branch_id, search=None,
                                                        limit=10_000)}
            if user_id not in eligible:
                if self.session.scalar(select(SupportStaff.id).where(SupportStaff.user_id == user_id)):
                    raise ConflictError("That person is already on support duty.", code="ALREADY_STAFF")
                raise ValidationError("That person cannot be added at this level.", code="NOT_ELIGIBLE")
            user = self.session.get(User, user_id)
        staff = SupportStaff(
            user_id=user.id, level=level, organization_id=organization_id, branch_id=branch_id,
            staff_role=staff_role, skills=clean_skills(skills), capacity=capacity, availability=availability,
            is_active=True, created_by_id=principal.user_id,
        )
        self.session.add(staff)
        with translate_unique_violation(self.session, "That person is already on support duty.", code="ALREADY_STAFF"):
            self.session.flush()
        if level == TicketLevel.PLATFORM:
            self._set_serves(staff, serves or [])
        record_event(self.session, "support_staff.added", actor=principal, organization_id=organization_id,
                     resource_type="support_staff", resource_id=staff.id,
                     details={"user_id": str(user.id), "level": level, "branch_id": str(branch_id) if branch_id else None,
                              "staff_role": staff_role, "capacity": capacity})
        with translate_unique_violation(self.session, EMAIL_TAKEN, code="EMAIL_TAKEN"):
            self.session.commit()
        return staff, password

    def _new_agent(self, principal: Principal, details: dict) -> tuple[User, str]:
        email = str(details["email"]).lower()
        if UserRepository(self.session).email_exists(email):
            raise ValidationError(EMAIL_TAKEN, code="EMAIL_TAKEN")
        password = generate_temporary_password()
        user = User(email=email, full_name=details["full_name"], phone=details.get("phone"),
                    password_hash=hash_password(password), role=Role.SUPPORT_AGENT)
        self.session.add(user)
        self.session.flush()
        record_event(self.session, "user.created", actor=principal, organization_id=None, resource_type="user",
                     resource_id=user.id, details={"email": user.email, "role": Role.SUPPORT_AGENT})
        return user, password

    def _set_serves(self, staff: SupportStaff, organization_ids: list[uuid.UUID]) -> None:
        wanted = set(organization_ids)
        if wanted:
            found = set(self.session.scalars(select(Organization.id).where(
                Organization.id.in_(wanted), Organization.deleted_at.is_(None))))
            if found != wanted:
                raise ValidationError("Choose organizations that exist.", code="ORGANIZATION_NOT_FOUND")
        self.session.execute(delete(SupportStaffOrganization).where(SupportStaffOrganization.staff_id == staff.id))
        for organization_id in sorted(wanted):
            self.session.add(SupportStaffOrganization(staff_id=staff.id, organization_id=organization_id))

    def update(self, principal: Principal, staff_id: uuid.UUID, changes: dict) -> SupportStaff:
        staff = self._get(principal, staff_id)
        recorded: dict = {}
        for key in ("staff_role", "capacity", "availability", "is_active"):
            if key in changes and changes[key] is not None and getattr(staff, key) != changes[key]:
                recorded[key] = {"from": getattr(staff, key), "to": changes[key]}
                setattr(staff, key, changes[key])
        if changes.get("skills") is not None:
            skills = clean_skills(changes["skills"])
            if skills != staff.skills:
                recorded["skills"] = {"from": staff.skills, "to": skills}
                staff.skills = skills
        if changes.get("serves") is not None:
            if staff.level != TicketLevel.PLATFORM:
                raise ValidationError("Only platform staff serve chosen organizations.")
            self._set_serves(staff, changes["serves"])
            recorded["serves"] = [str(o) for o in changes["serves"]]
        if recorded:
            record_event(self.session, "support_staff.updated", actor=principal, organization_id=staff.organization_id,
                         resource_type="support_staff", resource_id=staff.id, details={"changes": recorded})
        self.session.commit()
        return staff

    def set_my_availability(self, principal: Principal, availability: StaffAvailability) -> SupportStaff:
        staff = self.session.scalar(select(SupportStaff).where(SupportStaff.user_id == principal.user_id))
        if staff is None or not staff.is_active:
            raise NotFoundError("You are not on support duty.")
        if staff.availability != availability:
            record_event(self.session, "support_staff.updated", actor=principal, organization_id=staff.organization_id,
                         resource_type="support_staff", resource_id=staff.id,
                         details={"changes": {"availability": {"from": staff.availability, "to": availability}}})
            staff.availability = availability
        self.session.commit()
        return staff

    def remove(self, principal: Principal, staff_id: uuid.UUID) -> None:
        """Take someone off duty for good. Their open tickets must be reassigned first."""
        staff = self._get(principal, staff_id)
        if open_counts(self.session, [staff.user_id]).get(staff.user_id):
            raise ConflictError("Reassign their open tickets first, or deactivate them instead.", code="HAS_OPEN_TICKETS")
        record_event(self.session, "support_staff.removed", actor=principal, organization_id=staff.organization_id,
                     resource_type="support_staff", resource_id=staff.id, details={"user_id": str(staff.user_id)})
        self.session.delete(staff)
        self.session.commit()

    def profile_of(self, principal: Principal, staff_id: uuid.UUID) -> SupportStaff:
        return self._get(principal, staff_id)


# --- categories (Org Admin) ---------------------------------------------------------------------------------

class CategoryService:
    def __init__(self, session: Session) -> None:
        self.session = session

    @staticmethod
    def _require(principal: Principal) -> uuid.UUID:
        if principal.role is not Role.ORG_ADMIN or principal.organization_id is None:
            raise PermissionDeniedError("Only the Organization Admin sets up ticket categories.")
        return principal.organization_id

    def _materialize(self, organization_id: uuid.UUID) -> None:
        """The first change to an organization's categories starts from the defaults."""
        if self.session.scalar(select(SupportCategory.id).where(SupportCategory.organization_id == organization_id).limit(1)):
            return
        for order, option in enumerate(DEFAULT_CATEGORIES):
            self.session.add(SupportCategory(organization_id=organization_id, name=option.name, route=option.route,
                                             description=option.description, sort_order=order * 10))
        self.session.flush()

    def list(self, principal: Principal) -> list[tuple[CategoryOption, bool]]:
        """Every category of the organization, with whether it is offered."""
        organization_id = self._require(principal)
        active = {c.id for c in categories_for(self.session, organization_id)}
        return [(c, c.id in active) for c in categories_for(self.session, organization_id, include_inactive=True)]

    def create(self, principal: Principal, *, name: str, route: CategoryRoute, description: str | None) -> SupportCategory:
        organization_id = self._require(principal)
        self._materialize(organization_id)
        last = self.session.scalar(select(func.max(SupportCategory.sort_order))
                                   .where(SupportCategory.organization_id == organization_id)) or 0
        category = SupportCategory(organization_id=organization_id, name=" ".join(name.split()), route=route,
                                   description=(description or "").strip() or None, sort_order=last + 10)
        self.session.add(category)
        with translate_unique_violation(self.session, "A category with this name exists.", code="CATEGORY_EXISTS"):
            self.session.flush()
            record_event(self.session, "support_category.created", actor=principal, resource_type="support_category",
                         resource_id=category.id, details={"name": category.name, "route": route})
            self.session.commit()
        return category

    def update(self, principal: Principal, category_id: str, changes: dict) -> SupportCategory:
        organization_id = self._require(principal)
        if category_id.startswith("default:"):
            self._materialize(organization_id)
            name = next((c.name for c in DEFAULT_CATEGORIES if c.id == category_id), None)
            category = self.session.scalar(select(SupportCategory).where(
                SupportCategory.organization_id == organization_id, func.lower(SupportCategory.name) == (name or "").lower()))
        else:
            try:
                category = self.session.get(SupportCategory, uuid.UUID(category_id))
            except ValueError:
                category = None
        if category is None or category.organization_id != organization_id:
            raise NotFoundError("Category not found.")
        if changes.get("name"):
            category.name = " ".join(changes["name"].split())
        for key in ("route", "is_active", "sort_order"):
            if changes.get(key) is not None:
                setattr(category, key, changes[key])
        if "description" in changes:
            category.description = (changes["description"] or "").strip() or None
        category.updated_at = datetime.now(UTC)
        with translate_unique_violation(self.session, "A category with this name exists.", code="CATEGORY_EXISTS"):
            record_event(self.session, "support_category.updated", actor=principal, resource_type="support_category",
                         resource_id=category.id, details={k: str(v) for k, v in changes.items()})
            self.session.commit()
        return category
