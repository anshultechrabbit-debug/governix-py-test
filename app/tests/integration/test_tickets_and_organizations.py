"""Complaints and support tickets by level; organisation onboarding and branding."""

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


def raise_ticket(session, **body):
    response = session.post("/tickets", json={"subject": "Branch service issue", "description": "Queue was not managed.", **body})
    assert response.status_code == 201, response.text
    return response.json()["data"]


def test_user_complaint_reaches_only_their_branch_manager(client, tenant):
    user = login(client, tenant.user_a1)
    ticket = raise_ticket(user, kind="complaint", category="Service")
    assert (ticket["level"], ticket["status"], ticket["kind"]) == ("branch", "open", "complaint")

    manager_a, manager_b = login(client, tenant.manager_a), login(client, tenant.manager_b)
    queue = manager_a.get("/tickets?box=queue").json()["data"]["items"]
    assert [t["id"] for t in queue] == [ticket["id"]] and queue[0]["can_handle"] is True
    assert manager_a.get("/notifications").json()["data"]["items"][0]["type"] == "TICKET_UPDATED"
    assert manager_b.get(f"/tickets/{ticket['id']}").status_code == 404
    assert login(client, tenant.admin).get(f"/tickets/{ticket['id']}").status_code == 404  # organisation level only
    assert login(client, tenant.user_a2).get(f"/tickets/{ticket['id']}").status_code == 404

    # The manager's reply moves it along and notifies the user as a complaint response.
    replied = manager_a.post(f"/tickets/{ticket['id']}/messages", json={"body": "We have added a second counter."})
    assert replied.json()["data"]["status"] == "in_progress"
    assert user.get("/notifications").json()["data"]["items"][0]["type"] == "COMPLAINT_RESPONSE"
    resolved = manager_a.post(f"/tickets/{ticket['id']}/status", json={"status": "resolved", "note": "Fixed"})
    assert resolved.json()["data"]["status"] == "resolved" and resolved.json()["data"]["resolved_at"]

    # The user may accept (close) or reopen, but not e.g. set it in progress.
    assert user.post(f"/tickets/{ticket['id']}/status", json={"status": "in_progress"}).status_code == 403
    closed = user.post(f"/tickets/{ticket['id']}/status", json={"status": "closed"}).json()["data"]
    assert closed["status"] == "closed"
    assert user.post(f"/tickets/{ticket['id']}/messages", json={"body": "one more thing"}).status_code == 409
    history = [m["status_change"] for m in closed["messages"] if m["status_change"]]
    assert history == ["in_progress", "resolved", "closed"]


def test_support_from_anyone_goes_to_platform_support(client, tenant, master):
    raised = [raise_ticket(login(client, who), subject=f"Help needed {n}", category="Core banking software")
              for n, who in enumerate((tenant.user_a1, tenant.manager_a, tenant.admin))]
    assert {t["level"] for t in raised} == {"platform"}
    platform = login(client, master)
    queue = platform.get("/tickets?box=queue").json()["data"]["items"]
    assert {t["id"] for t in queue} == {t["id"] for t in raised}
    assert {t["organization_name"] for t in queue} == {tenant.org.name}
    assert {t["created_by_role"] for t in queue} == {"department_user", "branch_manager", "org_admin"}
    # Nobody in the organisation handles support: not the branch manager, not the admin.
    assert login(client, tenant.manager_a).get(f"/tickets/{raised[0]['id']}").status_code == 404
    assert login(client, tenant.admin).get(f"/tickets/{raised[1]['id']}").status_code == 404

    assert login(client, tenant.manager_a).post("/tickets", json={
        "kind": "complaint", "subject": "x" * 5, "description": "only users complain",
    }).status_code == 422
    assert platform.post("/tickets", json={"subject": "hello", "description": "masters do not raise"}).status_code == 403


def test_ticket_attachments(client, tenant):
    user = login(client, tenant.user_a1)
    ticket = raise_ticket(user, kind="complaint")  # handled by their branch manager
    sent = user.post(f"/tickets/{ticket['id']}/attachments",
                     files={"file": ("receipt.png", io.BytesIO(PNG), "image/png")})
    assert sent.status_code == 201, sent.text
    [attachment] = sent.json()["data"]["attachments"]
    download = login(client, tenant.manager_a).get(f"/tickets/{ticket['id']}/attachments/{attachment['id']}")
    assert download.status_code == 200 and download.content == PNG
    assert user.post(f"/tickets/{ticket['id']}/attachments",
                     files={"file": ("run.exe", io.BytesIO(b"MZ"), "application/octet-stream")}).status_code == 422
    assert login(client, tenant.manager_b).get(f"/tickets/{ticket['id']}/attachments/{attachment['id']}").status_code == 404


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


def test_complaint_about_an_assigned_policy_flows_to_resolution(client, db, tenant):
    from app.tests.factories import assign, category

    admin = login(client, tenant.admin)
    cid = str(category(db, tenant.org).id)
    vault = admin.post("/policies", json={"name": "Vault Cash Handling SOP", "category_id": cid, "scope": "global"})
    other = admin.post("/policies", json={"name": "Treasury Dealing Limits", "category_id": cid, "scope": "global"})
    assert vault.status_code == other.status_code == 201
    assign(db, tenant.user_a1, vault.json()["data"]["id"])

    user = login(client, tenant.user_a1)
    # Only a policy the User can read: not one that exists but is not assigned to them.
    refused = user.post("/tickets", json={"kind": "complaint", "subject": "Limits question",
                                          "description": "Which desk limit applies?", "policy_id": other.json()["data"]["id"]})
    assert refused.json()["error"]["reason"] == "POLICY_NOT_FOUND"
    ticket = raise_ticket(user, kind="complaint", policy_id=vault.json()["data"]["id"])
    assert ticket["policy_name"] == "Vault Cash Handling SOP" and ticket["last_reply_body"] is None

    manager = login(client, tenant.manager_a)
    assert manager.get("/tickets/stats?box=queue&kind=complaint").json()["data"]["open"] == 1
    found = manager.get("/tickets?box=queue&kind=complaint&search=vault").json()["data"]["items"]
    assert [t["id"] for t in found] == [ticket["id"]]
    manager.post(f"/tickets/{ticket['id']}/status", json={"status": "resolved", "note": "Two officers must sign."})
    mine = user.get("/tickets?box=mine&kind=complaint").json()["data"]["items"][0]
    assert mine["status"] == "resolved" and mine["last_reply_body"] == "Two officers must sign."
    assert user.get("/tickets/stats?box=mine&kind=complaint").json()["data"]["resolved"] == 1


def test_me_names_the_branch(client, tenant):
    me = login(client, tenant.user_a1).get("/auth/me").json()["data"]
    assert me["branch_name"] == tenant.branch_a.name
