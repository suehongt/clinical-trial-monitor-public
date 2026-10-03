#!/usr/bin/env bash
# ── One supported way to launch the full application ────────────────────────
#
#   ./start.sh              # production-style: API + built SPA on one port
#   ./start.sh --rebuild    # force a fresh frontend build first
#   ./start.sh --dev        # frontend dev server (HMR) on :5173 + API on :8000
#
# Both modes serve the SPA and the API from the same origin (the vite dev
# server proxies /api to the API), so every page — Dashboard, Trials,
# Monitors, Updates — reaches the monitoring backend.  `Ctrl-C` stops both.
#
# Environment: CT_DB_PATH (database override), CT_HOST, CT_PORT (default 8000).
set -euo pipefail
cd "$(dirname "$0")"

PY=venv/bin/python
MODE="prod"
REBUILD=0
for arg in "$@"; do
  case "$arg" in
    --dev) MODE="dev" ;;
    --rebuild) REBUILD=1 ;;
    *) echo "usage: ./start.sh [--dev] [--rebuild]" >&2; exit 2 ;;
  esac
done

[ -x "$PY" ] || { echo "error: venv not found — create it first (python3 -m venv venv && venv/bin/pip install -r requirements.txt)" >&2; exit 1; }

if [ ! -d web/node_modules ]; then
  echo "==> installing frontend dependencies"
  (cd web && npm install)
fi

build_web() {
  echo "==> building frontend (web/dist)"
  (cd web && npm run build)
}

if [ "$MODE" = "dev" ]; then
  [ -d web/dist ] || build_web   # FastAPI needs dist to exist even in dev
  echo "==> starting API on ${CT_PORT:-8000} and frontend dev server on 5173"
  trap 'kill 0' EXIT INT TERM
  CT_PORT="${CT_PORT:-8000}" "$PY" -m server &
  (cd web && npm run dev)
else
  if [ "$REBUILD" = 1 ] || [ ! -f web/dist/index.html ]; then
    build_web
  fi
  echo "==> http://127.0.0.1:${CT_PORT:-8000}  (API + app on the same port)"
  exec "$PY" -m server
fi
