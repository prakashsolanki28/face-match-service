"""Core API behaviour on generated images (see fakes.py)."""

from __future__ import annotations

import json

from fakes import Spec, blank_png, face_png


def _files(*images: bytes):
    return [("images", (f"photo{i}.png", data, "image/png")) for i, data in enumerate(images)]


def _enroll(client, *images: bytes, name="Asha", external_id="EMP-1", **data):
    fields = {"name": name, **({"external_id": external_id} if external_id else {}), **data}
    return client.post("/api/v1/persons", data=fields, files=_files(*images))


def _post(client, path: str, image: bytes, **data):
    return client.post(path, data=data, files={"image": ("frame.png", image, "image/png")})


def _first(client, path: str, image: bytes, **data):
    return _post(client, path, image, **data).json()["faces"][0]


# ---- service basics ---------------------------------------------------------------------


def test_health_and_root(client):
    health = client.get("/health").json()
    assert health["status"] == "ok" and health["engine"] == "fake"
    assert health["persons"] == 0 and health["auth_required"] is False
    root = client.get("/").json()
    assert root["service"] == "FaceMatch" and root["api"] == "/api/v1"


def test_api_only_no_ui_and_docs_off_by_default(client, make_client):
    assert client.get("/index.html").status_code == 404
    assert client.get("/kiosk.html").status_code == 404
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").json()["info"]["title"] == "FaceMatch"
    with make_client(api_docs=True) as docs:
        assert docs.get("/docs").status_code == 200


def test_invalid_and_oversize_images(client, make_client):
    res = _post(client, "/api/v1/detect", b"not an image")
    assert res.status_code == 400 and res.json()["error"]["code"] == "invalid_image"
    with make_client(max_upload_mb=0.001) as small:
        res = _post(small, "/api/v1/detect", face_png(Spec(1), width=1200, height=900))
        assert res.status_code == 413 and res.json()["error"]["code"] == "image_too_large"


# ---- detection ---------------------------------------------------------------------------


def test_detect_reports_boxes_and_landmarks(client):
    body = _post(client, "/api/v1/detect", face_png(Spec(1, size=200), Spec(2, size=100))).json()
    assert body["image"] == {"width": 640, "height": 480}
    assert [f["box"]["width"] for f in body["faces"]] == [200, 100]  # largest first
    assert len(body["faces"][0]["landmarks"]) == 5


def test_large_images_are_downscaled_but_boxes_use_original_pixels(client):
    body = _post(client, "/api/v1/detect", face_png(Spec(1, size=800), width=3200, height=2400)).json()
    box = body["faces"][0]["box"]
    assert body["image"] == {"width": 3200, "height": 2400}
    assert abs(box["width"] - 800) <= 4 and abs(box["x"] - 20) <= 4


def test_no_face(client):
    assert _post(client, "/api/v1/detect", blank_png()).json()["faces"] == []


# ---- recognition -----------------------------------------------------------------------


def test_recognise_another_photo_of_the_same_person(client):
    person = _enroll(client, face_png(Spec(1))).json()["person"]
    face = _first(client, "/api/v1/recognize", face_png(Spec(1, variant=3)))
    assert face["match"]["person_id"] == person["id"] and face["match"]["similarity"] > 0.9
    assert face["unknown_reason"] is None and face["closest"] is None


def test_unknown_faces_explain_why(client):
    assert _first(client, "/api/v1/recognize", face_png(Spec(1)))["unknown_reason"] == "nobody_enrolled"
    _enroll(client, face_png(Spec(1)))
    stranger = _first(client, "/api/v1/recognize", face_png(Spec(2)))
    assert stranger["match"] is None and stranger["unknown_reason"] == "below_threshold"
    assert stranger["closest"] is None  # opt-in: callers must never act on a near-miss
    tuning = _first(client, "/api/v1/recognize?closest=true", face_png(Spec(2)))
    assert tuning["closest"]["name"] == "Asha" and tuning["closest"]["similarity"] < 0.4
    small = _first(client, "/api/v1/recognize", face_png(Spec(1, size=24)))
    assert small["match"] is None and small["unknown_reason"] == "too_small"


def test_twins_are_too_close_to_call(client):
    _enroll(client, face_png(Spec(5)), name="Twin A", external_id="T-1")
    blocked = _enroll(client, face_png(Spec(105)), name="Twin B", external_id="T-2")
    assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "already_enrolled"
    twin = _enroll(client, face_png(Spec(105)), name="Twin B", external_id="T-2", allow_duplicate="true")
    assert twin.status_code == 201
    face = _first(client, "/api/v1/recognize", face_png(Spec(5, variant=1)))
    assert face["match"] is None and face["unknown_reason"] == "ambiguous"


def test_lookup_by_external_id_and_pagination(client):
    _enroll(client, face_png(Spec(1)), name="Bea", external_id="B")
    _enroll(client, face_png(Spec(2)), name="Amy", external_id="A")
    found = client.get("/api/v1/persons", params={"external_id": "B"}).json()
    assert [p["name"] for p in found] == ["Bea"]
    assert client.get("/api/v1/persons", params={"external_id": "nope"}).json() == []
    page1 = client.get("/api/v1/persons?limit=1")
    page2 = client.get("/api/v1/persons?limit=1&offset=1")
    assert page1.headers["X-Total-Count"] == "2"
    assert [p["name"] for p in page1.json() + page2.json()] == ["Amy", "Bea"]


# ---- enrollment quality checks ---------------------------------------------------------


def test_enroll_rejects_unusable_photos(client):
    res = _enroll(client, blank_png())
    assert res.status_code == 422 and res.json()["error"]["details"][0]["reason"] == "no_face"
    res = _enroll(client, face_png(Spec(1, size=150), Spec(2, size=140)))
    assert res.json()["error"]["details"][0]["reason"] == "multiple_faces"
    res = _enroll(client, face_png(Spec(1, size=30)))
    assert res.json()["error"]["details"][0]["reason"] == "face_too_small"


def test_small_bystander_is_ignored(client):
    res = _enroll(client, face_png(Spec(1, size=200), Spec(2, size=60)))
    assert res.status_code == 201 and res.json()["accepted"] == 1


def test_duplicate_face_and_external_id(client):
    assert _enroll(client, face_png(Spec(1))).status_code == 201
    same_face = _enroll(client, face_png(Spec(1, variant=2)), name="Other", external_id="EMP-2")
    assert same_face.status_code == 409 and same_face.json()["error"]["code"] == "already_enrolled"
    same_id = _enroll(client, face_png(Spec(3)), name="Other", external_id="EMP-1")
    assert same_id.status_code == 409 and same_id.json()["error"]["code"] == "external_id_exists"


def test_look_alikes_are_not_flagged_as_duplicates(client):
    """Similarity 0.46: above the match threshold, below DUPLICATE_THRESHOLD (0.50)."""
    assert _enroll(client, face_png(Spec(7)), external_id="K-1").status_code == 201
    res = _enroll(client, face_png(Spec(157)), name="Look-alike", external_id="M-1")
    assert res.status_code == 201 and res.json()["rejected"] == []


def test_odd_photo_is_dropped_whatever_the_upload_order(client):
    good = [face_png(Spec(1, variant=v)) for v in (0, 1, 2)]
    odd = face_png(Spec(9))
    forward = _enroll(client, odd, *good, external_id="F").json()
    backward = _enroll(client, *good, odd, external_id="B", allow_duplicate="true").json()
    assert forward["accepted"] == backward["accepted"] == 3
    assert forward["rejected"] == [{"index": 0, "filename": "photo0.png", "reason": "inconsistent_identity"}]


def test_add_and_replace_photos(client):
    person_id = _enroll(client, face_png(Spec(1))).json()["person"]["id"]
    body = client.post(
        f"/api/v1/persons/{person_id}/faces", files=_files(face_png(Spec(1, variant=1)), face_png(Spec(2)))
    ).json()
    assert body["accepted"] == 1 and body["person"]["face_count"] == 2
    assert body["rejected"][0]["reason"] == "inconsistent_identity"

    bad = client.put(f"/api/v1/persons/{person_id}/faces", files=_files(blank_png()))
    assert bad.status_code == 422
    assert client.get(f"/api/v1/persons/{person_id}").json()["face_count"] == 2  # all-or-nothing
    good = client.put(f"/api/v1/persons/{person_id}/faces", files=_files(face_png(Spec(1, variant=5))))
    assert good.status_code == 200 and good.json()["person"]["face_count"] == 1


# ---- photo-only enrollment and metadata ---------------------------------------------------


def test_enroll_with_a_photo_only_returns_an_id(client):
    res = client.post("/api/v1/persons", files=_files(face_png(Spec(1))))
    assert res.status_code == 201
    person = res.json()["person"]
    assert len(person["id"]) == 32
    assert person["name"] is None and person["external_id"] is None and person["metadata"] == {}

    face = _first(client, "/api/v1/recognize", face_png(Spec(1, variant=2)))
    assert face["match"]["person_id"] == person["id"] and face["match"]["name"] is None

    # A blank name is the same as none; named people are listed first.
    _enroll(client, face_png(Spec(2)), name="  ", external_id="E-2")
    _enroll(client, face_png(Spec(3)), name="Zoe", external_id="E-3")
    assert [p["name"] for p in client.get("/api/v1/persons").json()] == ["Zoe", None, None]


def test_metadata_is_stored_and_returned_with_matches(client):
    fields = {"class": "7-A", "roll_no": 12, "bus": True, "note": None, "fee_paid": 99.5, "नाम": "आशा"}
    res = _enroll(client, face_png(Spec(1)), metadata=json.dumps(fields))
    assert res.status_code == 201
    person = res.json()["person"]
    assert person["metadata"] == fields
    assert client.get(f"/api/v1/persons/{person['id']}").json()["metadata"] == fields
    assert client.get("/api/v1/persons").json()[0]["metadata"] == fields
    assert _first(client, "/api/v1/recognize", face_png(Spec(1, variant=1)))["match"]["metadata"] == fields
    stranger = _first(client, "/api/v1/recognize?closest=true", face_png(Spec(2)))
    assert stranger["closest"]["metadata"] == fields

    empty = _enroll(client, face_png(Spec(3)), external_id="E-3", metadata="  ")
    assert empty.status_code == 201 and empty.json()["person"]["metadata"] == {}


def test_invalid_metadata_is_rejected_before_enrolling(client):
    bad = [
        "not json",
        '["a", "list"]',
        '{"class": {"nested": 1}}',
        '{"subjects": ["maths"]}',
        '{"score": NaN}',
        json.dumps({f"k{i}": i for i in range(51)}),
        json.dumps({"k" * 65: 1}),
        json.dumps({" ": 1}),
        json.dumps({"note": "x" * 1001}),
    ]
    for text in bad:
        res = _enroll(client, face_png(Spec(1)), metadata=text)
        assert res.status_code == 422 and res.json()["error"]["code"] == "invalid_metadata", text
    assert client.get("/api/v1/persons").json() == []


def test_update_person(client):
    person = _enroll(client, face_png(Spec(1)), metadata='{"class": "7-A", "bus": true}').json()["person"]
    url = f"/api/v1/persons/{person['id']}"

    updated = client.patch(url, json={"metadata": {"class": "8-A"}})
    assert updated.status_code == 200
    assert updated.json()["metadata"] == {"class": "8-A"}  # replaced, not merged
    assert updated.json()["name"] == "Asha" and updated.json()["external_id"] == "EMP-1"  # unchanged
    assert _first(client, "/api/v1/recognize", face_png(Spec(1)))["match"]["metadata"] == {"class": "8-A"}

    cleared = client.patch(url, json={"name": None, "external_id": None, "metadata": None}).json()
    assert cleared["name"] is None and cleared["external_id"] is None and cleared["metadata"] == {}
    renamed = client.patch(url, json={"name": " Asha Verma ", "external_id": "STU0001"}).json()
    assert renamed["name"] == "Asha Verma" and renamed["external_id"] == "STU0001"
    assert client.patch(url, json={"external_id": "STU0001"}).status_code == 200  # its own ID
    assert client.patch(url, json={}).json() == renamed

    _enroll(client, face_png(Spec(2)), external_id="STU0002")
    taken = client.patch(url, json={"external_id": "STU0002"})
    assert taken.status_code == 409 and taken.json()["error"]["code"] == "external_id_exists"
    nested = client.patch(url, json={"metadata": {"a": {"b": 1}}})
    assert nested.status_code == 422 and nested.json()["error"]["code"] == "invalid_metadata"
    unknown = client.patch(url, json={"metadta": {}})
    assert unknown.status_code == 422 and unknown.json()["error"]["code"] == "validation_error"
    for body in ({"name": "X"}, {"external_id": "STU0002"}):
        missing = client.patch("/api/v1/persons/nope", json=body)
        assert missing.status_code == 404 and missing.json()["error"]["code"] == "person_not_found"
    assert client.get(url).json() == renamed


# ---- matching a camera frame -------------------------------------------------------------


def test_a_frame_returns_the_ids_of_everyone_recognised(client):
    """The caller (e.g. a school portal) acts on match.person_id, e.g. to mark people present."""
    asha = _enroll(client, face_png(Spec(1)), external_id="STU1").json()["person"]["id"]
    ravi = _enroll(client, face_png(Spec(2)), name="Ravi", external_id="STU2").json()["person"]["id"]
    frame = face_png(Spec(1, size=150, variant=1), Spec(2, size=120, variant=2), Spec(3, size=100))
    faces = _post(client, "/api/v1/recognize", frame).json()["faces"]
    assert [f["match"]["person_id"] if f["match"] else None for f in faces] == [asha, ravi, None]
    assert [f["match"]["external_id"] for f in faces[:2]] == ["STU1", "STU2"]
    assert faces[2]["unknown_reason"] == "below_threshold" and faces[2]["closest"] is None


def test_recognition_records_nothing_and_attendance_is_gone(client):
    _enroll(client, face_png(Spec(1)))
    for _ in range(2):
        assert _first(client, "/api/v1/recognize", face_png(Spec(1)))["match"]["name"] == "Asha"
    for method, path in [
        ("post", "/api/v1/attendance"),
        ("get", "/api/v1/attendance"),
        ("get", "/api/v1/attendance/summary"),
        ("get", "/api/v1/webhooks"),
    ]:
        assert getattr(client, method)(path).status_code in (404, 405), path


def test_delete_removes_the_person(client):
    person_id = _enroll(client, face_png(Spec(1))).json()["person"]["id"]
    assert client.delete(f"/api/v1/persons/{person_id}").status_code == 204
    assert client.get(f"/api/v1/persons/{person_id}").status_code == 404
    assert _first(client, "/api/v1/recognize", face_png(Spec(1)))["unknown_reason"] == "nobody_enrolled"
    assert client.delete(f"/api/v1/persons/{person_id}").status_code == 404


def test_data_persists_across_restarts(make_client):
    with make_client() as client:
        _enroll(client, face_png(Spec(1)))
    with make_client() as client:
        assert _first(client, "/api/v1/recognize", face_png(Spec(1)))["match"]["name"] == "Asha"
