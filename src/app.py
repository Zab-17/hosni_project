"""FastAPI app: serves the two web pages, handles registration, receives
inbound WhatsApp commands from the bridge, and runs the 5-minute poller.

Launched by start.sh as `uvicorn src.app:app` on port 8000. The Baileys
bridge (Node) runs alongside on 3001 and forwards inbound messages here.
"""

import logging
import re
from contextlib import asynccontextmanager

import httpx
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import BackgroundTasks, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from . import database as db
from .config import settings
from .poller import check_all
from .whatsapp_service import bridge_health, send_message

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("app")

templates = Jinja2Templates(directory="templates")
scheduler = BackgroundScheduler(timezone="UTC")


# ------------------------------------------------------------- helpers

def normalize_phone(raw: str) -> str | None:
    """Return E.164 digits (no +) or None if it doesn't look like a phone.
    Egyptian convenience: a leading 0 on an 11-digit local number becomes 20."""
    digits = re.sub(r"\D", "", raw or "")
    if digits.startswith("00"):
        digits = digits[2:]
    if len(digits) == 11 and digits.startswith("0"):
        digits = "20" + digits[1:]  # 01xxxxxxxxx -> 201xxxxxxxxx
    return digits if re.fullmatch(r"\d{7,15}", digits) else None


def parse_crns(raw: str) -> list[str]:
    """Pull 4-6 digit course reference numbers out of free text."""
    return [m for m in re.findall(r"\d{4,6}", raw or "")]


def registration_confirmation(name: str, phone: str) -> str:
    """The WhatsApp confirmation sent right after a user registers — greets them
    and lists every course they're currently tracking, which also proves their
    WhatsApp number is reachable."""
    rows = db.watches_for_user(phone)
    if rows:
        lines = "\n".join(
            f"• CRN {r['crn']}" + (f" — {r['title']}" if r["title"] else "") for r in rows
        )
    else:
        lines = "(none yet)"
    return (
        f"✅ You're all set, {name}!\n\n"
        f"I'm now watching these courses for an open seat:\n{lines}\n\n"
        f"You'll get a message right here the moment a seat opens. "
        f"Reply *list* anytime to see your courses, or *stop <CRN>* to remove one."
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

    db.upsert_user(norm, first_name.strip(), last_name.strip())
    for crn in crn_list:
        db.add_watch(norm, crn, settings.banner_term)

    # Confirm on WhatsApp (after the page returns) with their tracked courses.
    background_tasks.add_task(send_message, norm, registration_confirmation(first_name.strip(), norm))

    return templates.TemplateResponse(
        request,
        "register.html",
        {
            "term": settings.banner_term,
            "done": True,
            "name": first_name.strip(),
            "crns": crn_list,
        },
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


@app.post("/admin/{key}/broadcast")
def admin_broadcast(key: str, message: str = Form(...)):
    _check_admin(key)
    for u in db.all_users():
        if u["active"]:
            send_message(u["phone"], message)
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
    if low in ("list", "courses", "status"):
        rows = db.watches_for_user(phone)
        if not rows:
            return "You're not tracking any courses. Reply 'track <CRN>' to add one."
        lines = [
            f"• {r['crn']} — {(r['title'] or 'pending first check')} "
            f"({'OPEN ' + str(r['last_seats']) if (r['last_seats'] or 0) > 0 else 'full/—'})"
            for r in rows
        ]
        return "📋 You're tracking:\n" + "\n".join(lines)

    m = re.match(r"(?:stop|remove|untrack)\s+(\d{4,6})", low)
    if m:
        db.remove_watch(phone, m.group(1))
        return f"🗑️ Stopped tracking CRN {m.group(1)}."

    crns = parse_crns(low)
    if crns:
        for crn in crns:
            db.add_watch(phone, crn, settings.banner_term)
        return f"✅ Now tracking: {', '.join(crns)}. I'll text you the moment a seat opens."

    return (
        "Commands:\n"
        "• <CRN> or 'track <CRN>' — start watching a course\n"
        "• 'stop <CRN>' — stop watching one\n"
        "• 'list' — see your courses\n"
        "• 'stop' / 'start' — pause or resume all alerts"
    )


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
