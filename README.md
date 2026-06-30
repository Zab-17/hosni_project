# AUC Course Seat Watcher (Banner 9)

Watches AUC Banner 9 course sections and sends a **WhatsApp message** the moment
a seat frees up. Students register on a web page (name + WhatsApp number + course
numbers); the server checks each **distinct** course every few minutes and alerts
everyone watching it.

## Why it scales to 500+ users on a tiny box

- **No Chromium.** WhatsApp is sent via [Baileys](https://github.com/WhiskeySockets/Baileys)
  (a direct WebSocket client, ~80 MB) and seat-checking is plain HTTP. Both
  browser-heavy approaches were tried-and-dropped in the sibling `canvas-reminder`
  project — see its `LESSONS_LEARNED.txt`.
- **Dedupe by course, not by user.** 500 students fighting over 150 sections =
  **150 checks per cycle**, not 144,000. Each result fans out to its subscribers.

## Architecture

```
Browser ─┐                         ┌─ Banner 9 (plain httpx, no login)
 web page │                        │
         ▼                         ▼
   FastAPI app (:8000) ──poll every 5 min──> seat changed? ──┐
   src/app.py + poller.py                                     │
         ▲  localhost HTTP                                    ▼
         │                                          Baileys bridge (:3001)
   inbound WhatsApp cmds ◄───────────────────────── whatsapp-bridge/index.js
                                                            │
                                                       your WhatsApp ──> users
```

| File | Role |
|------|------|
| `src/config.py` | All settings, loaded from `.env` |
| `src/database.py` | SQLite: `users`, `courses` (distinct check-list), `watch` (link) |
| `src/banner_service.py` | Banner session handshake + seat reader |
| `src/poller.py` | Checks distinct courses, fans out alerts on `0 → open` |
| `src/whatsapp_service.py` | Sends via the Baileys bridge |
| `src/app.py` | Web pages, registration, inbound-command webhook, scheduler |
| `templates/` | `register.html` (public), `admin.html` (secret URL) |
| `whatsapp-bridge/` | Baileys WhatsApp bridge (copied & hardened from canvas-reminder) |

## Setup

1. **Configure** — copy `.env.example` to `.env` and fill in:
   - `BANNER_BASE_URL` — the AUC Banner host (the one real unknown).
   - `BANNER_TERM` — active term code, e.g. `202710`.
   - `ADMIN_KEY` — secret slug for the admin page (`openssl rand -hex 16`).

2. **Confirm Banner works over plain HTTP** (do this first):
   ```bash
   python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
   .venv/bin/python -m tools.test_banner <a-real-CRN>
   ```
   Green = no browser needed. If it can't read seats, it tells you what to fix.

3. **Run the WhatsApp bridge** (in one terminal):
   ```bash
   cd whatsapp-bridge && npm install && node index.js
   ```
   Open <http://localhost:3001/qr> and scan with WhatsApp → Linked Devices.

4. **Run the app** (another terminal):
   ```bash
   .venv/bin/uvicorn src.app:app --port 8000
   ```
   - Register: <http://localhost:8000/>
   - Admin: `http://localhost:8000/admin/<ADMIN_KEY>`

## WhatsApp commands (for registered users)

`track 12345` · `stop 12345` · `list` · `stop` / `start`

## Deploy (Fly.io — same as canvas-reminder)

```bash
fly launch --no-deploy          # uses the included fly.toml
fly volumes create seatwatch_data --size 1
fly secrets set ADMIN_KEY=... BANNER_BASE_URL=https://...
fly deploy
fly open /qr                    # scan once to link WhatsApp
```

## Tests

```bash
.venv/bin/python -m tests.smoke_test   # offline; proves dedupe + alert logic
```

## ⚠️ The one real risk

One WhatsApp number blasting 500 strangers via an **unofficial** client can get
the number flagged/banned. The alert rule is deliberately low-volume (only on
`0 → open`, never repeats), but for production consider a dedicated number and/or
the official WhatsApp Business API. See the sibling project's lessons on this.
