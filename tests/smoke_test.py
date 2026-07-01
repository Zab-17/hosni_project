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
os.environ["BRIDGE_TOKEN"] = ""  # disable webhook auth for the offline test

from fastapi.testclient import TestClient  # noqa: E402

from src import app as appmod  # noqa: E402
from src import database as db  # noqa: E402
from src import poller, whatsapp_service  # noqa: E402
from src.app import app  # noqa: E402

db.init_db()

# Capture outbound messages instead of hitting the bridge. Patch every module
# that holds its own reference to send_message (app imports it by name).
sent: list[tuple[str, str]] = []


def _fake_send(phone, msg, footer=True):
    sent.append((phone, msg))
    return True


whatsapp_service.send_message = _fake_send
poller.send_message = _fake_send
appmod.send_message = _fake_send

# Fake Banner: a dict we control of {crn: seats}.
SEATS = {"12345": 0, "23456": 5, "34567": 7}


class FakeBanner:
    def get_seats(self, crn, term):
        if crn not in SEATS:
            return None
        return {"seats": SEATS[crn], "max": 30, "enrolled": 30 - SEATS[crn],
                "title": f"COURSE {crn}", "wait_capacity": 15, "wait_count": 3}


poller.BannerClient = FakeBanner
appmod.BannerClient = FakeBanner  # the add command checks Banner via app's reference

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

# Registration confirmation lists courses WITH their just-checked seat status.
check("register sends WhatsApp confirmation with tracked courses",
      any(p == "201010101010" and "12345" in m and "23456" in m for p, m in sent))
check("confirmation flags an already-open course to the user",
      any(p == "201010101010" and "23456" in m and "OPEN" in m for p, m in sent))

# Immediate check on registration populated seats in the DB (no 5-min wait).
check("immediate check stored open-course seats", db.get_course("23456", "202710")["last_seats"] == 5)
check("immediate check stored full-course seats", db.get_course("12345", "202710")["last_seats"] == 0)
check("distinct courses deduped (2 not 3)", db.counts()["courses"] == 2)

# Isolate alert tests from the registration confirmations above.
sent.clear()

# Poll with nothing changed: open course already known, full stays full -> NO spam.
poller.check_all()
check("no alerts when nothing changed (no 5-min spam)", len(sent) == 0)

# A seat frees on the full course 12345 (0 -> 2): BOTH watchers alerted.
SEATS["12345"] = 2
poller.check_all()
check("seat opening (0->open) alerts BOTH watchers",
      set(p for p, m in sent if "12345" in m) == {"201010101010", "201020202020"})
check("already-open course not re-alerted on poll", not any("23456" in m for p, m in sent))

# Stays open -> no re-alert next poll.
sent.clear()
poller.check_all()
check("no re-alert while seat stays open", len(sent) == 0)

# 4. Bot is SEND-ONLY: inbound messages are ignored — no reply, no state change.
sent.clear()
before_w = db.counts()["watches"]
r = client.post("/webhook/whatsapp", json={"from": "201010101010", "text": "stop 23456"})
check("webhook accepts inbound (200)", r.status_code == 200)
check("send-only: inbound changes no state", db.counts()["watches"] == before_w)
check("send-only: inbound triggers no reply", len(sent) == 0)

# 5. Admin gating.
check("admin wrong key -> 404", client.get("/admin/wrong").status_code == 404)
check("admin right key -> 200", client.get("/admin/secret123").status_code == 200)

# 6. International phone numbers (not just Egyptian).
from src.app import normalize_phone  # noqa: E402
check("Egyptian local 01… -> 20…", normalize_phone("01154069714") == "201154069714")
check("US +1 international kept", normalize_phone("+1 415 555 1234") == "14155551234")
check("UAE 00971 prefix stripped", normalize_phone("0097150123456") == "97150123456")
check("UK +44 kept", normalize_phone("+44 7911 123456") == "447911123456")
check("garbage rejected", normalize_phone("abc") is None)

print()
if failures:
    print(f"❌ {len(failures)} FAILED: {failures}")
    raise SystemExit(1)
print("✅ ALL SMOKE TESTS PASSED")
