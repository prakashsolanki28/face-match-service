from __future__ import annotations

import hashlib
import hmac
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import cv2
import numpy as np


def _enroll(client, image: bytes, name="Lena", external_id="EMP-1"):
    data = {"name": name}
    if external_id:
        data["external_id"] = external_id
    return client.post("/api/v1/persons", data=data, files=[("images", ("face.jpg", image, "image/jpeg"))])


def _post_image(client, path: str, image: bytes, **data):
    return client.post(path, data=data, files={"image": ("frame.jpg", image, "image/jpeg")})


def test_health(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["engine"] == "opencv-yunet-sface"
    assert body["persons"] == 0
    assert body["auth_required"] is False


def test_demo_ui_is_served(client):
    res = client.get("/")
    assert res.status_code == 200
    assert "FaceMatch" in res.text


def test_detect_finds_face_with_landmarks(client, lena):
    body = _post_image(client, "/api/v1/detect", lena).json()
    assert body["image"] == {"width": 512, "height": 512}
    assert len(body["faces"]) == 1
    face = body["faces"][0]
    assert face["score"] > 0.9
    assert len(face["landmarks"]) == 5
    assert face["box"]["width"] > 100


def test_detect_no_face(client, blank):
    assert _post_image(client, "/api/v1/detect", blank).json()["faces"] == []


def test_invalid_image_returns_structured_error(client):
    res = _post_image(client, "/api/v1/detect", b"definitely not an image")
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "invalid_image"


def test_oversize_image_rejected(make_client, lena):
    with make_client(max_upload_mb=0.01) as client:
        res = _post_image(client, "/api/v1/detect", lena)
    assert res.status_code == 413
    assert res.json()["error"]["code"] == "image_too_large"


def test_enroll_then_recognize_same_and_other_person(client, lena, lena_variant, messi):
    res = _enroll(client, lena)
    assert res.status_code == 201, res.text
    person = res.json()["person"]
    assert person["face_count"] == 1 and person["external_id"] == "EMP-1"

    match = _post_image(client, "/api/v1/recognize", lena_variant).json()["faces"][0]["match"]
    assert match["person_id"] == person["id"]
    assert match["similarity"] > 0.6

    other = _post_image(client, "/api/v1/recognize", messi).json()["faces"]
    assert len(other) == 1 and other[0]["match"] is None


def test_unknown_faces_explain_why(client, lena, messi):
    face = _post_image(client, "/api/v1/recognize", lena).json()["faces"][0]
    assert face["match"] is None and face["unknown_reason"] == "nobody_enrolled"

    _enroll(client, lena)
    face = _post_image(client, "/api/v1/recognize", messi).json()["faces"][0]
    assert face["match"] is None and face["unknown_reason"] == "below_threshold"
    assert face["closest"]["name"] == "Lena" and face["closest"]["similarity"] < 0.40

    # The kiosk endpoint gives the reason but never reveals who the stranger resembles.
    face = _post_image(client, "/api/v1/attendance", messi).json()["faces"][0]
    assert face["unknown_reason"] == "below_threshold" and face["closest"] is None

    known = _post_image(client, "/api/v1/recognize", lena).json()["faces"][0]
    assert known["match"]["name"] == "Lena"
    assert known["unknown_reason"] is None and known["closest"] is None


def test_enroll_rejects_photo_without_face(client, blank):
    res = _enroll(client, blank)
    assert res.status_code == 422
    error = res.json()["error"]
    assert error["code"] == "no_usable_face"
    assert error["details"][0]["reason"] == "no_face"


def test_enroll_rejects_duplicate_face_and_external_id(client, lena, lena_variant):
    assert _enroll(client, lena).status_code == 201
    same_face = _enroll(client, lena_variant, name="Imposter", external_id="EMP-2")
    assert same_face.status_code == 409
    assert same_face.json()["error"]["code"] == "already_enrolled"
    same_id = _enroll(client, lena, name="Other", external_id="EMP-1")
    assert same_id.status_code == 409
    assert same_id.json()["error"]["code"] == "external_id_exists"


def test_small_passport_thumbnail_enrolls_and_matches_live_photo(client, passport_thumb, lena_variant):
    # 100x125 px: too small for the detector as-is, handled by the upscale fallback.
    res = _enroll(client, passport_thumb, external_id="STU0001")
    assert res.status_code == 201, res.text
    match = _post_image(client, "/api/v1/recognize", lena_variant).json()["faces"][0]["match"]
    assert match is not None and match["external_id"] == "STU0001"


def test_detect_reports_boxes_in_original_coordinates_after_upscale(client, passport_thumb):
    body = _post_image(client, "/api/v1/detect", passport_thumb).json()
    assert body["image"] == {"width": 100, "height": 125}
    box = body["faces"][0]["box"]
    assert box["x"] >= 0 and box["x"] + box["width"] <= 100
    assert box["y"] >= 0 and box["y"] + box["height"] <= 125


def test_ambiguous_match_between_two_people_is_unknown(client, lena):
    """Twins/look-alikes: when two enrolled people score almost equally, don't guess."""
    image = cv2.imdecode(np.frombuffer(lena, np.uint8), cv2.IMREAD_COLOR)
    twin_a = cv2.imencode(".jpg", cv2.GaussianBlur(image, (5, 5), 0))[1].tobytes()
    twin_b = cv2.imencode(".jpg", cv2.convertScaleAbs(image, alpha=0.8))[1].tobytes()
    assert _enroll(client, twin_a, name="Twin A", external_id="T-1").status_code == 201

    blocked = _enroll(client, twin_b, name="Twin B", external_id="T-2")
    assert blocked.status_code == 409  # look-alike caught by the duplicate check...
    twin = client.post(  # ...and enrolled anyway once an admin confirms
        "/api/v1/persons",
        data={"name": "Twin B", "external_id": "T-2", "allow_duplicate": "true"},
        files=[("images", ("b.jpg", twin_b, "image/jpeg"))],
    )
    assert twin.status_code == 201, twin.text

    # The original photo is ~0.98 similar to both twins: too close to call.
    face = _post_image(client, "/api/v1/attendance", lena).json()["faces"][0]
    assert face["match"] is None and face["attendance"]["status"] == "unknown"
    assert face["unknown_reason"] == "ambiguous"
    assert client.get("/api/v1/attendance").json() == []


def test_lookup_person_by_external_id(client, lena):
    person_id = _enroll(client, lena, external_id="STU0042").json()["person"]["id"]
    found = client.get("/api/v1/persons", params={"external_id": "STU0042"}).json()
    assert [p["id"] for p in found] == [person_id]
    assert client.get("/api/v1/persons", params={"external_id": "nope"}).json() == []


def test_odd_first_photo_does_not_become_the_reference(client, lena, lena_variant, messi):
    """The photo that agrees with the others is the reference, whatever the upload order."""
    files = [
        ("images", ("odd.jpg", messi, "image/jpeg")),  # a different person, uploaded first
        ("images", ("a.jpg", lena, "image/jpeg")),
        ("images", ("b.jpg", lena_variant, "image/jpeg")),
    ]
    body = client.post("/api/v1/persons", data={"name": "Lena"}, files=files).json()
    assert body["accepted"] == 2
    assert body["rejected"] == [{"index": 0, "filename": "odd.jpg", "reason": "inconsistent_identity"}]
    assert _post_image(client, "/api/v1/recognize", lena).json()["faces"][0]["match"]["name"] == "Lena"


def test_replace_faces_is_all_or_nothing(client, lena, lena_variant, blank):
    person_id = _enroll(client, lena).json()["person"]["id"]

    bad = client.put(f"/api/v1/persons/{person_id}/faces", files=[("images", ("x.jpg", blank, "image/jpeg"))])
    assert bad.status_code == 422
    assert client.get(f"/api/v1/persons/{person_id}").json()["face_count"] == 1  # untouched

    files = [("images", ("a.jpg", lena, "image/jpeg")), ("images", ("b.jpg", lena_variant, "image/jpeg"))]
    good = client.put(f"/api/v1/persons/{person_id}/faces", files=files)
    assert good.status_code == 200, good.text
    assert good.json()["person"] == {**good.json()["person"], "id": person_id, "face_count": 2}
    assert (
        _post_image(client, "/api/v1/recognize", lena).json()["faces"][0]["match"]["person_id"] == person_id
    )


def test_add_faces_to_person(client, lena, lena_variant, messi):
    person_id = _enroll(client, lena).json()["person"]["id"]
    files = [
        ("images", ("a.jpg", lena_variant, "image/jpeg")),
        ("images", ("b.jpg", messi, "image/jpeg")),
    ]
    body = client.post(f"/api/v1/persons/{person_id}/faces", files=files).json()
    assert body["accepted"] == 1
    assert body["person"]["face_count"] == 2
    assert body["rejected"] == [{"index": 1, "filename": "b.jpg", "reason": "inconsistent_identity"}]


def test_small_faces_are_flagged_not_guessed(client, lena, messi_small):
    _enroll(client, lena)
    face = _post_image(client, "/api/v1/attendance", messi_small).json()["faces"][0]
    assert face["box"]["width"] < 40
    assert face["match"] is None and face["attendance"]["status"] == "too_small"
    assert face["unknown_reason"] == "too_small"


def test_attendance_flow_with_cooldown_and_summary(client, lena, lena_variant, messi):
    person_id = _enroll(client, lena).json()["person"]["id"]

    first = _post_image(client, "/api/v1/attendance", lena_variant, device_id="gate-1").json()
    face = first["faces"][0]
    assert face["attendance"]["status"] == "marked"
    assert first["device_id"] == "gate-1"

    again = _post_image(client, "/api/v1/attendance", lena).json()["faces"][0]
    assert again["attendance"]["status"] == "already_marked"
    assert again["attendance"]["event_id"] == face["attendance"]["event_id"]

    stranger = _post_image(client, "/api/v1/attendance", messi).json()["faces"][0]
    assert stranger["attendance"]["status"] == "unknown"

    events = client.get("/api/v1/attendance").json()
    assert len(events) == 1 and events[0]["person_id"] == person_id

    summary = client.get("/api/v1/attendance/summary").json()
    assert len(summary) == 1
    assert summary[0]["name"] == "Lena" and summary[0]["events"] == 1


def test_zero_cooldown_logs_every_sighting(make_client, lena):
    with make_client(attendance_cooldown_seconds=0) as client:
        _enroll(client, lena)
        for _ in range(2):
            status = _post_image(client, "/api/v1/attendance", lena).json()["faces"][0]["attendance"]
            assert status["status"] == "marked"
        assert len(client.get("/api/v1/attendance").json()) == 2


def test_delete_person_keeps_history(client, lena):
    person_id = _enroll(client, lena).json()["person"]["id"]
    _post_image(client, "/api/v1/attendance", lena)
    assert client.delete(f"/api/v1/persons/{person_id}").status_code == 204
    assert client.get(f"/api/v1/persons/{person_id}").status_code == 404
    assert _post_image(client, "/api/v1/recognize", lena).json()["faces"][0]["match"] is None
    assert len(client.get("/api/v1/attendance").json()) == 1


def test_api_key_required_when_configured(make_client, lena):
    with make_client(api_key="secret-key") as client:
        assert client.get("/health").status_code == 200  # health stays public
        denied = client.get("/api/v1/persons")
        assert denied.status_code == 401
        assert denied.json()["error"]["code"] == "unauthorized"
        assert client.get("/api/v1/persons", headers={"X-API-Key": "wrong"}).status_code == 401
        assert client.get("/api/v1/persons", headers={"X-API-Key": "secret-key"}).status_code == 200


def test_data_persists_across_restarts(make_client, lena):
    with make_client() as client:
        _enroll(client, lena)
    with make_client() as client:
        match = _post_image(client, "/api/v1/recognize", lena).json()["faces"][0]["match"]
        assert match is not None and match["name"] == "Lena"


def test_webhook_delivers_signed_event(make_client, lena):
    received = []

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            received.append((self.headers.get("X-Signature-256"), body))
            self.send_response(204)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Receiver)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/hook"
        with make_client(webhook_url=url, webhook_secret="shh") as client:
            _enroll(client, lena)
            _post_image(client, "/api/v1/attendance", lena)  # background task runs before return
    finally:
        server.shutdown()

    assert len(received) == 1
    signature, body = received[0]
    expected = "sha256=" + hmac.new(b"shh", body, hashlib.sha256).hexdigest()
    assert hmac.compare_digest(signature, expected)
    payload = json.loads(body)
    assert payload["event"] == "attendance.marked"
    assert payload["data"]["name"] == "Lena" and payload["data"]["external_id"] == "EMP-1"
