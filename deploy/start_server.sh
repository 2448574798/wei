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

exec gunicorn src.app:app -c deploy/gunicorn.conf.py
