"""End-to-end tests for `face-match import` (face_match/tools/bulk_import.py) over real HTTP."""

from __future__ import annotations

import csv
import dataclasses
import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn

from face_match.config import Settings
from face_match.main import create_app
from face_match.tools import bulk_import


@pytest.fixture
def live_server(tmp_path):
    app = create_app(dataclasses.replace(Settings(), data_dir=tmp_path / "data"))
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
def photos(tmp_path, passport_thumb, blank, messi_small, lena_variant):
    folder = tmp_path / "photos"
    folder.mkdir()
    (folder / "STU0001_Asha_Verma.jpg").write_bytes(passport_thumb)
    (folder / "STU0002_No Face.jpg").write_bytes(blank)
    (folder / "STU0003_Far Away.jpg").write_bytes(messi_small)  # face only ~31 px wide
    (folder / "README.txt").write_text("ignored")
    (folder / "live").mkdir()
    (folder / "live" / "asha_live.jpg").write_bytes(lena_variant)
    return folder


def _report(path: Path):
    with path.open(newline="") as fh:
        return {row["external_id"]: row for row in csv.DictReader(fh)}


def test_folder_import_reports_each_student_and_is_rerunnable(live_server, photos, tmp_path):
    report = tmp_path / "report.csv"
    args = ["--url", live_server, "--photos", str(photos), "--report", str(report)]

    assert bulk_import.main(args) == 1  # some rows failed
    rows = _report(report)
    assert rows["STU0001"]["status"] == "enrolled"
    assert rows["STU0001"]["name"] == "Asha Verma"
    assert rows["STU0002"]["status"] == "failed" and "no face found" in rows["STU0002"]["detail"]
    assert rows["STU0003"]["status"] == "failed" and "too small" in rows["STU0003"]["detail"]
    assert "live" not in rows or rows["live"]["status"] == "failed"  # sub-folder without an ID prefix

    bulk_import.main(args)
    assert _report(report)["STU0001"]["status"] == "skipped"


def test_csv_import_with_portal_style_headers(live_server, photos, tmp_path):
    roster = tmp_path / "students.csv"
    roster.write_text(
        "Admission_No,Student_Name,Class,Photo\n"
        "STU0001,Asha Verma,7-A,STU0001_Asha_Verma.jpg\n"
        "STU0009,Missing Photo,7-A,does-not-exist.jpg\n"
        ",No ID,7-A,STU0001_Asha_Verma.jpg\n",
        encoding="utf-8-sig",  # as exported by Excel
    )
    report = tmp_path / "report.csv"
    base = ["--url", live_server, "--photos", str(photos), "--csv", str(roster), "--report", str(report)]
    bulk_import.main(base)
    rows = _report(report)
    assert rows["STU0001"]["status"] == "enrolled"
    assert "photo not found" in rows["STU0009"]["detail"]
    assert rows[""]["detail"] == "missing ID"

    # Later: add a fresh live photo to an already enrolled student.
    roster.write_text("admission_no,photo\nSTU0001,live/asha_live.jpg\n")
    bulk_import.main(base + ["--add-photos"])
    row = _report(report)["STU0001"]
    assert row["status"] == "photos_added" and row["photos_accepted"] == "1"
    assert row["name"] == "Asha Verma"  # taken from the enrolled record

    # New school year: replace the photos; a photo without a face changes nothing.
    roster.write_text("admission_no,photo\nSTU0001,STU0002_No Face.jpg\n")
    assert bulk_import.main(base + ["--replace"]) == 1
    roster.write_text("admission_no,photo\nSTU0001,live/asha_live.jpg\n")
    bulk_import.main(base + ["--replace"])
    row = _report(report)["STU0001"]
    assert row["status"] == "replaced" and row["photos_accepted"] == "1"


def test_dry_run_needs_no_server(photos, tmp_path):
    report = tmp_path / "report.csv"
    bulk_import.main(["--dry-run", "--photos", str(photos), "--report", str(report)])
    rows = _report(report)
    assert rows["STU0001"]["status"] == "valid"
