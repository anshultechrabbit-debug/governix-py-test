import uuid
from typing import BinaryIO

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.database import translate_unique_violation
from app.core.exceptions import ConflictError, NotFoundError, PayloadTooLargeError, ValidationError
from app.core.security import hash_password
from app.infrastructure.storage.base import Storage
from app.modules.audit.service import record_event
from app.modules.auth.permissions import Principal, Role
from app.modules.categories.defaults import seed_default_categories
from app.modules.organizations import hierarchy
from app.modules.organizations.model import Organization
from app.modules.organizations.repository import OrganizationRepository
from app.modules.organizations.schema import (
    BRAND_FONTS,
    DeletionPreview,
    DeletionResult,
    OrganizationCreate,
    OrganizationOptions,
    OrganizationUpdate,
    TakenPort,
)
from app.modules.users.model import User
from app.modules.users.repository import UserRepository

MAX_LOGO_BYTES = 2 * 1024 * 1024
LOGO_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}
LOGO_MAGIC = (b"\x89PNG", b"\xff\xd8\xff", b"RIFF")
# Branding, contact details and the access port: copied on create, and clearable on update.
PROFILE_FIELDS = (
    "primary_color", "secondary_color", "brand_font", "contact_email", "contact_phone", "website", "address",
    "allowed_port",
)

class OrganizationService:
    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings
        self.repository = OrganizationRepository(session)

    def options(self) -> OrganizationOptions:
        return OrganizationOptions(
            port_min=self.settings.TENANT_PORT_MIN,
            port_max=self.settings.TENANT_PORT_MAX,
            taken_ports=[
                TakenPort(port=o.allowed_port, organization_id=o.id, organization_name=o.name)
                for o in self.repository.with_ports()
            ],
            fonts=list(BRAND_FONTS),
        )

    def _check_port(self, port: int | None, organization_id: uuid.UUID | None = None) -> None:
        """A port must lie in the tenant range and belong to no other organization."""
        if port is None:
            return
        low, high = self.settings.TENANT_PORT_MIN, self.settings.TENANT_PORT_MAX
        if not low <= port <= high:
            raise ValidationError(f"Choose a port between {low} and {high}.", code="PORT_OUT_OF_RANGE")
        owner = self.repository.by_port(port)
        if owner is not None and owner.id != organization_id:
            raise ConflictError(f"Port {port} is already used by {owner.name}.", code="PORT_TAKEN")

    def create(self, principal: Principal, data: OrganizationCreate) -> Organization:
        """Create the organisation and, when given, its first admin, in one transaction."""
        if data.admin is not None and UserRepository(self.session).email_exists(data.admin.email):
            raise ValidationError("A user with this email already exists.", code="EMAIL_TAKEN")
        self._check_port(data.allowed_port)
        with translate_unique_violation(
            self.session, "An organization with this slug or port already exists."
        ):
            organization = self.repository.add(Organization(
                name=data.name, slug=data.slug,
                **{field: getattr(data, field) for field in PROFILE_FIELDS},
            ))
            seed_default_categories(self.session, organization)
            record_event(
                self.session,
                "organization.created",
                actor=principal,
                organization_id=organization.id,
                resource_type="organization",
                resource_id=organization.id,
                details={"name": data.name, "slug": data.slug, "allowed_port": data.allowed_port},
            )
            if data.admin is not None:
                admin = User(
                    email=data.admin.email.lower(), full_name=data.admin.full_name,
                    password_hash=hash_password(data.admin.password), role=Role.ORG_ADMIN,
                    organization_id=organization.id,
                )
                self.session.add(admin)
                self.session.flush()
                record_event(
                    self.session, "user.created", actor=principal, organization_id=organization.id,
                    resource_type="user", resource_id=admin.id, details={"email": admin.email, "role": Role.ORG_ADMIN},
                )
            self.session.commit()
        return organization

    def set_logo(self, principal: Principal, organization_id: uuid.UUID, storage: Storage, *,
                 file: BinaryIO, filename: str) -> Organization:
        organization = self.get(organization_id)
        extension = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if extension not in LOGO_TYPES:
            raise ValidationError("Upload the logo as PNG, JPEG or WebP.", code="UNSUPPORTED_FILE_TYPE")
        data = file.read(MAX_LOGO_BYTES + 1)
        if len(data) > MAX_LOGO_BYTES:
            raise PayloadTooLargeError("The logo must be 2 MB or smaller.")
        if not data.startswith(LOGO_MAGIC):
            raise ValidationError("The file is not a valid image.", code="UNSUPPORTED_FILE_TYPE")
        key = f"branding/{organization.id}/logo{extension}"
        old = organization.logo_key
        storage.put(key, data)
        organization.logo_key, organization.logo_content_type = key, LOGO_TYPES[extension]
        record_event(self.session, "organization.logo_updated", actor=principal, organization_id=organization.id,
                     resource_type="organization", resource_id=organization.id)
        self.session.commit()
        if old and old != key:
            storage.delete(old)
        return organization

    def get(self, organization_id: uuid.UUID) -> Organization:
        organization = self.repository.get(organization_id)
        if organization is None:
            raise NotFoundError("Organization not found.")
        return organization

    def list(self, *, limit: int, offset: int) -> tuple[list[Organization], int]:
        return self.repository.list(limit=limit, offset=offset)

    def update(
        self, principal: Principal, organization_id: uuid.UUID, data: OrganizationUpdate
    ) -> Organization:
        organization = self.get(organization_id)
        changes = data.model_dump(exclude_unset=True)
        # Profile fields may be cleared; name and status only change when given.
        changes = {k: v for k, v in changes.items() if v is not None or k in PROFILE_FIELDS}
        if "allowed_port" in changes:
            self._check_port(changes["allowed_port"], organization.id)
        for field, value in changes.items():
            setattr(organization, field, value)
        record_event(
            self.session,
            "organization.updated",
            actor=principal,
            organization_id=organization.id,
            resource_type="organization",
            resource_id=organization.id,
            details={"changes": {k: str(v) for k, v in changes.items()}},
        )
        with translate_unique_violation(self.session, "Another organization already uses this port.", code="PORT_TAKEN"):
            self.session.commit()
        return organization

    def deletion_preview(self, organization_id: uuid.UUID) -> DeletionPreview:
        return hierarchy.preview(self.session, hierarchy.Target(self.get(organization_id)))

    def delete(self, principal: Principal, organization_id: uuid.UUID, *, confirm: bool) -> DeletionResult:
        """Soft-delete the organisation with its branches, departments and users (see hierarchy.py)."""
        return hierarchy.delete(self.session, principal, hierarchy.Target(self.get(organization_id)), confirm=confirm)
