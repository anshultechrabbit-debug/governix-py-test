import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.core.config import get_settings
from app.modules.organizations.model import OrganizationStatus
from app.modules.users.schema import MIN_PASSWORD_LENGTH

SLUG_PATTERN = r"^[a-z0-9][a-z0-9-]{1,98}[a-z0-9]$"
COLOR_PATTERN = r"^#[0-9a-fA-F]{6}$"
# Google Fonts a tenant portal may use. Each serves weights 400-700, which the portal loads.
BRAND_FONTS = (
    "Inter", "Manrope", "Roboto", "Open Sans", "Poppins", "Montserrat", "Nunito Sans", "Source Sans 3",
    "IBM Plex Sans", "DM Sans", "Work Sans", "Noto Sans", "Plus Jakarta Sans", "Outfit", "Figtree", "Mulish",
)


class Branding(BaseModel):
    primary_color: str | None = Field(default=None, pattern=COLOR_PATTERN)
    secondary_color: str | None = Field(default=None, pattern=COLOR_PATTERN)
    brand_font: str | None = None
    contact_email: EmailStr | None = None
    contact_phone: str | None = Field(default=None, max_length=50)
    website: str | None = Field(default=None, max_length=300)
    address: str | None = Field(default=None, max_length=2000)
    # Checked against TENANT_PORT_MIN..MAX and other organizations by the service.
    allowed_port: int | None = None

    @field_validator("brand_font")
    @classmethod
    def _known_font(cls, value: str | None) -> str | None:
        if value is not None and value not in BRAND_FONTS:
            raise ValueError("Choose one of the listed Google Fonts.")
        return value


class FirstAdmin(BaseModel):
    """The organisation's first admin, created with it (spec §7.2-7.3)."""

    email: EmailStr
    full_name: str = Field(min_length=2, max_length=200)
    password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=1024)


class OrganizationCreate(Branding):
    name: str = Field(min_length=2, max_length=200)
    slug: str = Field(pattern=SLUG_PATTERN)
    admin: FirstAdmin | None = None


class OrganizationUpdate(Branding):
    name: str | None = Field(default=None, min_length=2, max_length=200)
    status: OrganizationStatus | None = None


class OrganizationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    slug: str
    status: str
    created_at: datetime
    primary_color: str | None = None
    secondary_color: str | None = None
    contact_email: str | None = None
    contact_phone: str | None = None
    website: str | None = None
    address: str | None = None
    brand_font: str | None = None
    allowed_port: int | None = None
    has_logo: bool = False
    # The logo as the browser loads it (<img src>; the session cookie authorises it), or None.
    # `v` changes when the organisation is saved, so a new logo is not served from the browser cache.
    logo_url: str | None = None


class TakenPort(BaseModel):
    port: int
    organization_id: uuid.UUID
    organization_name: str


class OrganizationOptions(BaseModel):
    """What the create and edit forms offer: the port range, the ports in use, and the fonts."""

    port_min: int
    port_max: int
    taken_ports: list[TakenPort]
    fonts: list[str]


DeletableType = Literal["organization", "branch", "department"]


class HierarchyNode(BaseModel):
    """One level of what a delete reaches: its people and policies, and the levels below it."""

    type: DeletableType
    id: uuid.UUID
    name: str
    code: str | None = None
    # People at exactly this level, by role (an organization's admins, a branch's manager and
    # Users outside any department, a department's Users).
    users: dict[str, int] = Field(default_factory=dict)
    # Active policies scoped to exactly this level.
    policies: int = 0
    children: list["HierarchyNode"] = Field(default_factory=list)


class DeletionCounts(BaseModel):
    """Everything below the deleted record that goes with it."""

    branches: int = 0
    departments: int = 0
    users: int = 0
    policies: int = 0  # archived, not deleted


class DeletionPreview(BaseModel):
    """What the confirmation dialog shows: "Delete all" sends the delete with confirm=true."""

    resource_type: DeletableType
    id: uuid.UUID
    name: str
    message: str
    counts: DeletionCounts
    hierarchy: HierarchyNode


class DeletionResult(BaseModel):
    resource_type: DeletableType
    id: uuid.UUID
    message: str
    counts: DeletionCounts


def organization_read(organization) -> OrganizationRead:
    read = OrganizationRead.model_validate(organization)
    read.has_logo = organization.logo_key is not None
    if read.has_logo:
        prefix = get_settings().API_PUBLIC_PREFIX.rstrip("/")
        version = int(organization.updated_at.timestamp())
        read.logo_url = f"{prefix}/organizations/{organization.id}/logo?v={version}"
    return read
