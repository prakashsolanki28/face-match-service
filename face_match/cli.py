"""Command line: `face-match <command>` (or `python -m face_match <command>`).

serve            run the HTTP service (default)
download-models  download and verify the ONNX models
import           bulk-enroll people from a CSV roster or photo folder
evaluate         measure accuracy on a labelled photo set
"""

from __future__ import annotations

import argparse
import ipaddress
import logging
import sys
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
    sub.add_parser("import", help="Bulk-enroll people (see `face-match import --help`).", add_help=False)
    sub.add_parser("evaluate", help="Measure accuracy (see `face-match evaluate --help`).", add_help=False)

    # `import` and `evaluate` own their arguments; hand everything after the command to them.
    if argv and argv[0] in ("import", "evaluate"):
        command, rest = argv[0], argv[1:]
        if command == "import":
            from .tools import bulk_import

            sys.exit(bulk_import.main(rest))
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

    host = getattr(args, "host", None) or settings.host
    port = getattr(args, "port", None) or settings.port
    check_auth(settings, host)

    import uvicorn

    uvicorn.run(
        "face_match.main:create_app",
        factory=True,
        host=host,
        port=port,
        reload=getattr(args, "reload", False),
        log_level=settings.log_level,
    )


if __name__ == "__main__":
    main()
