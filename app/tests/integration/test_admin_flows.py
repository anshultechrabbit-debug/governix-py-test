import pytest

from app.modules.auth.permissions import Role
from app.tests.factories import login, make_user, session_headers

pytestmark = pytest.mark.integration

ADDRESS = {"street_address": "1 MG Road", "state": "Maharashtra", "postal_code": "400001"}


def test_full_onboarding_flow(client, db):
    master = make_user(db, Role.MASTER_ADMIN)
    db.commit()
    platform = login(client, master)

    org = platform.post("/organizations", json={"name": "Axis Demo Bank", "slug": "axis-demo"})
    assert org.status_code == 201, org.text
    org_id = org.json()["data"]["id"]
    assert platform.post("/organizations", json={"name": "Dup", "slug": "axis-demo"}).json()[
        "error"]["reason"] == "CONFLICT"

    platform.post("/users", json={
        "email": "admin@axis.example", "full_name": "Org Admin",
        "password": "long-enough-password", "role": "org_admin", "organization_id": org_id,
    })
    admin_login = client.post("/auth/login", json={
        "email": "admin@axis.example", "password": "long-enough-password",
    })
    headers = session_headers(admin_login.cookies)

    branch = client.post("/branches", headers=headers, json={"name": "Mumbai HO", "code": "MUM", **ADDRESS})
    assert branch.status_code == 201
    branch_id = branch.json()["data"]["id"]
    assert client.post("/branches", headers=headers, json={"name": "Dup", "code": "MUM", **ADDRESS}).status_code == 409
    # A new branch needs its address.
    assert client.post("/branches", headers=headers, json={"name": "Pune", "code": "PUN"}).status_code == 422

    dept = client.post("/departments", headers=headers, json={
        "branch_id": branch_id, "name": "Credit", "code": "CRD",
    })
    assert dept.status_code == 201

    manager = client.post("/users", headers=headers, json={
        "email": "manager@axis.example", "full_name": "Branch Manager",
        "password": "long-enough-password", "role": "branch_manager", "branch_id": branch_id,
    })
    assert manager.status_code == 201, manager.text
    assert client.post("/users", headers=headers, json={
        "email": "MANAGER@axis.example", "full_name": "Dup", "password": "long-enough-password",
        "role": "branch_manager", "branch_id": branch_id,
    }).json()["error"]["reason"] == "EMAIL_TAKEN"

    actions = {e["action"] for e in client.get("/audit-events", headers=headers).json()["data"]["items"]}
    assert {"branch.created", "department.created", "user.created"} <= actions


def test_org_admin_moves_branch_managers_within_its_organization(client, db):
    from app.tests.factories import make_tenant

    tenant = make_tenant(db)
    other = make_tenant(db)
    admin = login(client, tenant.admin)
    response = admin.patch(f"/users/{tenant.manager_a.id}", json={"branch_id": str(tenant.branch_b.id)})
    assert response.status_code == 200, response.text
    assert response.json()["data"]["branch_id"] == str(tenant.branch_b.id)
    # Another organization's branch is rejected.
    assert admin.patch(f"/users/{tenant.manager_a.id}", json={
        "branch_id": str(other.branch_a.id),
    }).status_code == 422
    # Users are their Branch Manager's, and Organization Admins the Master Admin's.
    assert admin.patch(f"/users/{tenant.user_a1.id}", json={"role": "branch_manager"}).status_code == 404
    assert admin.patch(f"/users/{tenant.admin.id}", json={"is_active": False}).status_code == 404
