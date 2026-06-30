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

# App code (includes start.sh)
COPY . .
RUN chmod +x /app/start.sh

EXPOSE 8000
CMD ["/app/start.sh"]
