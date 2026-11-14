#!/usr/bin/env python3
"""Bulk-enroll people (e.g. all students of a school or staff of a company) into FaceMatch.

Two input layouts are supported.

1) CSV roster + photo folder (recommended; export both from your portal):

       face-match import --csv students.csv --photos ./photos

   The CSV needs an ID column and a photo column; a name column is optional.
   Accepted headers (case-insensitive):
       ID     external_id, student_id, admission_no, roll_no, employee_id, id
       Name   name, full_name, student_name
       Photo  photo, photos, image, filename, file
   Several photos for one person: separate file names with ";". Other columns are ignored.

2) Photo folder only; ID and name come from the file names:

       photos/STU001_Asha Verma.jpg   -> external_id "STU001", name "Asha Verma"
       photos/STU002.jpg              -> external_id "STU002", name "STU002"
       photos/STU003_Ravi Kumar/      -> a sub-folder holds several photos of one person

Safe to re-run: people whose ID is already enrolled are skipped; with --add-photos
their new photos are added, with --replace their photos are swapped for the new ones.
A CSV report with one row per person is always written.

Only the Python standard library is used, so this file can be copied to and run on
the portal server as-is. It is also a reference client for integrating the API.
"""

from __future__ import annotations

import argparse
import csv
import json
import mimetypes
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
ID_COLUMNS = ("external_id", "student_id", "admission_no", "roll_no", "employee_id", "id")
NAME_COLUMNS = ("name", "full_name", "student_name")
PHOTO_COLUMNS = ("photo", "photos", "image", "filename", "file")
REASONS = {
    "invalid_image": "not a valid image",
    "too_large": "file too large",
    "no_face": "no face found",
    "multiple_faces": "more than one face",
    "face_too_small": "face too small / low resolution",
    "inconsistent_identity": "photos show different people",
    "matches_other_person": "matches someone already enrolled",
}


@dataclass
class Entry:
    external_id: str
    name: str
    photos: List[Path]
    source: str  # where the entry came from, for the report


# ---- reading input -------------------------------------------------------------


def entries_from_csv(csv_path: Path, photos_dir: Path) -> List[Entry]:
    with csv_path.open(newline="", encoding="utf-8-sig") as fh:  # utf-8-sig strips Excel's BOM
        reader = csv.DictReader(fh)
        headers = {h.strip().lower(): h for h in (reader.fieldnames or [])}

        def column(options: Sequence[str]) -> Optional[str]:
            return next((headers[o] for o in options if o in headers), None)

        id_col, name_col, photo_col = column(ID_COLUMNS), column(NAME_COLUMNS), column(PHOTO_COLUMNS)
        if not id_col or not photo_col:
            raise SystemExit(
                f"CSV must have an ID column ({', '.join(ID_COLUMNS)}) and a photo column "
                f"({', '.join(PHOTO_COLUMNS)}). Found: {', '.join(reader.fieldnames or [])}"
            )
        entries = []
        for line, row in enumerate(reader, start=2):
            external_id = (row.get(id_col) or "").strip()
            name = (row.get(name_col) or "").strip() if name_col else ""
            files = [f.strip() for f in re.split(r"[;|]", row.get(photo_col) or "") if f.strip()]
            entries.append(
                Entry(external_id, name or external_id, [photos_dir / f for f in files], f"line {line}")
            )
        return entries


def entries_from_folder(photos_dir: Path) -> List[Entry]:
    entries = []
    for path in sorted(photos_dir.iterdir()):
        if path.name.startswith("."):
            continue
        if path.is_dir():
            photos = sorted(p for p in path.iterdir() if p.suffix.lower() in IMAGE_EXTENSIONS)
            stem = path.name
        elif path.suffix.lower() in IMAGE_EXTENSIONS:
            photos, stem = [path], path.stem
        else:
            continue
        external_id, _, name = stem.partition("_")
        name = name.replace("_", " ").strip()
        entries.append(Entry(external_id.strip(), name or external_id.strip(), photos, path.name))
    return entries


# ---- HTTP client ----------------------------------------------------------------


class ApiError(Exception):
    def __init__(self, status: int, body: Dict[str, Any]):
        error = body.get("error", {}) if isinstance(body, dict) else {}
        super().__init__(error.get("message") or f"HTTP {status}")
        self.status = status
        self.code = error.get("code", f"http_{status}")
        self.details = error.get("details")


class Client:
    def __init__(self, base_url: str, api_key: str = "", timeout: float = 60):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def _send(self, method: str, path: str, body: Optional[bytes] = None, content_type: str = "") -> Any:
        request = urllib.request.Request(self.base_url + path, data=body, method=method)
        if content_type:
            request.add_header("Content-Type", content_type)
        if self.api_key:
            request.add_header("X-API-Key", self.api_key)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            try:
                payload = json.loads(exc.read() or b"{}")
            except ValueError:
                payload = {}
            raise ApiError(exc.code, payload) from None

    def get(self, path: str) -> Any:
        return self._send("GET", path)

    def post_multipart(
        self, path: str, fields: List[Tuple[str, str]], files: List[Tuple[str, Path]], method: str = "POST"
    ) -> Any:
        boundary = uuid.uuid4().hex
        chunks: List[bytes] = []
        for name, value in fields:
            chunks.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
                + value.encode("utf-8")
                + b"\r\n"
            )
        for name, path_ in files:
            filename = path_.name.replace('"', "")
            content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
            chunks.append(
                (
                    f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; '
                    f'filename="{filename}"\r\nContent-Type: {content_type}\r\n\r\n'
                ).encode()
                + path_.read_bytes()
                + b"\r\n"
            )
        chunks.append(f"--{boundary}--\r\n".encode())
        return self._send(method, path, b"".join(chunks), f"multipart/form-data; boundary={boundary}")


# ---- import ---------------------------------------------------------------------


def _describe_rejections(rejected: List[Dict[str, Any]]) -> str:
    return "; ".join(
        f"{r.get('filename') or 'photo ' + str(r['index'] + 1)}: {REASONS.get(r['reason'], r['reason'])}"
        for r in rejected
    )


def _describe_error(exc: ApiError) -> str:
    if exc.code == "no_usable_face" and isinstance(exc.details, list):
        return _describe_rejections(exc.details)
    if exc.code == "already_enrolled" and isinstance(exc.details, dict) and exc.details.get("person"):
        other = exc.details["person"]
        who = f"{other.get('external_id') or other['id']} ({other['name']})"
        return f"face matches {who}; use --allow-duplicates for twins"
    return exc.args[0]


def validate(entry: Entry, seen: set) -> Optional[str]:
    if not entry.external_id:
        return "missing ID"
    if entry.external_id in seen:
        return "ID appears more than once in the input"
    if not entry.photos:
        return "no photo listed"
    missing = [p.name for p in entry.photos if not p.is_file()]
    if missing:
        return f"photo not found: {', '.join(missing)}"
    return None


def run_import(
    entries: List[Entry],
    client: Optional[Client],
    add_photos: bool = False,
    allow_duplicates: bool = False,
    log=print,
    replace: bool = False,
) -> List[Dict[str, Any]]:
    """Enroll every entry. With client=None it only validates the input (dry run)."""
    existing: Dict[str, Dict[str, Any]] = {}
    if client is not None:
        existing = {p["external_id"]: p for p in client.get("/api/v1/persons") if p["external_id"]}

    rows: List[Dict[str, Any]] = []
    seen: set = set()
    width = len(str(len(entries)))
    for number, entry in enumerate(entries, start=1):
        status, accepted, detail = "failed", 0, ""
        problem = validate(entry, seen)
        seen.add(entry.external_id)
        if entry.external_id in existing and entry.name == entry.external_id:
            entry.name = existing[entry.external_id]["name"]  # roster had no name column
        if problem:
            detail = problem
        elif client is None:
            status, detail = "valid", f"{len(entry.photos)} photo(s)"
        elif entry.external_id in existing and not (add_photos or replace):
            status, detail = "skipped", "already enrolled"
        else:
            try:
                files = [("images", p) for p in entry.photos]
                if entry.external_id in existing:
                    person_id = urllib.parse.quote(existing[entry.external_id]["id"], safe="")
                    path = f"/api/v1/persons/{person_id}/faces"
                    if replace:  # all-or-nothing on the server: old photos kept if new ones fail
                        extra = [("allow_duplicate", "true")] if allow_duplicates else []
                        result = client.post_multipart(path, extra, files, method="PUT")
                        status = "replaced"
                    else:
                        result = client.post_multipart(path, [], files)
                        status = "photos_added"
                else:
                    fields = [("name", entry.name), ("external_id", entry.external_id)]
                    if allow_duplicates:
                        fields.append(("allow_duplicate", "true"))
                    result = client.post_multipart("/api/v1/persons", fields, files)
                    status = "enrolled"
                accepted = result["accepted"]
                detail = _describe_rejections(result["rejected"])
            except ApiError as exc:
                if exc.status == 401:
                    raise SystemExit(
                        "The service rejected the API key. Pass --api-key or set FACE_API_KEY."
                    ) from None
                detail = f"{exc.code}: {_describe_error(exc)}"
        rows.append(
            {
                "external_id": entry.external_id,
                "name": entry.name,
                "status": status,
                "photos_accepted": accepted,
                "detail": detail,
                "source": entry.source,
            }
        )
        log(
            f"[{number:>{width}}/{len(entries)}] {status:<12} {entry.external_id:<14} {entry.name}"
            + (f"  ({detail})" if detail else "")
        )
    return rows


def write_report(rows: List[Dict[str, Any]], path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh, fieldnames=["external_id", "name", "status", "photos_accepted", "detail", "source"]
        )
        writer.writeheader()
        writer.writerows(rows)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="face-match import",
        description="Bulk-enroll people into FaceMatch.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("\n\n", 1)[1] if __doc__ else None,
    )
    parser.add_argument("--photos", type=Path, required=True, help="Folder containing the photos.")
    parser.add_argument("--csv", type=Path, help="Roster CSV. Without it, IDs come from file names.")
    parser.add_argument(
        "--url", default=os.environ.get("FACE_SERVICE_URL", "http://127.0.0.1:8000"), help="Service base URL."
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("FACE_API_KEY") or os.environ.get("API_KEY", ""),
        help="Service API key (or set FACE_API_KEY).",
    )
    parser.add_argument(
        "--add-photos",
        action="store_true",
        help="For IDs already enrolled, add these photos instead of skipping.",
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="For IDs already enrolled, replace their photos (kept if the new ones fail).",
    )
    parser.add_argument(
        "--allow-duplicates",
        action="store_true",
        help="Enroll even when a face resembles someone already enrolled (twins).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only check the input (IDs, names, files); don't contact the service.",
    )
    parser.add_argument("--report", type=Path, help="Where to write the CSV report.")
    args = parser.parse_args(argv)

    if args.add_photos and args.replace:
        parser.error("use either --add-photos or --replace, not both")
    if not args.photos.is_dir():
        parser.error(f"--photos {args.photos} is not a folder")
    entries = entries_from_csv(args.csv, args.photos) if args.csv else entries_from_folder(args.photos)
    if not entries:
        print("Nothing to import.")
        return 0

    client = None
    if not args.dry_run:
        client = Client(args.url, args.api_key)
        try:
            health = client.get("/health")
        except (urllib.error.URLError, OSError) as exc:
            raise SystemExit(
                f"Cannot reach the service at {args.url} ({exc}).\n"
                "Is it running? Start it in another terminal with:  face-match serve\n"
                "then run this command again."
            ) from None
        print(f"Service {args.url}: {health['persons']} people enrolled. Importing {len(entries)} ...\n")

    started = time.perf_counter()
    rows = run_import(entries, client, args.add_photos, args.allow_duplicates, replace=args.replace)
    report = args.report or Path(f"import-report-{time.strftime('%Y%m%d-%H%M%S')}.csv")
    write_report(rows, report)

    counts = Counter(r["status"] for r in rows)
    print(
        f"\nDone in {time.perf_counter() - started:.1f}s: "
        + ", ".join(f"{n} {s}" for s, n in sorted(counts.items()))
    )
    print(f"Report: {report.resolve()}")
    if counts.get("failed"):
        print(
            "Fix the failed rows (usually a clearer or larger photo) and run the same command again;"
            " people already enrolled are skipped."
        )
    return 1 if counts.get("failed") else 0


if __name__ == "__main__":
    sys.exit(main())
