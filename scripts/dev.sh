#!/usr/bin/env bash
# Start the LeadFlow API and the Streamlit dashboard together, from one terminal.
# Ctrl+C stops both.
#
#   bash scripts/dev.sh                 # API on 8001, dashboard on 8501
#   API_PORT=8002 bash scripts/dev.sh   # use another API port
set -euo pipefail
cd "$(dirname "$0")/.."

API_PORT="${API_PORT:-8001}"
UI_PORT="${UI_PORT:-8501}"
API_URL="http://127.0.0.1:${API_PORT}"

port_free() {
  if lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "Port $1 is already in use by:" >&2
    lsof -nP -iTCP:"$1" -sTCP:LISTEN >&2
    echo "Stop that program, or choose another port (e.g. API_PORT=8002)." >&2
    return 1
  fi
}
port_free "$API_PORT"
port_free "$UI_PORT"

echo "Starting LeadFlow API on ${API_URL} ..."
uv run uvicorn --factory leadflow_ai.main:create_app --host 127.0.0.1 --port "$API_PORT" &
API_PID=$!

stop_api() {
  if kill -0 "$API_PID" 2>/dev/null; then
    echo "Stopping LeadFlow API ..."
    kill "$API_PID" 2>/dev/null || true
    wait "$API_PID" 2>/dev/null || true
  fi
}
trap stop_api EXIT INT TERM

# Wait up to 15 s for the API to answer /health before starting the dashboard.
for _ in $(seq 1 30); do
  if curl -fsS "${API_URL}/health" >/dev/null 2>&1; then
    break
  fi
  if ! kill -0 "$API_PID" 2>/dev/null; then
    echo "The API stopped during startup; see the messages above." >&2
    exit 1
  fi
  sleep 0.5
done
if ! curl -fsS "${API_URL}/health" >/dev/null 2>&1; then
  echo "The API did not answer ${API_URL}/health within 15 seconds." >&2
  exit 1
fi

echo "API ready. Dashboard: http://127.0.0.1:${UI_PORT}  (Ctrl+C stops both)"
# The environment variable overrides LEADFLOW_API_URL from .env, so both always agree.
LEADFLOW_API_URL="$API_URL" uv run --group ui streamlit run dashboard/app.py \
  --server.port "$UI_PORT"
