"""Several FaceMatch servers sharing one database (SQLite file here; PostgreSQL in CI)."""

from __future__ import annotations

import threading
import time

from face_match.storage import open_database


def _img(image: bytes):
    return {"image": ("f.jpg", image, "image/jpeg")}


def test_enrollment_on_one_server_reaches_the_other(make_client, lena):
    with make_client() as server_a, make_client() as server_b:
        assert server_b.post("/api/v1/recognize", files=_img(lena)).json()["faces"][0]["match"] is None

        res = server_a.post(
            "/api/v1/persons",
            data={"name": "Lena", "external_id": "EMP-1"},
            files=[("images", ("f.jpg", lena, "image/jpeg"))],
        )
        person_id = res.json()["person"]["id"]
        assert server_b.app.state.service.sync_gallery() == 1  # normally every GALLERY_SYNC_SECONDS
        match = server_b.post("/api/v1/recognize", files=_img(lena)).json()["faces"][0]["match"]
        assert match["person_id"] == person_id

        server_a.delete(f"/api/v1/persons/{person_id}")
        server_b.app.state.service.sync_gallery()
        assert server_b.post("/api/v1/recognize", files=_img(lena)).json()["faces"][0]["match"] is None
        assert server_b.app.state.service.sync_gallery() == 0  # nothing new: cheap no-op


def test_background_sync_picks_up_changes(make_client, lena):
    with make_client(gallery_sync_seconds=0.2) as server_a, make_client(gallery_sync_seconds=0.2) as server_b:
        server_a.post(
            "/api/v1/persons",
            data={"name": "Lena"},
            files=[("images", ("f.jpg", lena, "image/jpeg"))],
        )
        deadline = time.time() + 5
        while time.time() < deadline and len(server_b.app.state.service.gallery) == 0:
            time.sleep(0.05)
        assert len(server_b.app.state.service.gallery) == 1


def test_a_webhook_delivery_is_claimed_by_only_one_server(make_client, lena):
    with make_client() as client:
        settings = client.app.state.settings
        client.post(
            "/api/v1/persons", data={"name": "Lena"}, files=[("images", ("f.jpg", lena, "image/jpeg"))]
        )
        person = client.app.state.service.db.list_persons()[0]
        db_a, db_b = open_database(settings), open_database(settings)
        try:
            db_a.mark_attendance(person, 0.9, None, time.time(), "2026-01-01", "t", 0, enqueue_webhook=True)
            claimed_a = db_a.claim_due_webhooks(time.time())
            claimed_b = db_b.claim_due_webhooks(time.time())
            assert len(claimed_a) == 1 and claimed_b == []
        finally:
            db_a.close()
            db_b.close()


def test_simultaneous_check_ins_on_two_servers_create_one_event(make_client, lena):
    with make_client() as client:
        settings = client.app.state.settings
        client.post(
            "/api/v1/persons", data={"name": "Lena"}, files=[("images", ("f.jpg", lena, "image/jpeg"))]
        )
        person = client.app.state.service.db.list_persons()[0]
        databases = [open_database(settings) for _ in range(4)]
        barrier = threading.Barrier(len(databases))
        created = []

        def check_in(db):
            barrier.wait()
            ok, _ = db.mark_attendance(person, 0.9, "gate", time.time(), "2026-01-01", "t", 300)
            created.append(ok)

        threads = [threading.Thread(target=check_in, args=(db,)) for db in databases]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        for db in databases:
            db.close()
        assert sorted(created) == [False, False, False, True]
        assert len(client.get("/api/v1/attendance").json()) == 1
