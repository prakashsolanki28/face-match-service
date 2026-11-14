"""End-to-end tests for `face-match import` over real HTTP, with generated image files."""

from __future__ import annotations

import csv
import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from conftest import make_test_app, reset_test_database
from fakes import Spec, blank_png, face_png

from face_match.tools import bulk_import


@pytest.fixture
def live_server(tmp_path):
    reset_test_database()
    app = make_test_app(tmp_path)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.02)
    yield f"http://127.0.0.1:{sock.getsockname()[1]}"
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture
def photos(tmp_path):
    folder = tmp_path / "photos"
    folder.mkdir()
    (folder / "STU0001_Asha_Verma.png").write_bytes(face_png(Spec(1)))
    (folder / "STU0002_No Face.png").write_bytes(blank_png())
    (folder / "STU0003_Far Away.png").write_bytes(face_png(Spec(3, size=24)))
    (folder / "README.txt").write_text("ignored")
    (folder / "live").mkdir()
    (folder / "live" / "asha_live.png").write_bytes(face_png(Spec(1, variant=4)))
    return folder


def _report(path: Path):
    with path.open(newline="") as fh:
        return {row["external_id"]: row for row in csv.DictReader(fh)}


def test_folder_import_reports_each_person_and_is_rerunnable(live_server, photos, tmp_path):
    report = tmp_path / "report.csv"
    args = ["--url", live_server, "--photos", str(photos), "--report", str(report)]
    assert bulk_import.main(args) == 1  # some rows failed
    rows = _report(report)
    assert rows["STU0001"]["status"] == "enrolled" and rows["STU0001"]["name"] == "Asha Verma"
    assert rows["STU0002"]["status"] == "failed" and "no face found" in rows["STU0002"]["detail"]
    assert rows["STU0003"]["status"] == "failed" and "too small" in rows["STU0003"]["detail"]
    bulk_import.main(args)
    assert _report(report)["STU0001"]["status"] == "skipped"


def test_csv_import_add_and_replace(live_server, photos, tmp_path):
    roster = tmp_path / "people.csv"
    roster.write_text(
        "Admission_No,Student_Name,Class,Photo\n"
        "STU0001,Asha Verma,7-A,STU0001_Asha_Verma.png\n"
        "STU0009,Missing Photo,7-A,does-not-exist.png\n"
        ",No ID,7-A,STU0001_Asha_Verma.png\n",
        encoding="utf-8-sig",  # as exported by Excel
    )
    report = tmp_path / "report.csv"
    base = ["--url", live_server, "--photos", str(photos), "--csv", str(roster), "--report", str(report)]
    bulk_import.main(base)
    rows = _report(report)
    assert rows["STU0001"]["status"] == "enrolled"
    assert "photo not found" in rows["STU0009"]["detail"] and rows[""]["detail"] == "missing ID"

    roster.write_text("admission_no,photo\nSTU0001,live/asha_live.png\n")
    bulk_import.main(base + ["--add-photos"])
    row = _report(report)["STU0001"]
    assert row["status"] == "photos_added" and row["photos_accepted"] == "1" and row["name"] == "Asha Verma"

    roster.write_text("admission_no,photo\nSTU0001,STU0002_No Face.png\n")
    assert bulk_import.main(base + ["--replace"]) == 1  # unusable photo: old photos kept
    roster.write_text("admission_no,photo\nSTU0001,live/asha_live.png\n")
    bulk_import.main(base + ["--replace"])
    assert _report(report)["STU0001"]["status"] == "replaced"


def test_dry_run_needs_no_server(photos, tmp_path):
    report = tmp_path / "report.csv"
    bulk_import.main(["--dry-run", "--photos", str(photos), "--report", str(report)])
    assert _report(report)["STU0001"]["status"] == "valid"
