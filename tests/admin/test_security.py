from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from app.admin.config import AdminConfig
from app.admin.password import hash_password, valid_hash, verify_password
from app.admin.security import SESSION_COOKIE, credential_fingerprint
from app.admin.store import digest
from tests.admin.helpers import PASSWORD, csrf, login
from vinted_deal_hunter.api import create_app
from vinted_deal_hunter.config import Settings


@pytest.mark.parametrize("path", ["", "/offers", "/deals", "/research", "/settings", "/system"])
def test_anonymous_pages_redirect(client, path):
    response = client.get("/admin" + path, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/login"
    assert response.headers["HX-Redirect"] == "/admin/login"


@pytest.mark.parametrize(
    "path", ["/settings", "/logout", "/jobs/run", "/jobs/pause", "/jobs/resume"]
)
def test_anonymous_mutations_redirect(client, path):
    assert (
        client.post("/admin" + path, data={"csrf": "forged"}, follow_redirects=False).status_code
        == 303
    )


def test_missing_password_fails_closed(settings):
    with TestClient(
        create_app(settings.model_copy(update={"admin_password_hash": None}))
    ) as client:
        assert client.get("/admin/login").status_code == 503
        assert client.get("/admin").status_code == 503


def test_password_hash_and_environment(password_hash, monkeypatch):
    assert valid_hash(password_hash)
    assert verify_password(PASSWORD, password_hash)
    assert not verify_password("incorrect", password_hash)
    assert not valid_hash("plaintext")
    with pytest.raises(ValueError):
        hash_password("short")
    with pytest.raises(ValidationError):
        Settings(admin_password_hash=SecretStr("plaintext"))
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", password_hash)
    monkeypatch.setenv("VDH_ADMIN", '{"username":"owner","secure_cookies":true}')
    settings = Settings.from_env()
    assert settings.admin.username == "owner" and settings.admin.secure_cookies
    assert password_hash not in settings.model_dump_json() and password_hash not in repr(settings)


def test_login_csrf_wrong_password_and_throttling(client):
    token = csrf(client.get("/admin/login"))
    payload = {"username": "admin", "password": "bad", "csrf": token}
    assert client.post("/admin/login", data={**payload, "csrf": "wrong"}).status_code == 403
    for _ in range(5):
        response = client.post("/admin/login", data=payload)
        assert response.status_code == 401 and SESSION_COOKIE not in client.cookies
    response = client.post("/admin/login", data=payload)
    assert response.status_code == 429 and response.headers["retry-after"]


def test_csrf_all_forms_and_origin(signed_in, seeded):
    from app.admin.queries import offer_key

    offer = seeded.request.snapshot.offer
    key = offer_key(offer.source_name, offer.external_id)
    token = csrf(signed_in.get("/admin"))
    paths = [
        "/settings",
        "/logout",
        "/jobs/run",
        "/jobs/pause",
        "/jobs/resume",
        f"/offers/{key}/feedback",
    ]
    for path in paths:
        assert signed_in.post("/admin" + path, data={"label": "ignored"}).status_code == 403
        assert signed_in.post("/admin" + path, data={"csrf": "forged"}).status_code == 403
        assert (
            signed_in.post(
                "/admin" + path, data={"csrf": token}, headers={"Origin": "https://evil.example"}
            ).status_code
            == 403
        )
    assert (
        signed_in.post(
            "/admin/jobs/pause", data={"csrf": token}, headers={"Sec-Fetch-Site": "cross-site"}
        ).status_code
        == 403
    )
    assert (
        signed_in.post(
            "/admin/jobs/pause", data={"csrf": token}, headers={"Origin": "http://testserver"}
        ).status_code
        == 200
    )


def test_logout_revocation_session_rotation_and_hash_at_rest(client, settings):
    token = login(client)
    old_cookie = client.cookies.get(SESSION_COOKIE)
    rt = client.app.state.runtime
    with rt.panel.transaction() as db:
        row = db.execute("SELECT token_hash FROM admin_sessions").fetchone()
        assert row[0] == digest(old_cookie) and row[0] != old_cookie
    login(client)
    new_cookie = client.cookies.get(SESSION_COOKIE)
    assert new_cookie != old_cookie
    assert rt.panel.session(old_cookie, datetime.now(UTC), credential_fingerprint(settings)) is None
    assert client.post("/admin/logout", data={"csrf": token}).status_code == 403
    token = csrf(client.get("/admin"))
    assert (
        client.post("/admin/logout", data={"csrf": token}, follow_redirects=False).status_code
        == 303
    )
    assert rt.panel.session(new_cookie, datetime.now(UTC), credential_fingerprint(settings)) is None
    assert client.get("/admin", follow_redirects=False).status_code == 303


def test_expiry_and_credentials_rotation(client, settings):
    store = client.app.state.runtime.panel
    now = datetime.now(UTC)
    token, csrf_token = store.new_session(now, 300, credential_fingerprint(settings))
    assert (
        store.session(token, now + timedelta(seconds=299), credential_fingerprint(settings))
        == csrf_token
    )
    assert (
        store.session(token, now + timedelta(seconds=300), credential_fingerprint(settings)) is None
    )
    assert store.session(token, now, "different-credentials") is None


def test_secure_cookie_flags(settings):
    settings = settings.model_copy(update={"admin": AdminConfig(secure_cookies=True)})
    with TestClient(create_app(settings), base_url="https://testserver") as client:
        response = client.get("/admin/login")
        header = response.headers["set-cookie"]
        assert "Secure" in header and "HttpOnly" in header and "SameSite=strict" in header
        response = client.post(
            "/admin/login",
            data={"username": "admin", "password": PASSWORD, "csrf": csrf(response)},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert "Secure" in response.headers["set-cookie"]


def test_invalid_and_oversize_forms(signed_in):
    token = csrf(signed_in.get("/admin"))
    assert signed_in.post("/admin/jobs/run", json={"csrf": token}).status_code == 415
    assert (
        signed_in.post(
            "/admin/jobs/run",
            content="a=" + "x" * 33000,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        ).status_code
        == 413
    )
    assert (
        signed_in.post(
            "/admin/jobs/run",
            content=f"csrf={token}&csrf={token}",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        ).status_code
        == 400
    )
    # Cookie auth for HTML routes does not grant bearer access to the JSON control API.
    assert signed_in.post("/jobs/run").status_code == 401


def test_expired_htmx_request_redirects_top_level_without_http_redirect(client):
    response = client.get("/admin/offers", headers={"HX-Request": "true"})
    assert response.status_code == 401
    assert response.headers["HX-Redirect"] == "/admin/login"
