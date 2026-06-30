"""Thin client to the Baileys bridge (whatsapp-bridge/index.js on :3001).

Hard lesson from canvas-reminder: keep the send timeout SHORT (30s, not 180s)
so one slow/zombie send doesn't stall the whole notification fan-out. On a
503 'zombie' we retry once after a beat — the bridge self-heals via its own
reconnect logic, so a second attempt usually lands."""

import time

import httpx

from .config import settings

# Appended to every outbound message so the user always sees what they can do.
COMMAND_FOOTER = (
    "\n\n— — — — —\n"
    "💬 *Reply anytime:*\n"
    "• *add <CRN>* — watch a course\n"
    "• *remove <CRN>* — stop watching one\n"
    "• *check* — your courses & seats\n"
    "• *stop* / *start* — pause / resume alerts"
)


def send_message(phone: str, message: str, footer: bool = True) -> bool:
    if footer and "Reply anytime" not in message:
        message = message + COMMAND_FOOTER
    url = f"{settings.baileys_bridge_url}/send"
    body = {"to": phone, "message": message}

    for attempt in range(2):
        try:
            r = httpx.post(url, json=body, timeout=30)
            if r.status_code == 200:
                return True
            # 503 = bridge not connected / zombie socket: pause, retry once
            if r.status_code == 503 and attempt == 0:
                time.sleep(3)
                continue
            return False
        except httpx.HTTPError:
            if attempt == 0:
                time.sleep(3)
                continue
            return False
    return False


def bridge_health() -> dict | None:
    try:
        r = httpx.get(f"{settings.baileys_bridge_url}/health", timeout=10)
        return r.json()
    except httpx.HTTPError:
        return None
