#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_DIR"

if [[ ! -d .git && ! -d ../.git ]]; then
  echo "Refusing to run outside a Wei cloud checkout: $PROJECT_DIR" >&2
  exit 1
fi

DRY_RUN=0
REMOVE_LOCAL_LAUNCHER=0

for arg in "$@"; do
  case "$arg" in
    --dry-run)
      DRY_RUN=1
      ;;
    --remove-local-launcher)
      REMOVE_LOCAL_LAUNCHER=1
      ;;
    *)
      echo "Unknown argument: $arg" >&2
      echo "Usage: $0 [--dry-run] [--remove-local-launcher]" >&2
      exit 1
      ;;
  esac
done

remove_path() {
  local path="$1"
  if [[ ! -e "$path" ]]; then
    return 0
  fi
  if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "[dry-run] remove $path"
    return 0
  fi
  rm -rf -- "$path"
  echo "removed $path"
}

shopt -s nullglob

# Accidental or purely local shell history / temp artifacts seen on servers.
remove_path "tash push -m server-local-gitignore"

# Root-level local test logs and backups that are not used by deploy/start_server.sh.
for path in \
  flask_langgraph.log.local.bak \
  uvicorn_local*.log \
  uvicorn_local_check*.log \
  uvicorn_local_test*.log
do
  for match in $path; do
    remove_path "$match"
  done
done

# Cloud runtime artifacts.
remove_path ".playwright-mcp"
remove_path "tmp"
remove_path "__pycache__"

# Local-only runtime artifacts when the full repo, not just cloud/, exists on the server.
remove_path "../local_launcher/runtime_env.local"
remove_path "../local_launcher/launcher_logs"
remove_path "../local_launcher/.playwright-mcp"

# Optional: remove the tracked local-only launcher code from the server worktree.
if [[ "$REMOVE_LOCAL_LAUNCHER" -eq 1 ]]; then
  remove_path "../local_launcher"
fi

echo "Server local cleanup complete."
