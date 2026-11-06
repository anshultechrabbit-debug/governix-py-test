"""Two-step sign-in with an authenticator app: setup, the code step, backup codes, turning off, admin reset."""

import time

import pyotp
import pytest
from sqlalchemy import select

from app.modules.audit.model import AuditEvent
from app.modules.users.model import User
from app.tests.factories import PASSWORD, AuthClient, login, make_tenant


@pytest.fixture
def tenant(db):
    return make_tenant(db)


def code_at(secret: str, offset_steps: int = 0) -> str:
    return pyotp.TOTP(secret).at(time.time() + 30 * offset_steps)


def turn_on(session: AuthClient, used: list[str] | None = None) -> tuple[str, list[str]]:
    setup = session.post("/auth/mfa/setup").json()["data"]
    assert setup["otpauth_uri"].startswith("otpauth://totp/") and "issuer=Governix" in setup["otpauth_uri"]
    code = code_at(setup["secret"])
    if used is not None:
        used.append(code)
    response = session.post("/auth/mfa/enable", json={"code": code})
    assert response.status_code == 200, response.text
    return setup["secret"], response.json()["data"]["codes"]


def password_step(client, user: User):
    response = client.post("/auth/login", json={"email": user.email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return response


def code_step(client, first, code: str):
    client.cookies.clear()
    return client.post("/auth/mfa/verify", json={"code": code}, headers={"Cookie": f"governix_mfa={first.cookies['governix_mfa']}"})


def test_setup_then_sign_in_needs_the_code(client, db, tenant):
    user = login(client, tenant.user_a1)
    # A wrong first code does not turn it on; sign-in is unchanged until a code is confirmed.
    user.post("/auth/mfa/setup")
    assert user.post("/auth/mfa/enable", json={"code": "000000"}).status_code == 422
    assert "governix_access" in password_step(client, tenant.user_a1).cookies

    used: list[str] = []
    secret, codes = turn_on(user, used)
    assert len(codes) == 10 and len(set(codes)) == 10
    assert user.get("/auth/me").json()["data"]["mfa_enabled"] is True
    assert user.get("/auth/mfa").json()["data"]["recovery_codes_left"] == 10
    assert user.post("/auth/mfa/setup").status_code == 409  # already on

    first = password_step(client, tenant.user_a1)
    assert first.json()["data"]["mfa_required"] is True
    assert "governix_access" not in first.cookies and "governix_mfa" in first.cookies
    # The code already used to turn it on is not accepted again; the next one is.
    assert code_step(client, first, used[0]).json()["error"]["reason"] == "INVALID_MFA_CODE"
    signed_in = code_step(client, first, code_at(secret, 1))
    assert signed_in.status_code == 200 and "governix_access" in signed_in.cookies
    # Nor is that one accepted twice.
    assert code_step(client, password_step(client, tenant.user_a1), code_at(secret, 1)).status_code == 401
    session = AuthClient(client, tenant.user_a1, dict(signed_in.cookies))
    assert session.get("/auth/me").status_code == 200
    # Without the password step there is nothing to verify.
    client.cookies.clear()
    assert client.post("/auth/mfa/verify", json={"code": code_at(secret)}).json()["error"]["reason"] == "MFA_CHALLENGE_EXPIRED"


def test_a_backup_code_works_once(client, db, tenant):
    user = login(client, tenant.user_a1)
    _, codes = turn_on(user)
    assert code_step(client, password_step(client, tenant.user_a1), codes[0].upper()).status_code == 200
    assert code_step(client, password_step(client, tenant.user_a1), codes[0]).status_code == 401
    db.expire_all()
    used = db.scalars(select(AuditEvent).where(AuditEvent.action == "auth.mfa_recovery_used")).all()
    assert [e.details["left"] for e in used] == [9]


@pytest.mark.skip(reason="LOCKOUT DISABLED for now (auth/service.py)")
def test_wrong_codes_lock_the_account(client, db, tenant):
    user = login(client, tenant.user_a1)
    turn_on(user)
    for _ in range(5):
        assert code_step(client, password_step(client, tenant.user_a1), "123456").status_code == 401
    db.expire_all()
    assert db.get(User, tenant.user_a1.id).locked_until is not None
    # Even the right code waits for the lockout to end.
    locked = client.post("/auth/login", json={"email": tenant.user_a1.email, "password": PASSWORD})
    assert locked.json()["error"]["reason"] == "ACCOUNT_LOCKED"


def test_turning_it_off_needs_the_password_and_a_code(client, db, tenant):
    user = login(client, tenant.user_a1)
    secret, codes = turn_on(user)
    # A backup code signs in, but never removes the protection.
    assert user.post("/auth/mfa/disable", json={"password": PASSWORD, "code": codes[0]}).status_code == 422
    assert user.post("/auth/mfa/disable", json={"password": "wrong-password", "code": code_at(secret, 1)}).status_code == 422
    assert user.post("/auth/mfa/disable", json={"password": PASSWORD, "code": "000000"}).status_code == 422
    assert user.post("/auth/mfa/disable", json={"password": PASSWORD, "code": code_at(secret, 1)}).status_code == 200
    assert user.get("/auth/me").json()["data"]["mfa_enabled"] is False
    assert "governix_access" in password_step(client, tenant.user_a1).cookies


def test_new_backup_codes_replace_the_old_ones(client, db, tenant):
    user = login(client, tenant.user_a1)
    secret, old = turn_on(user)
    new = user.post("/auth/mfa/recovery-codes", json={"code": code_at(secret, 1)}).json()["data"]["codes"]
    assert set(new).isdisjoint(old)
    assert code_step(client, password_step(client, tenant.user_a1), old[0]).status_code == 401
    assert code_step(client, password_step(client, tenant.user_a1), new[0]).status_code == 200


def test_an_admin_resets_it_for_a_lost_phone(client, db, tenant):
    user = login(client, tenant.user_a1)
    turn_on(user)
    # Only someone who manages that user: not another branch's manager, not the user themselves.
    assert login(client, tenant.manager_b).post(f"/users/{tenant.user_a1.id}/reset-mfa").status_code == 404
    manager = login(client, tenant.manager_a)
    listed = manager.get(f"/users/{tenant.user_a1.id}").json()["data"]
    assert listed["mfa_enabled"] is True
    reset = manager.post(f"/users/{tenant.user_a1.id}/reset-mfa")
    assert reset.status_code == 200 and reset.json()["data"]["mfa_enabled"] is False
    # Signed out everywhere; the password alone signs in again.
    assert user.get("/auth/me").status_code == 401
    assert "governix_access" in password_step(client, tenant.user_a1).cookies
    assert manager.post(f"/users/{tenant.user_a1.id}/reset-mfa").status_code == 422  # nothing left to reset
    db.expire_all()
    assert db.scalar(select(AuditEvent.id).where(AuditEvent.action == "user.mfa_reset")) is not None


def test_the_person_and_their_managers_are_alerted(client, db, tenant):
    from app.modules.notifications.model import Notification

    user = login(client, tenant.user_a1)
    secret, codes = turn_on(user)
    code_step(client, password_step(client, tenant.user_a1), codes[0])
    db.expire_all()
    alerts = db.scalars(select(Notification).where(Notification.type == "SECURITY_ALERT")).all()
    told = {(n.user_id, n.data["event"]) for n in alerts}
    for event in ("enabled", "recovery_used"):
        # The user, their Branch Manager and the Org Admin; not another branch's manager.
        assert {(tenant.user_a1.id, event), (tenant.manager_a.id, event), (tenant.admin.id, event)} <= told
        assert (tenant.manager_b.id, event) not in told
