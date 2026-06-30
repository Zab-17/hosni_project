FROM node:20-slim

# Python only — Baileys talks WhatsApp over a WebSocket, no Chromium needed.
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip python3-venv \
    ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Node deps for the WhatsApp bridge (cached layer)
COPY whatsapp-bridge/package.json ./whatsapp-bridge/
RUN cd whatsapp-bridge && npm install --production

# Python deps
COPY requirements.txt .
RUN python3 -m venv /app/venv && /app/venv/bin/pip install --no-cache-dir -r requirements.txt

# App code
COPY . .

# Launcher: clear stale Signal session keys (KEEP pre-keys!), run the Node
# bridge under a while-true auto-restart loop, then exec the Python app.
RUN printf '#!/bin/bash\n\n\
echo "Clearing stale session keys (pre-keys preserved)..."\n\
rm -f ${WA_SESSION_PATH:-./auth_session}/session-*.json ${WA_SESSION_PATH:-./auth_session}/sender-key-*.json 2>/dev/null\n\n\
start_bridge() {\n\
    while true; do\n\
        echo "Starting WhatsApp bridge (Baileys)..."\n\
        node /app/whatsapp-bridge/index.js\n\
        echo "Bridge exited ($?). Restarting in 5s..."\n\
        sleep 5\n\
    done\n\
}\n\
start_bridge &\n\n\
echo "Starting Python app..."\n\
exec /app/venv/bin/uvicorn src.app:app --host 0.0.0.0 --port 8000\n' \
    > /app/start.sh && chmod +x /app/start.sh

EXPOSE 8000
CMD ["/app/start.sh"]
