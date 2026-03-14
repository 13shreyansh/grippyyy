#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PID_FILE="$ROOT_DIR/artifacts/demo_session/current_server.pid"

if [[ -f "$PID_FILE" ]]; then
  PID="$(cat "$PID_FILE")"
  if kill -0 "$PID" >/dev/null 2>&1; then
    kill "$PID"
    echo "Stopped demo server process $PID."
  else
    echo "PID file exists but process $PID is not running."
  fi
  rm -f "$PID_FILE"
  exit 0
fi

PIDS="$(lsof -tiTCP:8000 -sTCP:LISTEN || true)"
if [[ -n "$PIDS" ]]; then
  echo "$PIDS" | xargs kill
  echo "Stopped process(es) listening on port 8000."
  exit 0
fi

echo "No demo server process found."
