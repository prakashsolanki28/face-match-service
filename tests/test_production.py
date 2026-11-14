"""Production features: scoped keys, audit log, rate limiting, overload, reliable webhooks, metrics,
structured logs, data protection (backups, retention) and the pilot report. Generated images only."""

from __future__ import annotations

import json
import logging
import sqlite3
import stat
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from conftest import sqlite_only
from fakes import Spec, face_png

from face_match.maintenance import MaintenanceWorker, apply_retention, list_backups, make_backup
from face_match.observability import JsonFormatter
from face_match.security import Busy, ImageGate
from face_match.storage import Database
from face_match.tools import pilot_report

ADMIN = {"X-API-Key": "admin-key"}


def _img(person: int = 1, variant: int = 0):
    return {"image": ("f.png", face_png(Spec(person, variant)), "image/png")}


def _enroll(client, person=1, external_id="EMP-1", headers=None):
    return client.post(
        "/api/v1/persons",
        data={"name": "Asha", "external_id": external_id},
        files=[("images", ("f.png", face_png(Spec(person)), "image/png"))],
        headers=headers or {},
    )


def _new_key(client, scopes, name="device"):
    res = client.post("/api/v1/keys", json={"name": name, "scopes": scopes}, headers=ADMIN)
    assert res.status_code == 201, res.text
    return {"X-API-Key": res.json()["token"]}, res.json()["key"]


# ---- scoped API keys ------------------------------------------------------------


def test_kiosk_key_can_only_mark_attendance(make_client):
    with make_client(api_key="admin-key") as client:
        assert _enroll(client, headers=ADMIN).status_code == 201
        kiosk, key = _new_key(client, ["attendance"], "gate-1")
        assert key["prefix"].startswith("fm_") and key["scopes"] == ["attendance"]
        assert client.post("/api/v1/attendance", files=_img(), headers=kiosk).status_code == 200
        for method, path in [
            ("get", "/api/v1/persons"),
            ("post", "/api/v1/recognize"),
            ("get", "/api/v1/keys"),
        ]:
            kwargs = {"files": _img()} if method == "post" else {}
            res = getattr(client, method)(path, headers=kiosk, **kwargs)
            assert res.status_code == 403 and res.json()["error"]["code"] == "forbidden", path
        assert _enroll(client, 2, "EMP-2", headers=kiosk).status_code == 403
        me = client.get("/api/v1/auth/me", headers=kiosk).json()
        assert me == {"kind": "key", "name": "gate-1", "scopes": ["attendance"]}


def test_bearer_auth_and_revocation(make_client):
    with make_client(api_key="admin-key") as client:
        reader, key = _new_key(client, ["read"])
        bearer = {"Authorization": f"Bearer {reader['X-API-Key']}"}
        assert client.get("/api/v1/persons", headers=bearer).status_code == 200
        listed = client.get("/api/v1/keys", headers=ADMIN).json()
        assert [k["id"] for k in listed] == [key["id"]] and "token" not in listed[0]
        assert client.delete(f"/api/v1/keys/{key['id']}", headers=ADMIN).status_code == 204
        assert client.get("/api/v1/persons", headers=reader).status_code == 401


def test_unknown_scope_rejected(make_client):
    with make_client(api_key="admin-key") as client:
        res = client.post("/api/v1/keys", json={"name": "x", "scopes": ["root"]}, headers=ADMIN)
        assert res.status_code == 422 and res.json()["error"]["code"] == "invalid_scopes"


def test_device_keys_alone_turn_auth_on(client):
    assert client.get("/health").json()["auth_required"] is False
    client.app.state.service.db.create_api_key("kiosk", ["attendance"], "2026-01-01T00:00:00")
    client.app.state.keys.invalidate()
    assert client.get("/health").json()["auth_required"] is True
    assert client.get("/api/v1/persons").status_code == 401


def test_audit_log_records_which_key_did_what(make_client):
    with make_client(api_key="admin-key") as client:
        portal, key = _new_key(client, ["enroll", "read"], "portal-sync")
        person = _enroll(client, headers=portal).json()["person"]
        client.post(
            f"/api/v1/persons/{person['id']}/faces",
            files=[("images", ("f.png", face_png(Spec(1, 1)), "image/png"))],
            headers=portal,
        )
        client.delete(f"/api/v1/persons/{person['id']}?erase_history=true", headers=ADMIN)
        client.delete(f"/api/v1/keys/{key['id']}", headers=ADMIN)

        entries = client.get("/api/v1/audit", headers=ADMIN).json()
        actor = f"key:portal-sync#{key['id']}"
        assert [(e["actor"], e["action"]) for e in reversed(entries)] == [
            ("admin_key", "key.create"),
            (actor, "person.create"),
            (actor, "person.add_faces"),
            ("admin_key", "person.delete"),
            ("admin_key", "key.revoke"),
        ]
        deletion = entries[1]
        assert deletion["details"] == {"name": "Asha", "external_id": "EMP-1", "erase_history": True}
        assert deletion["request_id"] and deletion["ip"]
        filtered = client.get("/api/v1/audit?action=person.*", headers=ADMIN).json()
        assert {e["action"] for e in filtered} == {"person.create", "person.add_faces", "person.delete"}
        assert client.get("/api/v1/audit", headers=portal).status_code == 401  # revoked


# ---- abuse protection -------------------------------------------------------------


def test_rate_limit_returns_429_with_retry_after(make_client):
    with make_client(rate_limit_per_minute=3) as client:
        assert [client.get("/api/v1/persons").status_code for _ in range(4)] == [200, 200, 200, 429]
        res = client.get("/api/v1/persons")
        assert res.json()["error"]["code"] == "rate_limited" and int(res.headers["Retry-After"]) >= 1


def test_repeated_bad_keys_are_throttled(make_client):
    with make_client(api_key="admin-key", auth_failures_per_minute=2) as client:
        codes = [client.get("/api/v1/persons", headers={"X-API-Key": "guess"}).status_code for _ in range(3)]
        assert codes == [401, 401, 429]


def test_image_gate_refuses_when_full():
    gate = ImageGate(1, wait_seconds=0.05)
    with gate.slot(), pytest.raises(Busy), gate.slot():
        pass
    with gate.slot():  # released again
        pass


# ---- reliable webhooks ------------------------------------------------------------


class _Receiver:
    """Local HTTP endpoint that fails the first `fail_first` requests."""

    def __init__(self, fail_first: int = 0):
        self.requests = []
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                receiver.requests.append((dict(self.headers), json.loads(body)))
                self.send_response(204 if len(receiver.requests) > fail_first else 500)
                self.end_headers()

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/hook"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def wait_for(self, count: int, timeout: float = 10.0):
        deadline = time.time() + timeout
        while len(self.requests) < count and time.time() < deadline:
            time.sleep(0.05)
        return self.requests

    def close(self):
        self.server.shutdown()


def _wait_status(client, status, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        rows = client.app.state.service.db.list_webhooks(status, 10)
        if rows:
            return rows
        time.sleep(0.05)
    return []


def test_webhook_is_retried_until_delivered(make_client):
    receiver = _Receiver(fail_first=2)
    try:
        with make_client(webhook_url=receiver.url, webhook_retry_base_seconds=0.2) as client:
            _enroll(client)
            client.post("/api/v1/attendance", files=_img())
            requests = receiver.wait_for(3)
            assert len(requests) == 3
            assert len({headers["X-Event-Id"] for headers, _ in requests}) == 1  # same id on every retry
            assert requests[-1][1]["data"]["name"] == "Asha"
            row = _wait_status(client, "delivered")[0]
            assert row["attempts"] == 3
    finally:
        receiver.close()


def test_webhook_gives_up_then_can_be_retried(make_client):
    receiver = _Receiver(fail_first=10**6)
    try:
        settings = dict(
            api_key="admin-key",
            webhook_url=receiver.url,
            webhook_retry_base_seconds=0.1,
            webhook_max_attempts=2,
        )
        with make_client(**settings) as client:
            _enroll(client, headers=ADMIN)
            client.post("/api/v1/attendance", files=_img(), headers=ADMIN)
            receiver.wait_for(2)
            assert _wait_status(client, "failed")
            failed = client.get("/api/v1/webhooks?status=failed", headers=ADMIN).json()
            assert failed[0]["attempts"] == 2 and "500" in failed[0]["last_error"]
            assert client.post(f"/api/v1/webhooks/{failed[0]['id']}/retry", headers=ADMIN).status_code == 202
            assert len(receiver.wait_for(3)) >= 3
    finally:
        receiver.close()


# ---- observability ----------------------------------------------------------------


def test_metrics_and_request_ids(make_client):
    with make_client(api_key="admin-key") as client:
        assert client.get("/health", headers={"X-Request-ID": "abc123"}).headers["X-Request-ID"] == "abc123"
        assert len(client.get("/health").headers["X-Request-ID"]) == 16

        client.post("/api/v1/recognize", files=_img(), headers=ADMIN)
        kiosk, _ = _new_key(client, ["attendance"])
        assert client.get("/metrics", headers=kiosk).status_code == 403
        scraper, _ = _new_key(client, ["metrics"])
        body = client.get("/metrics", headers=scraper).text
        assert 'facematch_faces_total{endpoint="recognize",outcome="nobody_enrolled"} 1' in body
        assert 'facematch_http_requests_total{method="POST",route="/api/v1/recognize",status="200"} 1' in body
        assert "facematch_http_request_duration_seconds_bucket" in body
        assert "facematch_enrolled_persons 0" in body and 'facematch_build_info{version="' in body


def test_json_log_format():
    record = logging.LogRecord("face_match.access", logging.INFO, __file__, 1, "GET /x 200", None, None)
    record.request_id, record.status = "rid-1", 200
    line = json.loads(JsonFormatter().format(record))
    assert line["msg"] == "GET /x 200" and line["level"] == "info"
    assert line["request_id"] == "rid-1" and line["status"] == 200


# ---- data protection ----------------------------------------------------------------


def test_database_and_backups_are_owner_only(tmp_path):
    db = Database(tmp_path / "data" / "facematch.db")
    assert stat.S_IMODE((tmp_path / "data" / "facematch.db").stat().st_mode) == 0o600
    db.create_person("A", "A-1", [], "2026-01-01T00:00:00")
    backups = tmp_path / "data" / "backups"
    for _ in range(3):
        path = make_backup(db, backups, keep=2)
        time.sleep(1.05)  # backup names have one-second resolution
    assert len(list_backups(backups)) == 2
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    copy = sqlite3.connect(path)
    assert copy.execute("SELECT name FROM persons").fetchall() == [("A",)]
    copy.close()
    db.close()


@sqlite_only
def test_maintenance_backs_up_and_applies_retention(make_client):
    with make_client(attendance_retention_days=1, backup_interval_hours=1) as client:
        service = client.app.state.service
        _enroll(client)
        person = service.db.list_persons()[0]
        old = time.time() - 3 * 86400
        service.db.mark_attendance(person, 0.9, None, old, "2026-01-01", "2026-01-01T09:00:00+00:00", 0)
        client.post("/api/v1/attendance", files=_img())
        assert len(service.db.list_attendance(None, None, 10)) == 2

        MaintenanceWorker(service.db, service.settings).run_once()
        assert len(list_backups(service.settings.backup_dir)) == 1
        assert len(service.db.list_attendance(None, None, 10)) == 1  # the 3-day-old event is gone
        assert apply_retention(service.db, service.settings)["attendance"] == 0


# ---- CLI & pilot report ---------------------------------------------------------------


def test_cli_keys_and_backup(tmp_path, monkeypatch, capsys):
    from face_match.cli import main

    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    main(["keys", "create", "--name", "gate-1", "--scopes", "attendance,read"])
    out = capsys.readouterr().out
    assert out.split("Token (shown once, store it securely): ")[1].strip().startswith("fm_")
    key_id = out.split("Created key ")[1].split()[0]
    main(["keys", "list"])
    assert "gate-1" in capsys.readouterr().out
    main(["keys", "revoke", key_id])
    with pytest.raises(SystemExit):
        main(["keys", "create", "--name", "x", "--scopes", "superuser"])
    main(["backup"])
    assert "Backup written" in capsys.readouterr().out


def test_pilot_report_compare_flags_false_presents():
    register = {"A": True, "B": True, "C": False, "D": True}
    marked = {"A": {"name": "a", "first_seen": "09:00"}, "C": {"name": "c", "first_seen": "09:05"}, "E": {}}
    outcomes = {
        r["external_id"]: r["outcome"] for r in pilot_report.compare(register, marked, {"A", "B", "C"})
    }
    assert outcomes == {
        "A": "confirmed",
        "B": "missed",
        "C": "FALSE_PRESENT",
        "D": "not_enrolled",
        "E": "not_in_register",
    }


def test_pilot_report_reads_portal_style_register(tmp_path):
    path = tmp_path / "register.csv"
    path.write_text("Admission_No,Attendance\nSTU1,P\nSTU2,A\nSTU3,present\n", encoding="utf-8-sig")
    assert pilot_report.read_register(path) == {"STU1": True, "STU2": False, "STU3": True}
