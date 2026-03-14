#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BASE_URL="http://127.0.0.1:8000"
SESSION_ROOT="$ROOT_DIR/artifacts/demo_session"
STAMP="$(date +%Y%m%d_%H%M%S)"
SESSION_DIR="$SESSION_ROOT/$STAMP"
LOG_FILE="$SESSION_DIR/server.log"
PID_FILE="$SESSION_ROOT/current_server.pid"
URL_FILE="$SESSION_DIR/urls.txt"

mkdir -p "$SESSION_DIR"

is_port_live() {
  lsof -nP -iTCP:8000 -sTCP:LISTEN >/dev/null 2>&1
}

stop_existing_server() {
  local pids
  pids="$(lsof -tiTCP:8000 -sTCP:LISTEN || true)"
  if [[ -n "$pids" ]]; then
    echo "$pids" | xargs kill
    sleep 1
  fi
}

wait_for_health() {
  local max_attempts=60
  local attempt=1
  while [[ "$attempt" -le "$max_attempts" ]]; do
    if curl -fsS "$BASE_URL/api/health" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
    attempt=$((attempt + 1))
  done
  return 1
}

prewarm_routes() {
  curl -fsS "$BASE_URL/" >/dev/null
  curl -fsS "$BASE_URL/chat" >/dev/null
  curl -fsS "$BASE_URL/demo" >/dev/null
}

seed_demo_profile() {
  curl -fsS "$BASE_URL/api/v3/profile" \
    -H "Content-Type: application/json" \
    -X POST \
    -d '{
      "data": {
        "full_name": "John Smith",
        "given_name": "John",
        "family_name": "Smith",
        "email": "john.smith@example.com",
        "phone": "+6591234567",
        "phone_mobile": "91234567",
        "salutation": "Mr",
        "gender": "Male",
        "year_of_birth": "1990",
        "nric": "S1234567A",
        "nationality": "Singaporean",
        "race": "Chinese",
        "block": "123",
        "street": "Orchard Road",
        "unit": "05-01",
        "unit_number": "05-01",
        "building": "The Orchid",
        "building_name": "The Orchid",
        "postal_code": "238858",
        "country": "Singapore",
        "vendor_name": "Shopee Singapore",
        "vendor_phone": "62708100",
        "vendor_email": "help@support.shopee.sg",
        "website": "https://shopee.sg",
        "vendor_block": "1",
        "vendor_street": "Fusionopolis Place",
        "vendor_unit": "17-10",
        "vendor_building": "Galaxis",
        "vendor_postal_code": "138522",
        "case_nature_of_complaint": "Refund issue",
        "case_industry": "Computers",
        "transaction_type": "Purchase",
        "transaction_date": "01/03/2026",
        "desired_outcome": "Quantum of claim",
        "amount": "1299",
        "complaint_description": "Shopee Singapore sold me a defective laptop and refused a refund after repeated follow-up.",
        "association_member": "false",
        "case_terms_consent": "true",
        "case_marketing_consent": "false"
      }
    }' >/dev/null
}

open_demo_tabs() {
  if [[ -d "/Applications/Google Chrome.app" ]]; then
    open -a "Google Chrome" "$BASE_URL/"
    open -a "Google Chrome" "$BASE_URL/chat?new=1"
  else
    open "$BASE_URL/"
    open "$BASE_URL/chat?new=1"
  fi
}

if is_port_live; then
  echo "Port 8000 is already serving. Restarting in demo mode..."
  stop_existing_server
fi

echo "Starting Grippy demo server..."
ROOT_DIR="$ROOT_DIR" LOG_FILE="$LOG_FILE" PID_FILE="$PID_FILE" \
GRIPPY_BROWSER_HEADLESS="false" GRIPPY_BROWSER_SLOWMO_MS="120" python - <<'PY'
import os
import subprocess
from pathlib import Path

root = Path(os.environ["ROOT_DIR"])
log_path = Path(os.environ["LOG_FILE"])
pid_path = Path(os.environ["PID_FILE"])

with log_path.open("w", encoding="utf-8") as log_file:
    proc = subprocess.Popen(
        ["python", "-m", "uvicorn", "app:app", "--port", "8000"],
        cwd=root,
        stdin=subprocess.DEVNULL,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        env=os.environ.copy(),
    )
    pid_path.write_text(f"{proc.pid}\n", encoding="utf-8")
PY

if ! wait_for_health; then
  echo "Server did not become healthy in time."
  echo "Check: $LOG_FILE"
  exit 1
fi

prewarm_routes
seed_demo_profile

cat >"$URL_FILE" <<EOF
$BASE_URL/
$BASE_URL/chat?new=1
$BASE_URL/demo
EOF

open_demo_tabs

echo
echo "Grippy demo session is ready."
echo "Base URL: $BASE_URL"
echo "Session dir: $SESSION_DIR"
echo "Server log: $LOG_FILE"
if [[ -f "$PID_FILE" ]]; then
  echo "PID file: $PID_FILE"
fi
echo "Tabs opened: /, /chat?new=1"
echo "Stop command: $ROOT_DIR/scripts/stop_demo_session.sh"
