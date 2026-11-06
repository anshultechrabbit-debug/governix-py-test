"""Important actions need a fresh authenticator code, every time (auth/step_up.py)."""

import time

import pyotp
import pytest
from sqlalchemy import select

from app.modules.audit.model import AuditEvent
from app.modules.users.model import User
from app.tests.factories import AuthClient, login, make_tenant

CODE = "X-Authenticator-Code"


@pytest.fixture
def settings(settings):
    return settings.model_copy(update={"STEP_UP_ENABLED": True})


@pytest.fixture
def tenant(db):
    return make_tenant(db)


def code_at(secret: str, offset_steps: int = 0) -> str:
    return pyotp.TOTP(secret).at(time.time() + 30 * offset_steps)


def with_authenticator(session: AuthClient) -> str:
    secret = session.post("/auth/mfa/setup").json()["data"]["secret"]
    assert session.post("/auth/mfa/enable", json={"code": code_at(secret)}).status_code == 200
    return secret


def reason(response) -> str:
    return response.json()["error"]["reason"]


def test_without_an_authenticator_important_actions_are_refused(client, db, tenant):
    admin = login(client, tenant.admin)
    refused = admin.delete(f"/users/{tenant.user_b1.id}")
    assert refused.status_code == 403 and reason(refused) == "STEP_UP_SETUP_REQUIRED"
    db.expire_all()
    assert db.get(User, tenant.user_b1.id).deleted_at is None
    # Routine changes are not important actions.
    assert admin.patch(f"/users/{tenant.user_b1.id}", json={"full_name": "New Name"}).status_code == 200
    # Someone without the permission is refused for that, not asked for a code.
    assert reason(login(client, tenant.user_a1).delete(f"/users/{tenant.user_b1.id}")) == "PERMISSION_DENIED"


def test_each_important_action_needs_its_own_fresh_code(client, db, tenant):
    admin = login(client, tenant.admin)
    secret = with_authenticator(admin)  # spends the current step
    assert reason(admin.delete(f"/users/{tenant.user_b1.id}")) == "STEP_UP_REQUIRED"
    assert reason(admin.delete(f"/users/{tenant.user_b1.id}", headers={CODE: "000000"})) == "STEP_UP_INVALID"
    assert reason(admin.delete(f"/users/{tenant.user_b1.id}", headers={CODE: code_at(secret)})) == "STEP_UP_CODE_USED"
    db.expire_all()
    assert db.get(User, tenant.user_b1.id).deleted_at is None
    failures = db.scalars(select(AuditEvent).where(AuditEvent.action == "auth.step_up_failed")).all()
    assert [(e.details["action"], e.details["reused"]) for e in failures] == [("user.delete", False), ("user.delete", True)]

    fresh = code_at(secret, 1)
    assert admin.delete(f"/users/{tenant.user_b1.id}", headers={CODE: fresh}).status_code == 200
    # That code approved one action: the next one waits for another code.
    changed = admin.patch(f"/users/{tenant.user_a2.id}", json={"is_active": False}, headers={CODE: fresh})
    assert reason(changed) == "STEP_UP_CODE_USED"
    db.expire_all()
    assert db.get(User, tenant.user_a2.id).is_active is True


def test_a_deletion_preview_needs_no_code_but_the_deletion_does(client, db, tenant):
    admin = login(client, tenant.admin)
    secret = with_authenticator(admin)
    preview = admin.delete(f"/branches/{tenant.branch_b.id}")
    assert preview.status_code == 409 and reason(preview) == "DELETE_CONFIRMATION_REQUIRED"
    assert reason(admin.delete(f"/branches/{tenant.branch_b.id}?confirm=true")) == "STEP_UP_REQUIRED"
    deleted = admin.delete(f"/branches/{tenant.branch_b.id}?confirm=true", headers={CODE: code_at(secret, 1)})
    assert deleted.status_code == 200, deleted.text
