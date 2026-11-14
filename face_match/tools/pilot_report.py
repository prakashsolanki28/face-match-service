#!/usr/bin/env python3
"""Compare FaceMatch attendance with a manual register, to validate a pilot.

During a pilot, keep taking attendance the usual way (paper or portal) and export it as CSV:

    external_id,status
    STU0001,present
    STU0002,absent

ID columns are matched like the bulk importer (external_id, student_id, admission_no, roll_no,
employee_id, id). The status column may be called status/attendance/present and hold
present/absent, P/A, yes/no, 1/0 or true/false.

    face-match pilot-report --register register-2026-11-14.csv --date 2026-11-14

Outcomes per person:
  confirmed          present in the register and marked by FaceMatch
  missed             present in the register, not marked (unknown, too small, or skipped the kiosk)
  FALSE_PRESENT      absent in the register but marked by FaceMatch: a wrong name, a spoof,
                     or a register mistake. Investigate every one.
  not_enrolled       present in the register but not enrolled in FaceMatch
  not_in_register    marked by FaceMatch but missing from the register

Exits with status 1 when there is any FALSE_PRESENT, so it can gate a go-live decision.
Uses only the Python standard library (plus bulk_import.py from the same folder).
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import urllib.error
import urllib.parse
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional, Sequence

try:
    from .bulk_import import ID_COLUMNS, Client
except ImportError:  # run as a standalone script next to bulk_import.py
    from bulk_import import ID_COLUMNS, Client  # type: ignore[no-redef]

STATUS_COLUMNS = ("status", "attendance", "present")
PRESENT = {"present", "p", "yes", "y", "1", "true"}
ABSENT = {"absent", "a", "no", "n", "0", "false"}


def read_register(path: Path) -> Dict[str, bool]:
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        headers = {h.strip().lower(): h for h in (reader.fieldnames or [])}
        id_col = next((headers[c] for c in ID_COLUMNS if c in headers), None)
        status_col = next((headers[c] for c in STATUS_COLUMNS if c in headers), None)
        if not id_col or not status_col:
            raise SystemExit(
                f"Register needs an ID column ({', '.join(ID_COLUMNS)}) and a status column "
                f"({', '.join(STATUS_COLUMNS)}). Found: {', '.join(reader.fieldnames or [])}"
            )
        register: Dict[str, bool] = {}
        for line, row in enumerate(reader, start=2):
            external_id = (row.get(id_col) or "").strip()
            value = (row.get(status_col) or "").strip().lower()
            if not external_id:
                continue
            if value not in PRESENT | ABSENT:
                raise SystemExit(f"{path}:{line}: unrecognised status {value!r} for {external_id}")
            register[external_id] = value in PRESENT
        return register


def compare(register: Dict[str, bool], marked: Dict[str, dict], enrolled: set) -> List[dict]:
    rows = []
    for external_id in sorted(set(register) | set(marked)):
        present = register.get(external_id)
        seen = marked.get(external_id)
        if present is None:
            outcome = "not_in_register"
        elif present and seen:
            outcome = "confirmed"
        elif present and external_id not in enrolled:
            outcome = "not_enrolled"
        elif present:
            outcome = "missed"
        elif seen:
            outcome = "FALSE_PRESENT"
        else:
            outcome = "absent"
        rows.append(
            {
                "external_id": external_id,
                "register": "" if present is None else ("present" if present else "absent"),
                "facematch": "marked" if seen else "",
                "first_seen": (seen or {}).get("first_seen", ""),
                "name": (seen or {}).get("name", ""),
                "outcome": outcome,
            }
        )
    return rows


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="face-match pilot-report",
        description="Compare FaceMatch attendance with a manual register.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("\n\n", 1)[1] if __doc__ else None,
    )
    parser.add_argument("--register", type=Path, required=True, help="Manual register CSV.")
    parser.add_argument("--date", required=True, help="Day to compare, YYYY-MM-DD.")
    parser.add_argument("--url", default=os.environ.get("FACE_SERVICE_URL", "http://127.0.0.1:8000"))
    parser.add_argument(
        "--api-key",
        default=os.environ.get("FACE_API_KEY") or os.environ.get("API_KEY", ""),
        help="The service's API key (or set FACE_API_KEY).",
    )
    parser.add_argument("--report", type=Path, help="Where to write the per-person CSV.")
    args = parser.parse_args(argv)

    register = read_register(args.register)
    client = Client(args.url, args.api_key)
    try:
        summary = client.get(f"/api/v1/attendance/summary?date={urllib.parse.quote(args.date)}")
        persons = client.get("/api/v1/persons")
    except (urllib.error.URLError, OSError) as exc:
        raise SystemExit(f"Cannot reach the service at {args.url}: {exc}") from None
    marked = {r["external_id"]: r for r in summary if r.get("external_id")}
    enrolled = {p["external_id"] for p in persons if p.get("external_id")}
    rows = compare(register, marked, enrolled)

    report = args.report or Path(f"pilot-report-{args.date}.csv")
    with report.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]) if rows else ["external_id", "outcome"])
        writer.writeheader()
        writer.writerows(rows)

    counts = Counter(r["outcome"] for r in rows)
    present = sum(1 for v in register.values() if v)
    pct = (100.0 * counts["confirmed"] / present) if present else 0.0
    print(f"Pilot report for {args.date}: {len(register)} people in the register, {present} present")
    print(f"  confirmed        {counts['confirmed']:>4}  ({pct:.1f}% of those present were marked)")
    print(f"  missed           {counts['missed']:>4}")
    print(f"  not enrolled     {counts['not_enrolled']:>4}")
    print(f"  not in register  {counts['not_in_register']:>4}")
    print(
        f"  FALSE PRESENT    {counts['FALSE_PRESENT']:>4}  (must be 0: wrong name, spoof or register error)"
    )
    for r in rows:
        if r["outcome"] == "FALSE_PRESENT":
            print(
                f"    ! {r['external_id']} {r['name']} marked at {r['first_seen']} but absent in the register"
            )
    print(f"Report: {report.resolve()}")
    return 1 if counts["FALSE_PRESENT"] else 0


if __name__ == "__main__":
    sys.exit(main())
