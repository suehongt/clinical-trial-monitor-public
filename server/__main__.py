"""``python -m server`` — run the local web platform (API + static viewer).

Environment:
    CT_DB_PATH        database file override (default db/ct_monitor.db)
    CT_HOST           bind address (default 127.0.0.1 — local/intranet only)
    CT_PORT           port (default 8000)
    CT_AUTH_USER      shared-credential Basic auth (with CT_AUTH_PASSWORD);
                      both unset keeps the app fully open (localhost use)
    CT_AUTH_PASSWORD  see CT_AUTH_USER — see deploy/DEPLOY.md for the
                      server deployment that relies on this gate
"""
from __future__ import annotations

import os
import sys

import uvicorn

from .app import create_app


def main() -> None:
    mode = os.environ.get("CT_MODE", "development").lower()
    host = os.environ.get("CT_HOST", "127.0.0.1")
    if mode == "production" and host not in {"127.0.0.1", "::1", "localhost"}:
        raise RuntimeError("production mode is single-user localhost only; use a trusted reverse proxy for remote access")
    if mode == "production" and not (os.environ.get("CT_AUTH_USER") and os.environ.get("CT_AUTH_PASSWORD")):
        print("WARNING: CT_MODE=production without CT_AUTH_USER/CT_AUTH_PASSWORD — "
              "the API has no built-in gate and is only as safe as your reverse "
              "proxy. Set both env vars to enable Basic auth (deploy/DEPLOY.md).",
              file=sys.stderr)
    app = create_app(db_path=os.environ.get("CT_DB_PATH") or None)
    uvicorn.run(
        app,
        host=host,
        port=int(os.environ.get("CT_PORT", "8000")),
        log_level="info",
    )


if __name__ == "__main__":
    main()
