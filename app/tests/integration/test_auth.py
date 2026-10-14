from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update

from app.core.security import hash_token
from app.modules.audit.model import AuditEvent
from app.modules.auth.cookies import ACCESS_COOKIE, CSRF_COOKIE, CSRF_HEADER, REFRESH_COOKIE
from app.modules.auth.model import RefreshToken
from app.modules.auth.permissions import Role
from app.modules.organizations.model import OrganizationStatus
from app.tests.factories import PASSWORD, login, make_org, make_tenant, make_user, session_headers

pytestmark = pytest.mark.integration

ADDRESS = {"street_address": "1 MG Road", "state": "Maharashtra", "postal_code": "400001"}


def test_login_me_and_permissions(client, db):
    tenant = make_tenant(db)
    session = login(client, tenant.user_a1)
    me = session.get("/auth/me").json()["data"]
    assert me["email"] == tenant.user_a1.email
    assert me["organization_id"] == str(tenant.org.id)
    assert "ai:query" in me["permissions"]
    assert "users:manage" not in me["permissions"]


def test_login_is_case_insensitive_on_email(client, db):
    tenant = make_tenant(db)
    response = client.post(
        "/auth/login", json={"email": tenant.admin.email.upper(), "password": PASSWORD}
    )
    assert response.status_code == 200


@pytest.mark.parametrize("email", ["nobody@example.com", None])
def test_bad_credentials_give_identical_error(client, db, email):
    tenant = make_tenant(db)
    response = client.post(
        "/auth/login", json={"email": email or tenant.admin.email, "password": "wrong-password"}
    )
    assert response.status_code == 401
    assert response.json()["error"] == {
        "code": 401, "reason": "INVALID_CREDENTIALS", "message": "Invalid email or password.",
    }


def test_lockout_after_repeated_failures(client, db, settings):
    tenant = make_tenant(db)
    for _ in range(settings.LOGIN_MAX_FAILED_ATTEMPTS):
        client.post("/auth/login", json={"email": tenant.admin.email, "password": "nope"})
    response = client.post("/auth/login", json={"email": tenant.admin.email, "password": PASSWORD})
    assert response.json()["error"]["reason"] == "ACCOUNT_LOCKED"

    db.refresh(tenant.admin)
    tenant.admin.locked_until = datetime.now(UTC) - timedelta(seconds=1)
    db.commit()
    assert client.post(
        "/auth/login", json={"email": tenant.admin.email, "password": PASSWORD}
    ).status_code == 200


def test_missing_and_malformed_tokens(client):
    assert client.get("/auth/me").json()["error"]["reason"] == "UNAUTHENTICATED"
    response = client.get("/auth/me", headers={"Cookie": f"{ACCESS_COOKIE}=not.a.jwt"})
    assert response.status_code == 401
    assert response.json()["error"]["reason"] == "INVALID_TOKEN"


def test_tokens_travel_only_in_cookies(client, db):
    tenant = make_tenant(db)
    response = client.post("/auth/login", json={"email": tenant.admin.email, "password": PASSWORD})
    assert set(response.json()["data"]) == {"expires_in"}
    set_cookies = {c.split("=", 1)[0]: c.lower() for c in response.headers.get_list("set-cookie")}
    assert set(set_cookies) == {ACCESS_COOKIE, REFRESH_COOKIE, CSRF_COOKIE}
    assert all("samesite=strict" in c for c in set_cookies.values())
    assert "httponly" in set_cookies[ACCESS_COOKIE] and "httponly" in set_cookies[REFRESH_COOKIE]
    assert "httponly" not in set_cookies[CSRF_COOKIE]  # the page must read it to echo it
    # A bearer header is no longer a way in.
    access = response.cookies[ACCESS_COOKIE]
    client.cookies.clear()
    assert client.get("/auth/me", headers={"Authorization": f"Bearer {access}"}).status_code == 401


def test_unsafe_requests_need_their_sessions_csrf_header(client, db):
    tenant = make_tenant(db)
    other = login(client, tenant.manager_a)
    admin = login(client, tenant.admin)
    cookie_only = {"Cookie": session_headers(admin.cookies)["Cookie"]}
    assert client.get("/auth/me", headers=cookie_only).status_code == 200  # reads need no header
    for headers in (cookie_only, {**cookie_only, CSRF_HEADER: other.cookies[CSRF_COOKIE]}):
        response = client.post("/branches", headers=headers, json={"name": "X", "code": "X", **ADDRESS})
        assert response.status_code == 403 and response.json()["error"]["reason"] == "CSRF_FAILED"
    assert admin.post("/branches", json={"name": "X", "code": "X", **ADDRESS}).status_code == 201


def test_refresh_rotates_and_detects_reuse(client, db):
    tenant = make_tenant(db)
    client.post("/auth/login", json={"email": tenant.admin.email, "password": PASSWORD})
    old = dict(client.cookies)

    rotated = client.post("/auth/refresh", headers={CSRF_HEADER: old[CSRF_COOKIE]})
    assert rotated.status_code == 200 and set(rotated.json()["data"]) == {"expires_in"}
    new = dict(client.cookies)
    assert new[REFRESH_COOKIE] != old[REFRESH_COOKIE] and new[CSRF_COOKIE] != old[CSRF_COOKIE]
    client.cookies.clear()

    # Moments later the old token is another tab losing the race: refused, but the session lives on.
    race = client.post("/auth/refresh", headers=session_headers(old))
    assert race.status_code == 401 and race.json()["error"]["reason"] == "TOKEN_ROTATED"
    assert "set-cookie" not in race.headers  # keeps the cookies the other tab just received

    # Later, replaying the old token is treated as theft: the whole family is revoked.
    db.execute(update(RefreshToken).where(RefreshToken.token_hash == hash_token(old[REFRESH_COOKIE]))
               .values(revoked_at=datetime.now(UTC) - timedelta(minutes=1)))
    db.commit()
    reuse = client.post("/auth/refresh", headers=session_headers(old))
    assert reuse.status_code == 401 and reuse.json()["error"]["reason"] == "INVALID_TOKEN"
    assert client.post("/auth/refresh", headers=session_headers(new)).status_code == 401
    assert db.scalar(select(AuditEvent).where(AuditEvent.action == "auth.refresh_token_reuse"))


def test_logout_revokes_the_session_and_clears_cookies(client, db):
    tenant = make_tenant(db)
    session = login(client, tenant.admin)
    live = select(RefreshToken).where(RefreshToken.user_id == tenant.admin.id, RefreshToken.revoked_at.is_(None))

    # Without the CSRF header (another site posting the form) the cookies go but the session stays.
    client.post("/auth/logout", headers={"Cookie": session_headers(session.cookies)["Cookie"]})
    assert db.scalar(live) is not None

    response = session.post("/auth/logout")
    assert response.status_code == 200
    assert all("max-age=0" in c.lower() for c in response.headers.get_list("set-cookie"))
    assert db.scalar(live) is None
    assert client.post("/auth/refresh", headers=session_headers(session.cookies)).status_code == 401


def test_deactivation_invalidates_existing_access_tokens_immediately(client, db):
    tenant = make_tenant(db)
    user_session = login(client, tenant.user_a1)
    manager = login(client, tenant.manager_a)  # a User's Branch Manager manages them
    assert manager.patch(f"/users/{tenant.user_a1.id}", json={"is_active": False}).status_code == 200
    assert user_session.get("/auth/me").status_code == 401


def test_suspended_organization_blocks_its_users(client, db):
    tenant = make_tenant(db)
    master = make_user(db, Role.MASTER_ADMIN)
    db.commit()
    user_session = login(client, tenant.user_a1)
    response = login(client, master).patch(
        f"/organizations/{tenant.org.id}", json={"status": OrganizationStatus.SUSPENDED}
    )
    assert response.status_code == 200
    assert user_session.get("/auth/me").json()["error"]["reason"] == "ORGANIZATION_SUSPENDED"
    assert client.post(
        "/auth/login", json={"email": tenant.user_a1.email, "password": PASSWORD}
    ).status_code == 401


def test_change_password_ends_all_sessions(client, db):
    tenant = make_tenant(db)
    session = login(client, tenant.user_a1)
    new_password = "another-long-password-42"
    assert session.post(
        "/auth/change-password",
        json={"current_password": PASSWORD, "new_password": new_password},
    ).status_code == 200
    assert session.get("/auth/me").status_code == 401
    assert client.post(
        "/auth/login", json={"email": tenant.user_a1.email, "password": new_password}
    ).status_code == 200


def test_master_admin_has_no_tenant_data_access(client, db):
    make_org(db)
    master = make_user(db, Role.MASTER_ADMIN)
    db.commit()
    session = login(client, master)
    assert session.get("/branches").status_code == 403
    assert session.get("/audit-events").status_code == 403
