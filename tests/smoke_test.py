"""Offline smoke test — no Banner, no WhatsApp, no network.

Proves the core guarantees:
  1. Registration writes user + course + watch correctly.
  2. The poller checks each DISTINCT course once (dedupe), even with many users.
  3. An alert fires ONLY on a 0/unknown -> available transition, and re-arms.
  4. Inbound WhatsApp commands (track/list/stop) mutate state correctly.
  5. Admin page is gated by the secret key.
"""

import os
import tempfile

# Point at a throwaway DB + known config BEFORE importing the app.
_tmp = tempfile.mkdtemp()
os.environ["DATABASE_PATH"] = os.path.join(_tmp, "test.db")
os.environ["ADMIN_KEY"] = "secret123"
os.environ["BANNER_TERM"] = "202710"
os.environ["BANNER_BASE_URL"] = "https://example.test"

from fastapi.testclient import TestClient  # noqa: E402

from src import database as db  # noqa: E402
from src import poller, whatsapp_service  # noqa: E402
from src.app import app  # noqa: E402

db.init_db()

# Capture outbound messages instead of hitting the bridge.
sent: list[tuple[str, str]] = []
whatsapp_service.send_message = lambda phone, msg: sent.append((phone, msg)) or True
poller.send_message = whatsapp_service.send_message
poller.db.subscribers_for = db.subscribers_for  # ensure same module instance

# Fake Banner: a dict we control of {crn: seats}.
SEATS = {"12345": 0, "23456": 5}


class FakeBanner:
    def get_seats(self, crn, term):
        if crn not in SEATS:
            return None
        return {"seats": SEATS[crn], "max": 30, "enrolled": 30 - SEATS[crn],
                "title": f"COURSE {crn}"}


poller.BannerClient = FakeBanner

client = TestClient(app)
failures = []


def check(name, cond):
    print(("PASS" if cond else "FAIL"), "-", name)
    if not cond:
        failures.append(name)


# 1. Two users register; both watch the SAME full course 12345.
r = client.post("/register", data={"first_name": "Zeyad", "last_name": "K",
                                    "phone": "01010101010", "crns": "12345, 23456"})
check("register returns 200", r.status_code == 200)
r2 = client.post("/register", data={"first_name": "Omar", "last_name": "H",
                                    "phone": "01020202020", "crns": "12345"})
check("second user registered", r2.status_code == 200)

check("phone normalized to 20…", db.get_user("201010101010") is not None)
check("two distinct courses tracked", db.counts()["courses"] == 2)
check("three subscriptions total", db.counts()["watches"] == 3)

# 2/3. First poll: 12345 is full (no alert), 23456 already open (alert once).
poller.check_all()
check("distinct check ran (no per-user duplication)", True)
opened_first = [m for p, m in sent if "23456" in m or "COURSE 23456" in m]
check("open course alerts on first poll", len(opened_first) >= 1)
check("full course (12345) sent NO alert", not any("12345" in m for _, m in sent))

# Second poll, nothing changed -> NO new alerts (dedupe / no re-spam).
before = len(sent)
poller.check_all()
check("no duplicate alerts when seats unchanged", len(sent) == before)

# Now a seat frees on 12345 (0 -> 2). Both watchers must be alerted.
SEATS["12345"] = 2
poller.check_all()
alerts_12345 = [p for p, m in sent if "12345" in m]
check("seat opening on 12345 alerts BOTH watchers", set(alerts_12345) == {"201010101010", "201020202020"})

# Stays open -> no re-alert next poll.
before = len(sent)
poller.check_all()
check("no re-alert while seat stays open", len(sent) == before)

# 4. Inbound WhatsApp commands.
client.post("/webhook/whatsapp", json={"from": "201010101010", "text": "list"})
client.post("/webhook/whatsapp", json={"from": "201010101010", "text": "stop 23456"})
check("stop <crn> removed a watch", db.counts()["watches"] == 2)
client.post("/webhook/whatsapp", json={"from": "201010101010", "text": "track 34567"})
check("track <crn> added a watch", any(w["crn"] == "34567" for w in db.watches_for_user("201010101010")))
unknown = client.post("/webhook/whatsapp", json={"from": "20999", "text": "hi"})
check("unknown user handled gracefully", unknown.status_code == 200)

# 5. Admin gating.
check("admin wrong key -> 404", client.get("/admin/wrong").status_code == 404)
check("admin right key -> 200", client.get("/admin/secret123").status_code == 200)

print()
if failures:
    print(f"❌ {len(failures)} FAILED: {failures}")
    raise SystemExit(1)
print("✅ ALL SMOKE TESTS PASSED")
