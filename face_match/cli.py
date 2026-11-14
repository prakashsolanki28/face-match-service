"""Command line: `face-match <command>` (or `python -m face_match <command>`).

serve            run the HTTP service (default)
download-models  download and verify the ONNX models
backup           write a consistent backup of the database
import           bulk-enroll people from a CSV roster or photo folder
evaluate         measure accuracy on a labelled photo set
pilot-report     compare system attendance with a manual register
loadtest         measure throughput and latency for capacity planning
export-api       write openapi.json and a Postman collection for API clients
"""

from __future__ import annotations

import argparse
import ipaddress
import logging
import sys
from pathlib import Path
from typing import List, Optional

from . import __version__
from .config import Settings


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check_auth(settings: Settings, host: str) -> None:
    """Refuse to expose an unauthenticated API on a network interface (secure by default)."""
    if settings.api_key or settings.allow_no_auth or _is_loopback(host):
        return
    raise SystemExit(
        f"Refusing to listen on {host} without authentication.\n"
        "Set API_KEY (recommended), or set ALLOW_NO_AUTH=true to run an open API on purpose.\n"
        'Generate a key with:  python -c "import secrets; print(secrets.token_urlsafe(32))"'
    )


def main(argv: Optional[List[str]] = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(
        prog="face-match", description="FaceMatch: self-hosted face detection and matching service."
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command")
    serve = sub.add_parser("serve", help="Run the HTTP service (default).")
    serve.add_argument("--host", help="Interface to bind (default from HOST, else 127.0.0.1).")
    serve.add_argument("--port", type=int, help="Port (default from PORT, else 8000).")
    serve.add_argument("--reload", action="store_true", help="Auto-reload on code changes.")
    sub.add_parser("download-models", help="Download and verify the ONNX models.")
    backup = sub.add_parser("backup", help="Write a consistent database backup (safe while serving).")
    backup.add_argument("--out", help="Target file (default: DATA_DIR/backups/facematch-<time>.db).")
    sub.add_parser("import", help="Bulk-enroll people (see `face-match import --help`).", add_help=False)
    sub.add_parser("evaluate", help="Measure accuracy (see `face-match evaluate --help`).", add_help=False)
    sub.add_parser(
        "pilot-report", help="Compare attendance with a manual register (see --help).", add_help=False
    )
    sub.add_parser("loadtest", help="Measure throughput and latency (see --help).", add_help=False)
    sub.add_parser(
        "export-api", help="Write openapi.json and a Postman collection (see --help).", add_help=False
    )

    # These tools own their arguments; hand everything after the command to them.
    if argv and argv[0] in ("import", "evaluate", "pilot-report", "loadtest", "export-api"):
        command, rest = argv[0], argv[1:]
        if command == "export-api":
            from .tools import export_api

            sys.exit(export_api.main(rest))
        if command == "import":
            from .tools import bulk_import

            sys.exit(bulk_import.main(rest))
        if command == "pilot-report":
            from .tools import pilot_report

            sys.exit(pilot_report.main(rest))
        if command == "loadtest":
            from .tools import loadtest

            sys.exit(loadtest.main(rest))
        from .tools import evaluate

        sys.exit(evaluate.main(rest))

    args = parser.parse_args(argv)
    settings = Settings.from_env()

    if args.command == "download-models":
        from .model_store import ensure_models

        logging.basicConfig(level=logging.INFO, format="%(message)s")
        for key, path in ensure_models(settings.models_dir).items():
            print(f"{key:<11} {path}")
        return

    if args.command == "backup":
        from .maintenance import make_backup
        from .storage import open_database

        db = open_database(settings)
        try:
            if args.out:
                path = db.backup_to(Path(args.out))
            else:
                path = make_backup(db, settings.backup_dir, settings.backup_keep)
        finally:
            db.close()
        print(f"Backup written: {path}")
        return

    host = getattr(args, "host", None) or settings.host
    port = getattr(args, "port", None) or settings.port
    check_auth(settings, host)

    import uvicorn

    from .observability import configure_logging

    configure_logging(settings.log_level, settings.log_format)
    uvicorn.run(
        "face_match.main:create_app",
        factory=True,
        host=host,
        port=port,
        reload=getattr(args, "reload", False),
        log_level=settings.log_level,
        log_config=None,  # logging is configured by FaceMatch (text or JSON)
        access_log=False,  # FaceMatch logs each request itself, with a request id
    )


if __name__ == "__main__":
    main()
