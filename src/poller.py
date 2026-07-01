"""The heart of the app: check every DISTINCT course once, fan results out.

Alert rule (the safe, low-volume default):
  fire only when seats transition into 'available' from full/unknown —
  i.e. seats > 0 AND previous last_seats was 0 or NULL.
  After firing, last_seats is set to the new (>0) value, so we don't re-alert
  every 5 minutes while the seat stays open. It re-arms automatically once the
  course fills again (seats back to 0) and re-opens.

This keeps message volume tiny — important because the WhatsApp account is an
unofficial Baileys client and high volume risks a ban.
"""

import logging

from . import database as db
from .banner_service import BannerClient
from .config import settings
from .whatsapp_service import send_message

log = logging.getLogger("poller")


def _registration_link(term: str) -> str:
    base = settings.banner_base_url.rstrip("/")
    return f"{base}{settings.banner_path_prefix}/registration"


def _alert_text(crn: str, term: str, info: dict) -> str:
    title = info.get("title") or f"CRN {crn}"
    return (
        f"🎉 A seat just opened!\n\n"
        f"{title}\n"
        f"CRN: {crn}  |  Term: {term}\n"
        f"Seats available: {info['seats']} / {info['max']}\n\n"
        f"Register NOW before it fills:\n{_registration_link(term)}"
    )


def check_courses(crns: list[str], term: str) -> None:
    """Check specific CRNs right now and store their seat counts — called when a
    user registers or adds a course, so the data is in the database immediately
    and a 'check' command can answer without waiting for the 5-minute poll.
    Skips any CRN already checked (don't re-hit Banner for a popular course)."""
    client = BannerClient()
    for crn in crns:
        course = db.get_course(crn, term)
        if course is not None and course["last_checked"] is not None:
            continue
        info = client.get_seats(crn, term)
        if info:
            db.update_course(crn, term, info["seats"], title=info["title"],
                             wait_capacity=info["wait_capacity"], wait_count=info["wait_count"])
            log.info("Immediate check CRN %s: %d seats available", crn, info["seats"])


def check_all() -> None:
    courses = db.distinct_courses()
    if not courses:
        log.info("No courses being watched — nothing to check.")
        return

    client = BannerClient()
    checked = alerts = 0

    # One fresh Banner session PER course (== reload before each search), so a
    # cached previous search can never make a real CRN look empty. Verified live.
    for row in courses:
        crn, term, prev = row["crn"], row["term"], row["last_seats"]
        info = client.get_seats(crn, term)
        if info is None:
            continue
        checked += 1
        seats = info["seats"]
        db.update_course(crn, term, seats, title=info["title"],
                         wait_capacity=info["wait_capacity"], wait_count=info["wait_count"])

        if seats > 0 and (prev is None or prev == 0):
            alerts += _notify(crn, term, info)

    log.info("Poll done: %d courses checked, %d seat-alerts sent.", checked, alerts)


def _notify(crn: str, term: str, info: dict) -> int:
    subs = db.subscribers_for(crn, term)
    if not subs:
        return 0
    text = _alert_text(crn, term, info)
    sent = 0
    for phone in subs:
        if send_message(phone, text):
            sent += 1
        else:
            log.warning("Failed to alert %s about CRN %s", phone, crn)
    log.info("CRN %s opened (%d seats) -> alerted %d/%d watchers", crn, info["seats"], sent, len(subs))
    return sent
