"""Admin UI accounts: login sessions, CSRF, roles, last-admin protection, audit log, pagination."""

from __future__ import annotations

import io

import pytest

ADMIN = {"X-API-Key": "admin-key"}
PASSWORD = "correct horse battery"


def _img(image: bytes):
    return {"image": ("f.jpg", image, "image/jpeg")}


def _enroll(client, image, external_id="EMP-1", headers=None):
    return client.post(
        "/api/v1/persons",
        data={"name": "Lena", "external_id": external_id},
        files=[("images", ("f.jpg", image, "image/jpeg"))],
        headers=headers or {},
    )


def _create_user(client, username, role, password=PASSWORD):
    res = client.post(
        "/api/v1/users", json={"username": username, "password": password, "role": role}, headers=ADMIN
    )
    assert res.status_code == 201, res.text
    return res.json()


def _login(client, username, password=PASSWORD):
    return client.post("/api/v1/auth/login", json={"username": username, "password": password})


def test_operator_session_with_csrf(make_client, lena):
    with make_client(api_key="admin-key") as client:
        _create_user(client, "asha", "operator")

        res = _login(client, "asha")
        assert res.status_code == 200
        body = res.json()
        assert body["kind"] == "user" and body["role"] == "operator"
        assert body["scopes"] == ["attendance", "enroll", "read", "recognize"]
        cookie = res.headers["set-cookie"].lower()
        assert "fm_session=" in cookie and "httponly" in cookie and "samesite=strict" in cookie
        csrf = {"X-CSRF-Token": body["csrf_token"]}

        assert client.get("/api/v1/auth/me").json()["name"] == "asha"
        assert client.get("/api/v1/persons").status_code == 200

        # Changes need the CSRF header, which a cross-site page can't send.
        no_csrf = _enroll(client, lena)
        assert no_csrf.status_code == 403 and no_csrf.json()["error"]["code"] == "csrf_failed"
        assert _enroll(client, lena, headers=csrf).status_code == 201

        # Operators can't do admin things.
        res = client.post("/api/v1/keys", json={"name": "x", "scopes": ["read"]}, headers=csrf)
        assert res.status_code == 403 and res.json()["error"]["code"] == "forbidden"

        assert client.post("/api/v1/auth/logout").status_code == 204
        assert client.get("/api/v1/auth/me").status_code == 401


def test_viewer_is_read_only(make_client, lena):
    with make_client(api_key="admin-key") as client:
        _create_user(client, "viewer1", "viewer")
        csrf = {"X-CSRF-Token": _login(client, "viewer1").json()["csrf_token"]}
        assert client.get("/api/v1/attendance/summary").status_code == 200
        assert _enroll(client, lena, headers=csrf).status_code == 403
        assert client.post("/api/v1/recognize", files=_img(lena), headers=csrf).status_code == 403


def test_wrong_password_and_unknown_user_look_the_same(make_client):
    with make_client(api_key="admin-key") as client:
        _create_user(client, "asha", "operator")
        wrong = _login(client, "asha", "not the password")
        missing = _login(client, "nobody", "whatever-password")
        assert wrong.status_code == missing.status_code == 401
        assert wrong.json()["error"] == missing.json()["error"]


def test_login_brute_force_is_throttled(make_client):
    with make_client(api_key="admin-key", auth_failures_per_minute=2) as client:
        _create_user(client, "asha", "operator")
        codes = [_login(client, "asha", "guess-guess-guess").status_code for _ in range(3)]
        assert codes == [401, 401, 429]


def test_disabling_or_resetting_password_ends_sessions(make_client):
    with make_client(api_key="admin-key") as client:
        user = _create_user(client, "asha", "operator")
        _login(client, "asha")
        assert client.get("/api/v1/auth/me").status_code == 200
        res = client.patch(f"/api/v1/users/{user['id']}", json={"disabled": True}, headers=ADMIN)
        assert res.status_code == 200 and res.json()["disabled_at"]
        assert client.get("/api/v1/auth/me").status_code == 401
        assert _login(client, "asha").status_code == 401

        client.patch(f"/api/v1/users/{user['id']}", json={"disabled": False}, headers=ADMIN)
        assert _login(client, "asha").status_code == 200
        client.patch(f"/api/v1/users/{user['id']}", json={"password": "a brand new password"}, headers=ADMIN)
        assert client.get("/api/v1/auth/me").status_code == 401
        assert _login(client, "asha", "a brand new password").status_code == 200


def test_last_admin_cannot_be_removed(make_client):
    with make_client(api_key="admin-key") as client:
        admin = _create_user(client, "root", "admin")
        for method, body in [("patch", {"role": "viewer"}), ("patch", {"disabled": True}), ("delete", None)]:
            kwargs = {"json": body} if body else {}
            res = getattr(client, method)(f"/api/v1/users/{admin['id']}", headers=ADMIN, **kwargs)
            assert res.status_code == 409 and res.json()["error"]["code"] == "last_admin", method
        _create_user(client, "root2", "admin")
        assert client.delete(f"/api/v1/users/{admin['id']}", headers=ADMIN).status_code == 204


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        ({"username": "ab", "password": PASSWORD, "role": "viewer"}, "invalid_username"),
        ({"username": "asha", "password": "short", "role": "viewer"}, "weak_password"),
    ],
)
def test_user_validation(make_client, payload, code):
    with make_client(api_key="admin-key") as client:
        res = client.post("/api/v1/users", json=payload, headers=ADMIN)
        assert res.status_code == 422 and res.json()["error"]["code"] == code


def test_duplicate_username_rejected_case_insensitively(make_client):
    with make_client(api_key="admin-key") as client:
        _create_user(client, "Asha", "viewer")
        res = client.post("/api/v1/users", json={"username": "asha", "password": PASSWORD}, headers=ADMIN)
        assert res.status_code == 409


def test_first_user_turns_authentication_on(client):
    assert client.get("/health").json()["login"] is False
    assert client.get("/api/v1/persons").status_code == 200  # open on localhost, no accounts yet
    _create_user(client, "root", "admin")  # an open server accepts the admin call
    health = client.get("/health").json()
    assert health["login"] is True and health["auth_required"] is True
    assert client.get("/api/v1/persons").status_code == 401


def test_secure_cookie_flag(make_client):
    with make_client(api_key="admin-key", session_cookie_secure="true") as client:
        _create_user(client, "asha", "viewer")
        assert "secure" in _login(client, "asha").headers["set-cookie"].lower()


def test_audit_log_records_who_did_what(make_client, lena):
    with make_client(api_key="admin-key") as client:
        _create_user(client, "asha", "operator")
        _login(client, "asha", "wrong-password-123")
        csrf = {"X-CSRF-Token": _login(client, "asha").json()["csrf_token"]}
        person = _enroll(client, lena, headers=csrf).json()["person"]
        client.post("/api/v1/auth/logout")
        client.post("/api/v1/keys", json={"name": "gate", "scopes": ["attendance"]}, headers=ADMIN)
        client.delete(f"/api/v1/persons/{person['id']}?erase_history=true", headers=ADMIN)

        entries = client.get("/api/v1/audit", headers=ADMIN).json()
        actions = [(e["actor"], e["action"]) for e in reversed(entries)]
        assert actions == [
            ("admin_key", "user.create"),
            ("user:asha", "auth.login_failed"),
            ("user:asha", "auth.login"),
            ("user:asha", "person.create"),
            ("user:asha", "auth.logout"),
            ("admin_key", "key.create"),
            ("admin_key", "person.delete"),
        ]
        deletion = entries[0]
        assert deletion["details"] == {"name": "Lena", "external_id": "EMP-1", "erase_history": True}
        assert deletion["request_id"] and deletion["ip"]
        filtered = client.get("/api/v1/audit?action=person.*", headers=ADMIN).json()
        assert {e["action"] for e in filtered} == {"person.create", "person.delete"}


def test_people_list_pagination(client, lena, lena_variant):
    client.post(
        "/api/v1/persons",
        data={"name": "Bea", "external_id": "B"},
        files=[("images", ("f.jpg", lena, "image/jpeg"))],
    )
    client.post(
        "/api/v1/persons",
        data={"name": "Amy", "external_id": "A", "allow_duplicate": "true"},
        files=[("images", ("f.jpg", lena_variant, "image/jpeg"))],
    )
    page1 = client.get("/api/v1/persons?limit=1")
    page2 = client.get("/api/v1/persons?limit=1&offset=1")
    assert page1.headers["X-Total-Count"] == "2"
    assert [p["name"] for p in page1.json() + page2.json()] == ["Amy", "Bea"]


def test_cli_users(tmp_path, monkeypatch, capsys):
    from face_match.cli import main

    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr("sys.stdin", io.StringIO(PASSWORD + "\n"))
    main(["users", "create", "--username", "root", "--role", "admin", "--password-stdin"])
    assert "Created admin 'root'" in capsys.readouterr().out
    main(["users", "list"])
    assert "root" in capsys.readouterr().out
    with pytest.raises(SystemExit, match="only active admin"):
        main(["users", "disable", "--username", "root"])
    monkeypatch.setattr("sys.stdin", io.StringIO("short\n"))
    with pytest.raises(SystemExit, match="at least"):
        main(["users", "set-password", "--username", "root", "--password-stdin"])
