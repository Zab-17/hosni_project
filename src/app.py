"""FastAPI app: serves the two web pages, handles registration, receives
inbound WhatsApp commands from the bridge, and runs the 5-minute poller.

Launched by start.sh as `uvicorn src.app:app` on port 8000. The Baileys
bridge (Node) runs alongside on 3001 and forwards inbound messages here.
"""

import logging
import re
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import BackgroundTasks, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from . import database as db
from .config import settings
from .banner_service import BannerClient
from .poller import check_all, check_courses
from .whatsapp_service import bridge_health, send_message

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("app")

templates = Jinja2Templates(directory="templates")
scheduler = BackgroundScheduler(timezone="UTC")


# ------------------------------------------------------------- helpers

def normalize_phone(raw: str) -> str | None:
    """Normalize any INTERNATIONAL number to bare E.164 digits (country code +
    number, no +). Matches canvas-reminder: strip spaces/+/-, require 7-15 digits.
    Conveniences that don't affect properly-entered international numbers:
      - a leading '00' international prefix is dropped (00<cc>... -> <cc>...)
      - a local Egyptian mobile '01XXXXXXXXX' (11 digits) becomes '20XXXXXXXXX'
    A foreign number entered with its own country code (no leading 0) is kept
    as-is, so US 1415..., UK 44..., UAE 971..., etc. all work."""
    digits = re.sub(r"\D", "", raw or "")
    if digits.startswith("00"):
        digits = digits[2:]
    if len(digits) == 11 and digits.startswith("01"):
        digits = "20" + digits[1:]  # Egyptian local mobile -> E.164
    return digits if re.fullmatch(r"\d{7,15}", digits) else None


def parse_crns(raw: str) -> list[str]:
    """Pull 4-6 digit course reference numbers out of free text."""
    return [m for m in re.findall(r"\d{4,6}", raw or "")]


def _ago(iso: str) -> str:
    """Human 'time since' for the last-check timestamp (e.g. '3 min ago')."""
    try:
        t = datetime.fromisoformat(iso)
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        secs = (datetime.now(timezone.utc) - t).total_seconds()
    except (ValueError, TypeError):
        return "recently"
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{int(secs // 60)} min ago"
    return f"{int(secs // 3600)} h ago"


def clean_name(raw: str) -> str:
    """Sanitize a user-supplied name before it's stored or put in a WhatsApp
    message: first line only, no URLs, letters/spaces/'-. only, capped length.
    Stops attackers from injecting links or multi-line payloads via /register."""
    s = (raw or "").splitlines()[0] if raw else ""
    s = re.sub(r"https?://\S+|www\.\S+", "", s, flags=re.I)
    s = re.sub(r"[^\w\s'\-.]", "", s, flags=re.UNICODE)
    return s.strip()[:40] or "there"


# Simple in-memory per-IP rate limit on registration (resets on restart). Stops
# someone looping the public endpoint to spray WhatsApp messages at many numbers.
_reg_hits: dict[str, list[float]] = defaultdict(list)
_REG_MAX = 5
_REG_WINDOW = 3600  # seconds


def _client_ip(request: Request) -> str:
    return request.headers.get("fly-client-ip") or (request.client.host if request.client else "unknown")


def _rate_limited(ip: str) -> bool:
    now = time.time()
    hits = [t for t in _reg_hits[ip] if now - t < _REG_WINDOW]
    _reg_hits[ip] = hits
    if len(hits) >= _REG_MAX:
        return True
    hits.append(now)
    return False


# Separate, looser limit for the /me lookup page (30/hour per IP).
_me_hits: dict[str, list[float]] = defaultdict(list)


def _me_rate_limited(ip: str) -> bool:
    now = time.time()
    hits = [t for t in _me_hits[ip] if now - t < 3600]
    _me_hits[ip] = hits
    if len(hits) >= 30:
        return True
    hits.append(now)
    return False


def _course_view(r) -> dict:
    """Build the display row for a watched course: seat status + waitlist as
    'open out of total'."""
    seats, cap, cnt = r["last_seats"], r["last_wait_capacity"], r["last_wait_count"]
    if seats is None:
        seat_state = "checking…"
    elif seats > 0:
        seat_state = f"{seats} open"
    else:
        seat_state = "full"
    if cap and cap > 0:
        wait_state = f"{cap - (cnt or 0)} out of {cap}"
    elif cap == 0:
        wait_state = "no waitlist"
    else:
        wait_state = "—"
    return {
        "crn": r["crn"],
        "title": r["title"] or f"CRN {r['crn']}",
        "seats_open": (seats or 0) > 0,
        "seat_state": seat_state,
        "wait_state": wait_state,
    }


def _finish_registration(phone: str, name: str, crns: list[str], term: str) -> None:
    """Runs after the page returns: populate seats for the new courses, then
    confirm on WhatsApp (so the confirmation shows current availability)."""
    check_courses(crns, term)
    send_message(phone, registration_confirmation(name, phone))


def registration_confirmation(name: str, phone: str) -> str:
    """The WhatsApp confirmation sent right after a user registers — greets them
    and lists every course they're currently tracking, which also proves their
    WhatsApp number is reachable."""
    rows = db.watches_for_user(phone)
    parts, open_now = [], False
    for r in rows:
        seats = r["last_seats"]
        title = r["title"] or f"CRN {r['crn']}"
        if seats is None:
            parts.append(f"• {r['crn']} — {title}: ⏳ checking…")
        elif seats > 0:
            open_now = True
            parts.append(f"• {r['crn']} — {title}: ✅ {seats} seat(s) OPEN now!")
        else:
            parts.append(f"• {r['crn']} — {title}: ❌ full")
    body = "\n".join(parts) if parts else "(none yet)"
    tail = ("\n\n🔥 Seats are open right now — go register fast!" if open_now
            else "\n\nI'll message you the moment a seat opens.")
    return (
        f"✅ You're all set, {name}!\n\n"
        f"Watching for you:\n{body}{tail}"
    )


# ------------------------------------------------------------ lifespan

@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    scheduler.add_job(
        check_all,
        trigger=IntervalTrigger(minutes=settings.check_interval_minutes),
        id="seat_poller",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    # Run one check right away on boot so courses don't sit "pending" for up to
    # check_interval_minutes after a (re)start.
    scheduler.add_job(check_all, "date", run_date=datetime.now(timezone.utc),
                      id="startup_check", replace_existing=True)
    scheduler.start()
    log.info("Scheduler started — checking every %d min.", settings.check_interval_minutes)
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(lifespan=lifespan)


# -------------------------------------------------------- public pages

@app.get("/", response_class=HTMLResponse)
def register_page(request: Request):
    return templates.TemplateResponse(
        request,
        "register.html",
        {"term": settings.banner_term, "done": False},
    )


@app.post("/register", response_class=HTMLResponse)
def register(
    request: Request,
    background_tasks: BackgroundTasks,
    first_name: str = Form(...),
    last_name: str = Form(...),
    phone: str = Form(...),
    crns: str = Form(...),
):
    norm = normalize_phone(phone)
    crn_list = parse_crns(crns)
    fn, ln = clean_name(first_name), clean_name(last_name)
    if not norm or not crn_list:
        return templates.TemplateResponse(
            request,
            "register.html",
            {
                "term": settings.banner_term,
                "done": False,
                "error": "Please enter a valid WhatsApp number and at least one course number (CRN).",
            },
            status_code=400,
        )

    # Throttle abuse: cap registrations per IP so the bot can't be used to spray
    # WhatsApp messages at arbitrary numbers.
    if _rate_limited(_client_ip(request)):
        return templates.TemplateResponse(
            request,
            "register.html",
            {
                "term": settings.banner_term,
                "done": False,
                "error": "Too many sign-ups from your connection. Please try again in a little while.",
            },
            status_code=429,
        )

    db.upsert_user(norm, fn, ln)
    for crn in crn_list:
        db.add_watch(norm, crn, settings.banner_term)

    # After the page returns: check the new courses live so their seats land in
    # the DB immediately (a 'check' works at once), THEN send the confirmation
    # reflecting those seats.
    background_tasks.add_task(_finish_registration, norm, fn, crn_list, settings.banner_term)

    return templates.TemplateResponse(
        request,
        "register.html",
        {
            "term": settings.banner_term,
            "done": True,
            "name": fn,
            "crns": crn_list,
        },
    )


# ---------------------------------------------- user "my courses" page

@app.get("/me", response_class=HTMLResponse)
def my_courses_page(request: Request):
    return templates.TemplateResponse(request, "me.html", {"done": False})


@app.post("/me", response_class=HTMLResponse)
def my_courses(request: Request, phone: str = Form(...)):
    if _me_rate_limited(_client_ip(request)):
        return templates.TemplateResponse(
            request, "me.html",
            {"done": False, "error": "Too many lookups — please wait a little."},
            status_code=429,
        )
    norm = normalize_phone(phone)
    user = db.get_user(norm) if norm else None
    if not user:
        return templates.TemplateResponse(
            request, "me.html",
            {"done": False, "error": "No account found for that number. Register first at the sign-up page."},
            status_code=404,
        )
    courses = [_course_view(r) for r in db.watches_for_user(norm)]
    return templates.TemplateResponse(
        request, "me.html",
        {"done": True, "name": user["first_name"], "courses": courses, "active": bool(user["active"])},
    )


# ---------------------------------------------------------- admin page

def _check_admin(key: str) -> None:
    if key != settings.admin_key:
        raise HTTPException(status_code=404)  # don't reveal the page exists


@app.get("/admin/{key}", response_class=HTMLResponse)
def admin_page(request: Request, key: str):
    _check_admin(key)
    return templates.TemplateResponse(
        request,
        "admin.html",
        {
            "key": key,
            "stats": db.counts(),
            "users": db.all_users(),
            "courses": db.all_courses(),
            "health": bridge_health(),
            "term": settings.banner_term,
        },
    )


@app.post("/admin/{key}/delete-user")
def admin_delete_user(key: str, phone: str = Form(...)):
    _check_admin(key)
    db.delete_user(phone)
    return RedirectResponse(f"/admin/{key}", status_code=303)


@app.post("/admin/{key}/add-course")
def admin_add_course(key: str, phone: str = Form(...), crns: str = Form(...)):
    _check_admin(key)
    norm = normalize_phone(phone)
    if norm and db.get_user(norm):
        for crn in parse_crns(crns):
            db.add_watch(norm, crn, settings.banner_term)
    return RedirectResponse(f"/admin/{key}", status_code=303)


@app.post("/admin/{key}/remove-course")
def admin_remove_course(key: str, crn: str = Form(...)):
    _check_admin(key)
    db.remove_course(crn.strip(), settings.banner_term)
    return RedirectResponse(f"/admin/{key}", status_code=303)


def _rate_limited_broadcast(message: str, batch: int = 5, gap_seconds: int = 60) -> None:
    """Send a broadcast in small batches (default 5 per minute) so a freshly-
    linked WhatsApp number doesn't trip spam detection and get banned."""
    phones = [u["phone"] for u in db.all_users() if u["active"]]
    log.info("Broadcast start: %d users, %d every %ds", len(phones), batch, gap_seconds)
    for i in range(0, len(phones), batch):
        sent = sum(1 for p in phones[i:i + batch] if send_message(p, message))
        log.info("Broadcast batch %d-%d: %d sent", i, i + batch, sent)
        if i + batch < len(phones):
            time.sleep(gap_seconds)
    log.info("Broadcast complete: %d users", len(phones))


@app.post("/admin/{key}/broadcast")
def admin_broadcast(key: str, message: str = Form(...)):
    _check_admin(key)
    # Run as a background scheduler job (5/min) so the request returns instantly
    # and the paced sending survives outside the HTTP request lifetime.
    scheduler.add_job(_rate_limited_broadcast, args=[message], id="broadcast", replace_existing=True)
    return RedirectResponse(f"/admin/{key}", status_code=303)


@app.post("/admin/{key}/check-now")
def admin_check_now(key: str):
    _check_admin(key)
    scheduler.add_job(check_all, id="manual_check", replace_existing=True)
    return RedirectResponse(f"/admin/{key}", status_code=303)


# --------------------------------------------- inbound WhatsApp webhook

@app.post("/webhook/whatsapp")
async def whatsapp_webhook(request: Request):
    # Only the local Baileys bridge should call this. When a shared token is
    # configured, require it — blocks public spoofing of inbound messages.
    if settings.bridge_token and request.headers.get("x-bridge-token") != settings.bridge_token:
        raise HTTPException(status_code=403)
    body = await request.json()
    phone = normalize_phone(str(body.get("from", "")))
    text = (body.get("text") or "").strip()
    if not phone or not text:
        return {"ok": True}

    reply = _handle_command(phone, text)
    if reply:
        send_message(phone, reply)
    return {"ok": True}


def _handle_command(phone: str, text: str) -> str | None:
    low = text.lower().strip()
    user = db.get_user(phone)

    if not user:
        # Policy: the bot ONLY messages numbers that exist in the database.
        # An unregistered sender gets no reply at all (registration is web-only).
        log.info("Ignoring inbound from unregistered number %s", phone)
        return None

    if low in ("stop", "unsubscribe", "pause"):
        db.set_active(phone, False)
        return "🔕 Paused. You won't get alerts. Reply 'start' to resume."
    if low in ("start", "resume"):
        db.set_active(phone, True)
        return "🔔 Resumed — you'll be alerted when a seat opens."

    if low in ("list", "courses", "status", "check", "seats", "available", "my courses"):
        # Answer from the LAST stored check (courses.last_seats) — never a live
        # Banner hit, so many users asking at once can't hammer the portal.
        rows = db.watches_for_user(phone)
        if not rows:
            return "You're not tracking any courses. Send 'add <CRN>' to start."
        lines = []
        for r in rows:
            seats = r["last_seats"]
            title = r["title"] or f"CRN {r['crn']}"
            if seats is None:
                state = "⏳ checking…"
            elif seats > 0:
                state = f"✅ {seats} seat(s) open"
            else:
                state = "❌ full"
            lines.append(f"• {r['crn']} — {title}: {state}")
        return "📋 Your courses:\n" + "\n".join(lines)

    # Remove: 'remove/stop/untrack/delete/drop <CRN> [more...]'
    if re.match(r"^(remove|stop|untrack|delete|drop)\b", low):
        crns = parse_crns(low)
        if not crns:
            return "Which course? e.g. 'remove 12345'."
        for crn in crns:
            db.remove_watch(phone, crn)
        return f"🗑️ Stopped tracking: {', '.join(crns)}."

    # Add: 'add/track <CRN>' or a bare CRN. Check Banner now so we confirm it
    # exists, report current seats, and reject CRNs Banner can't read.
    crns = parse_crns(low)
    if crns:
        client = BannerClient()
        out = []
        for crn in crns:
            course = db.get_course(crn, settings.banner_term)
            if course is not None and course["last_checked"] is not None:
                seats, title = course["last_seats"], course["title"]
            else:
                info = client.get_seats(crn, settings.banner_term)
                if info is None:
                    out.append(f"⚠️ {crn} — couldn't read this CRN from Banner. Double-check the number.")
                    continue
                db.update_course(crn, settings.banner_term, info["seats"], title=info["title"],
                                 wait_capacity=info["wait_capacity"], wait_count=info["wait_count"])
                seats, title = info["seats"], info["title"]
            db.add_watch(phone, crn, settings.banner_term)
            label = title or f"CRN {crn}"
            if seats and seats > 0:
                out.append(f"✅ {crn} — {label}: {seats} seat(s) OPEN now!")
            else:
                out.append(f"➕ {crn} — {label}: full — I'll alert you the moment a seat opens.")
        return "\n".join(out)

    return "🤔 I didn't catch that — try one of these:"


# -------------------------------------------------------------- system

@app.get("/admin/{key}/qr", response_class=HTMLResponse)
def qr_proxy(key: str):
    """Proxy the bridge's QR page — gated behind the admin secret so a stranger
    can't scan it during the unlinked window and hijack the WhatsApp account."""
    _check_admin(key)
    try:
        r = httpx.get(f"{settings.baileys_bridge_url}/qr", timeout=10)
        return Response(content=r.text, media_type="text/html")
    except httpx.HTTPError:
        return HTMLResponse("<h1>Bridge not reachable yet — refresh in a moment.</h1>", status_code=503)


@app.get("/health")
def health():
    return {"app": "ok", "bridge": bridge_health(), **db.counts()}
