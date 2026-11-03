"""Organisation onboarding and branding. (Support Center tickets: test_support_center.py.)"""

import io

import pytest

from app.modules.auth.permissions import Role
from app.tests.factories import login, make_tenant, make_user, session_headers

pytestmark = pytest.mark.integration

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


@pytest.fixture
def tenant(db):
    return make_tenant(db)


@pytest.fixture
def master(db):
    user = make_user(db, Role.MASTER_ADMIN)
    db.commit()
    return user


def test_organisation_is_created_with_branding_and_its_first_admin(client, db, master):
    platform = login(client, master)
    created = platform.post("/organizations", json={
        "name": "ABC Bank", "slug": "abc-bank", "primary_color": "#1e3a8a", "contact_email": "it@abcbank.com",
        "admin": {"email": "admin@abcbank.com", "full_name": "ABC Admin", "password": "a-very-long-password-1"},
    })
    assert created.status_code == 201, created.text
    org = created.json()["data"]
    assert (org["primary_color"], org["contact_email"], org["has_logo"]) == ("#1e3a8a", "it@abcbank.com", False)
    assert platform.post(f"/organizations/{org['id']}/logo",
                         files={"file": ("logo.png", io.BytesIO(PNG), "image/png")}).json()["data"]["has_logo"] is True

    session = client.post("/auth/login", json={"email": "admin@abcbank.com", "password": "a-very-long-password-1"})
    assert session.status_code == 200, session.text
    headers = session_headers(session.cookies)
    mine = client.get("/organizations/me", headers=headers).json()["data"]
    assert mine["name"] == "ABC Bank" and mine["has_logo"] is True
    assert client.get(f"/organizations/{org['id']}/logo", headers=headers).content == PNG

    duplicate_admin = platform.post("/organizations", json={
        "name": "XYZ Bank", "slug": "xyz-bank",
        "admin": {"email": "admin@abcbank.com", "full_name": "Dup", "password": "a-very-long-password-1"},
    })
    assert duplicate_admin.status_code == 422
    assert platform.get("/organizations").json()["data"]["total"] == 1  # nothing half-created
    assert platform.patch(f"/organizations/{org['id']}", json={"primary_color": "blue"}).status_code == 422


def test_organisation_port_is_in_range_and_unique_and_font_is_listed(client, master):
    platform = login(client, master)
    body = {"name": "Port Bank", "slug": "port-bank", "allowed_port": 7007, "brand_font": "Manrope"}
    created = platform.post("/organizations", json=body)
    assert created.status_code == 201, created.text
    assert (created.json()["data"]["allowed_port"], created.json()["data"]["brand_font"]) == (7007, "Manrope")

    taken = platform.post("/organizations", json={**body, "name": "Other Bank", "slug": "other-bank"})
    assert taken.status_code == 409 and taken.json()["error"]["reason"] == "PORT_TAKEN"
    outside = platform.post("/organizations", json={**body, "slug": "other-bank", "allowed_port": 8080})
    assert outside.status_code == 422 and outside.json()["error"]["reason"] == "PORT_OUT_OF_RANGE"
    assert platform.post("/organizations", json={**body, "slug": "other-bank", "allowed_port": 7008,
                                                 "brand_font": "Comic Sans"}).status_code == 422

    options = platform.get("/organizations/options").json()["data"]
    assert (options["port_min"], options["port_max"]) == (7001, 7100) and "Manrope" in options["fonts"]
    assert [(t["port"], t["organization_name"]) for t in options["taken_ports"]] == [(7007, "Port Bank")]

    # Keeping its own port is not a conflict; another organization's is.
    org_id = created.json()["data"]["id"]
    assert platform.patch(f"/organizations/{org_id}", json={"allowed_port": 7007}).status_code == 200
    other = platform.post("/organizations", json={**body, "slug": "other-bank", "allowed_port": 7008}).json()["data"]
    assert platform.patch(f"/organizations/{other['id']}", json={"allowed_port": 7007}).json()["error"]["reason"] == "PORT_TAKEN"


def test_other_organisations_cannot_read_a_logo(client, db, tenant, master):
    other = make_tenant(db)
    platform = login(client, master)
    platform.post(f"/organizations/{other.org.id}/logo", files={"file": ("logo.png", io.BytesIO(PNG), "image/png")})
    assert login(client, tenant.admin).get(f"/organizations/{other.org.id}/logo").status_code == 404


def test_branch_manager_creates_users_without_a_department(client, tenant):
    response = login(client, tenant.manager_a).post("/users", json={
        "email": "rahul@example.com", "full_name": "Rahul", "password": "a-very-long-password-1",
        "role": "department_user",
    })
    assert response.status_code == 201, response.text
    user = response.json()["data"]
    assert user["branch_id"] == str(tenant.branch_a.id) and user["department_id"] is None


def test_me_names_the_branch(client, tenant):
    me = login(client, tenant.user_a1).get("/auth/me").json()["data"]
    assert me["branch_name"] == tenant.branch_a.name
