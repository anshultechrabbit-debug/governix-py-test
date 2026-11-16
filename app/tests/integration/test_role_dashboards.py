"""The role dashboards: /dashboard/platform (Master Admin), /organization (Org Admin), /branch (Branch Manager)."""

import uuid

import pytest

from app.modules.audit.model import AuditEvent
from app.modules.auth.permissions import Role
from app.modules.documents.model import Document, DocumentStatus
from app.modules.policies.model import Policy
from app.tests.factories import assign, category, login, make_tenant, make_user

pytestmark = pytest.mark.integration


def make_policy(db, tenant, name: str) -> tuple[Policy, Document]:
    """An organization-wide active policy with one ready document (no pipeline: the dashboards only count)."""
    policies = category(db, tenant.org)
    policy = Policy(organization_id=tenant.org.id, category_id=policies.id, name=name, normalized_name=name.lower())
    db.add(policy)
    db.flush()
    document = Document(
        organization_id=tenant.org.id, policy_id=policy.id, category_id=policies.id, title=name,
        original_filename=f"{name.lower().replace(' ', '_')}.pdf", storage_key=f"test/{uuid.uuid4()}",
        content_type="application/pdf", size_bytes=2048, file_sha256=uuid.uuid4().hex * 2,
        status=DocumentStatus.READY,
    )
    db.add(document)
    db.commit()
    return policy, document


def opened(db, user, document) -> None:
    db.add(AuditEvent(organization_id=user.organization_id, actor_user_id=user.id, action="document.accessed",
                      resource_type="document", resource_id=document.id, details={}))
    db.commit()


@pytest.fixture
def tenant(db):
    return make_tenant(db)


@pytest.fixture
def reached(db, tenant):
    """One policy assigned to three Users: one opened it after, one before the assignment, one never."""
    policy, document = make_policy(db, tenant, "Vault Cash Handling SOP")
    opened(db, tenant.user_b1, document)  # before it was assigned: does not count
    assign(db, tenant.user_a1, policy.id)
    assign(db, tenant.user_a2, policy.id)
    assign(db, tenant.user_b1, policy.id)
    opened(db, tenant.user_a1, document)
    return policy, document


def test_each_dashboard_is_for_its_role(client, db, tenant):
    master = make_user(db, Role.MASTER_ADMIN)
    db.commit()
    admin, manager, user = login(client, tenant.admin), login(client, tenant.manager_a), login(client, tenant.user_a1)
    assert login(client, master).get("/dashboard/platform").status_code == 200
    assert admin.get("/dashboard/organization").status_code == 200
    assert manager.get("/dashboard/branch").status_code == 200
    for who, path in ((admin, "/dashboard/platform"), (admin, "/dashboard/branch"),
                      (manager, "/dashboard/organization"), (user, "/dashboard/branch")):
        response = who.get(path)
        assert response.status_code == 403 and response.json()["error"]["reason"] == "WRONG_DASHBOARD"
    assert admin.get("/dashboard/organization?period=1y").status_code == 422


def test_read_means_opened_since_assigned(client, tenant, reached):
    policy, _ = reached
    data = login(client, tenant.admin).get("/dashboard/organization").json()["data"]
    top = data["top_policies"][0]
    assert top["policy_id"] == str(policy.id)
    assert (top["assigned"], top["read"], top["pending"], top["read_percent"], top["branches"]) == (3, 1, 2, 33, 2)


def test_organization_dashboard(client, db, tenant, reached):
    other = make_tenant(db)
    opened(db, other.user_a1, make_policy(db, other, "Elsewhere")[1])
    data = login(client, tenant.admin).get("/dashboard/organization?rows=2").json()["data"]
    assert data["users"]["total"] == 6 and data["users"]["by_role"]["department_user"] == 3
    assert data["branches"] == {"total": 2, "active": 2, "with_manager": 2}
    assert data["documents"]["total"] == 1 and data["documents"]["ready"] == 1
    assert {b["name"]: (b["users"], b["managers"], b["status"]) for b in data["branch_users"]} == {
        "Branch A": (2, 1, "active"), "Branch B": (1, 1, "active"),
    }
    assert len(data["latest_branches"]) == 2 and data["latest_branches"][0]["manager_name"]
    assert data["recent_documents"][0]["extension"] == "PDF"
    today = data["document_activity"][-1]
    assert len(data["document_activity"]) == 7 and (today["uploads"], today["assignments"]) == (1, 3)
    assert all(event["organization_id"] == str(tenant.org.id) for event in data["activity"])
    assert data["portal"] == {"port": None, "state": None}
    admin = login(client, tenant.admin)
    assert len(admin.get("/dashboard/organization?period=30d").json()["data"]["document_activity"]) == 4
    assert len(admin.get("/dashboard/organization?period=90d").json()["data"]["document_activity"]) == 3


def test_branch_dashboard_counts_only_its_branch(client, tenant, reached):
    data = login(client, tenant.manager_a).get("/dashboard/branch").json()["data"]
    assert data["branch_name"] == "Branch A"
    assert data["counts"] == {"documents": 1, "staff": 2, "assignments": 2, "open_tickets": 0}
    assert data["assignment_status"] == {"read": 1, "pending": 1, "unassigned_policies": 0}
    assert sorted((d["name"], d["count"], d["percent"]) for d in data["departments"]) == [
        ("Dept A1", 1, 50), ("Dept A2", 1, 50),
    ]
    assert data["top_policies"][0]["assigned"] == 2  # branch B's assignment is not counted
    assert {item["kind"] for item in data["activity"]} >= {"assignment", "document", "staff"}
    assert data["document_activity"][-1]["assignments"] == 2


def test_platform_dashboard(client, db, tenant, reached):
    master = make_user(db, Role.MASTER_ADMIN)
    db.commit()
    login(client, tenant.admin)  # a sign-in in the period
    data = login(client, master).get("/dashboard/platform").json()["data"]
    row = next(o for o in data["recent_organizations"] if o["id"] == str(tenant.org.id))
    assert (row["users"], row["branches"], row["admins"], row["storage_bytes"]) == (6, 2, 1, 2048)
    assert row["status"] == "provisioning"  # no port yet
    assert data["organizations"]["provisioning"] >= 1 and data["org_admins"]["total"] >= 1
    assert {"org_admin", "branch_manager", "department_user", "master_admin"} <= {r["role"] for r in data["roles"]}
    busy = next(a for a in data["activity_by_organization"] if a["organization_id"] == str(tenant.org.id))
    assert busy["sign_ins"] >= 1 and busy["documents_opened"] == 2
    assert data["support"]["counts"]["total"] == 0 and data["audit"]
