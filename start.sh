#!/bin/bash
# Launches the Node Baileys bridge + the Python app together.
# Render (and most PaaS) inject $PORT — honor it; default to 8000 locally/Fly.
export PORT="${PORT:-8000}"
export PYTHON_WEBHOOK_URL="http://localhost:${PORT}/webhook/whatsapp"

echo "Clearing stale session keys (pre-keys preserved)..."
rm -f "${WA_SESSION_PATH:-./auth_session}"/session-*.json \
      "${WA_SESSION_PATH:-./auth_session}"/sender-key-*.json 2>/dev/null

# Auto-restart the WhatsApp bridge if it ever crashes.
start_bridge() {
    while true; do
        echo "Starting WhatsApp bridge (Baileys)..."
        node /app/whatsapp-bridge/index.js
        echo "Bridge exited ($?). Restarting in 5s..."
        sleep 5
    done
}
start_bridge &

echo "Starting Python app on port ${PORT}..."
exec /app/venv/bin/uvicorn src.app:app --host 0.0.0.0 --port "${PORT}"
