"""Command line: `face-match <command>` (or `python -m face_match <command>`).

serve            run the HTTP service (default)
download-models  download and verify the ONNX models
keys             create, list and revoke scoped API keys
users            manage admin UI accounts (admin, operator, viewer)
backup           write a consistent backup of the database
import           bulk-enroll people from a CSV roster or photo folder
evaluate         measure accuracy on a labelled photo set
pilot-report     compare system attendance with a manual register
loadtest         measure throughput and latency for capacity planning
"""

from __future__ import annotations

import argparse
import ipaddress
import logging
import sys
from datetime import UTC
from pathlib import Path
from typing import List, Optional

from . import __version__
from .config import Settings
from .security import SCOPES


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _has_device_keys(settings: Settings) -> bool:
    if not settings.database_url and not settings.db_path.exists():
        return False
    from .storage import open_database

    db = open_database(settings)
    try:
        return db.has_active_api_keys() or db.has_active_users()
    finally:
        db.close()


def check_auth(settings: Settings, host: str) -> None:
    """Refuse to expose an unauthenticated API on a network interface (secure by default)."""
    if settings.api_key or settings.allow_no_auth or _is_loopback(host) or _has_device_keys(settings):
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
    keys = sub.add_parser("keys", help="Manage scoped API keys (stored in the database).")
    keys_sub = keys.add_subparsers(dest="keys_command", required=True)
    create = keys_sub.add_parser("create", help="Create a key; the token is printed once.")
    create.add_argument("--name", required=True, help='e.g. "gate-1 kiosk"')
    create.add_argument("--scopes", required=True, help="Comma-separated: " + ", ".join(SCOPES))
    keys_sub.add_parser("list", help="List keys (tokens are never shown again).")
    revoke = keys_sub.add_parser("revoke", help="Revoke a key by id.")
    revoke.add_argument("key_id")
    users = sub.add_parser("users", help="Manage admin UI accounts.")
    users_sub = users.add_subparsers(dest="users_command", required=True)
    for name, help_text in [
        ("create", "Create a user (password asked interactively, or --password-stdin)."),
        ("set-password", "Reset a user's password (signs them out everywhere)."),
    ]:
        command = users_sub.add_parser(name, help=help_text)
        command.add_argument("--username", required=True)
        command.add_argument("--password-stdin", action="store_true", help="Read the password from stdin.")
        if name == "create":
            command.add_argument("--role", default="admin", choices=["admin", "operator", "viewer"])
    users_sub.add_parser("list", help="List users.")
    for name in ("disable", "enable", "delete"):
        users_sub.add_parser(name, help=f"{name.capitalize()} a user.").add_argument(
            "--username", required=True
        )
    backup = sub.add_parser("backup", help="Write a consistent database backup (safe while serving).")
    backup.add_argument("--out", help="Target file (default: DATA_DIR/backups/facematch-<time>.db).")
    sub.add_parser("import", help="Bulk-enroll people (see `face-match import --help`).", add_help=False)
    sub.add_parser("evaluate", help="Measure accuracy (see `face-match evaluate --help`).", add_help=False)
    sub.add_parser(
        "pilot-report", help="Compare attendance with a manual register (see --help).", add_help=False
    )
    sub.add_parser("loadtest", help="Measure throughput and latency (see --help).", add_help=False)

    # These tools own their arguments; hand everything after the command to them.
    if argv and argv[0] in ("import", "evaluate", "pilot-report", "loadtest"):
        command, rest = argv[0], argv[1:]
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

    if args.command == "keys":
        _keys_command(args, settings)
        return

    if args.command == "users":
        _users_command(args, settings)
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


def _keys_command(args: argparse.Namespace, settings: Settings) -> None:
    from datetime import datetime

    from .storage import open_database

    db = open_database(settings)
    now = datetime.now(UTC).isoformat(timespec="seconds")
    try:
        if args.keys_command == "create":
            scopes = [s.strip() for s in args.scopes.split(",") if s.strip()]
            unknown = sorted(set(scopes) - set(SCOPES))
            if not scopes or unknown:
                raise SystemExit(f"Unknown or missing scopes {unknown}. Choose from: {', '.join(SCOPES)}")
            row, token = db.create_api_key(args.name, scopes, now)
            print(f"Created key {row['id']} ({row['name']}) with scopes: {row['scopes']}")
            print(f"Token (shown once, store it securely): {token}")
        elif args.keys_command == "list":
            for row in db.list_api_keys():
                state = f"revoked {row['revoked_at']}" if row["revoked_at"] else "active"
                print(
                    f"{row['id']}  {row['prefix']}…  {row['name']:<20} [{row['scopes']}]  {state}"
                    f"  last used: {row['last_used_at'] or 'never'}"
                )
        elif args.keys_command == "revoke":
            if not db.revoke_api_key(args.key_id, now):
                raise SystemExit(f"No active key with id {args.key_id}")
            print(f"Revoked {args.key_id} (running servers stop accepting it within 30 s)")
    finally:
        db.close()


def _read_password(args: argparse.Namespace) -> str:
    if args.password_stdin:
        return sys.stdin.readline().rstrip("\n")
    import getpass

    first = getpass.getpass("Password: ")
    if first != getpass.getpass("Repeat password: "):
        raise SystemExit("Passwords don't match.")
    return first


def _users_command(args: argparse.Namespace, settings: Settings) -> None:
    from .service import FaceService, ServiceError
    from .storage import open_database

    db = open_database(settings)

    class _NoEngine:  # user management doesn't need the face models
        embedding_size = 128

    service = FaceService(settings, _NoEngine(), db)  # type: ignore[arg-type]
    try:
        if args.users_command == "list":
            for row in db.list_users():
                state = "disabled" if row["disabled_at"] else "active"
                print(
                    f"{row['id']}  {row['username']:<24} {row['role']:<9} {state:<8} last login: "
                    f"{row['last_login_at'] or 'never'}"
                )
            return
        if args.users_command == "create":
            row = service.create_user(args.username, _read_password(args), args.role)
            print(f"Created {row['role']} '{row['username']}' ({row['id']}). Sign in at the web UI.")
            return
        user = db.find_user(args.username)
        if user is None:
            raise SystemExit(f"No user named {args.username}")
        if args.users_command == "set-password":
            service.update_user(user["id"], password=_read_password(args))
            print(f"Password changed for {user['username']}; their sessions were signed out.")
        elif args.users_command in ("disable", "enable"):
            service.update_user(user["id"], disabled=args.users_command == "disable")
            print(f"{user['username']} {args.users_command}d.")
        elif args.users_command == "delete":
            service.delete_user(user["id"])
            print(f"Deleted {user['username']}.")
    except ServiceError as exc:
        raise SystemExit(exc.message) from None
    finally:
        db.close()


if __name__ == "__main__":
    main()
