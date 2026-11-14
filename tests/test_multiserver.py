"""Several FaceMatch servers sharing one database (SQLite file here; PostgreSQL in CI)."""

from __future__ import annotations

import threading
import time

from fakes import Spec, face_png


def _img(person=1):
    return {"image": ("f.png", face_png(Spec(person)), "image/png")}


def _enroll(client, person=1):
    return client.post(
        "/api/v1/persons",
        data={"name": "Asha"},
        files=[("images", ("f.png", face_png(Spec(person)), "image/png"))],
    )


def test_enrollment_on_one_server_reaches_the_other(make_client):
    with make_client() as server_a, make_client() as server_b:
        assert server_b.post("/api/v1/recognize", files=_img()).json()["faces"][0]["match"] is None
        person_id = _enroll(server_a).json()["person"]["id"]
        assert server_b.app.state.service.sync_gallery() == 1  # normally every GALLERY_SYNC_SECONDS
        assert (
            server_b.post("/api/v1/recognize", files=_img()).json()["faces"][0]["match"]["person_id"]
            == person_id
        )

        server_a.delete(f"/api/v1/persons/{person_id}")
        server_b.app.state.service.sync_gallery()
        assert server_b.post("/api/v1/recognize", files=_img()).json()["faces"][0]["match"] is None
        assert server_b.app.state.service.sync_gallery() == 0  # nothing new: cheap no-op


def test_background_sync_picks_up_changes(make_client):
    with make_client(gallery_sync_seconds=0.2) as server_a, make_client(gallery_sync_seconds=0.2) as server_b:
        _enroll(server_a)
        deadline = time.time() + 5
        while time.time() < deadline and len(server_b.app.state.service.gallery) == 0:
            time.sleep(0.05)
        assert len(server_b.app.state.service.gallery) == 1


def test_updates_are_seen_by_the_other_server_at_once(make_client):
    with make_client() as server_a, make_client() as server_b:
        person_id = _enroll(server_a).json()["person"]["id"]
        server_b.app.state.service.sync_gallery()
        server_a.patch(f"/api/v1/persons/{person_id}", json={"metadata": {"class": "8-A"}})
        match = server_b.post("/api/v1/recognize", files=_img()).json()["faces"][0]["match"]
        assert match["metadata"] == {"class": "8-A"}  # details are read from the shared database


def test_simultaneous_enrollments_with_one_external_id_create_one_person(make_client):
    clients = [make_client().__enter__() for _ in range(4)]
    barrier = threading.Barrier(len(clients))
    statuses = []

    def enroll(index, client):
        barrier.wait()
        res = client.post(
            "/api/v1/persons",
            data={"external_id": "STU1"},
            files=[("images", ("f.png", face_png(Spec(index + 1)), "image/png"))],
        )
        statuses.append((res.status_code, res.json().get("error", {}).get("code")))

    threads = [threading.Thread(target=enroll, args=(i, c)) for i, c in enumerate(clients)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for client in clients:
        client.__exit__(None, None, None)
    assert sorted(statuses) == [(201, None)] + [(409, "external_id_exists")] * 3
