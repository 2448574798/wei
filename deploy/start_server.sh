#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_DIR"

if [[ -f ".env" ]]; then
  set -a
  source ".env"
  set +a
fi

mkdir -p "${WEI_LOG_DIR:-./logs}"

GUNICORN_BIN="${PROJECT_DIR}/venv/bin/gunicorn"
if [[ ! -x "$GUNICORN_BIN" ]]; then
  GUNICORN_BIN="${PROJECT_DIR}/.venv/bin/gunicorn"
fi
if [[ ! -x "$GUNICORN_BIN" ]]; then
  echo "gunicorn executable not found in ${PROJECT_DIR}/venv or ${PROJECT_DIR}/.venv" >&2
  exit 127
fi

exec "$GUNICORN_BIN" src.app:app -c deploy/gunicorn.conf.py
