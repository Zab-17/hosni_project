"""SQLite layer — three tables, exactly as specified:

  users   : who (phone is the WhatsApp identity + primary key)
  courses : the DISTINCT list of CRNs the poller checks (deduped by crn+term)
  watch   : the link between a user and a course (many-to-many)

WAL mode is on so the web requests and the background poller can read/write
concurrently without 'database is locked' errors.
"""

import sqlite3
import threading
from datetime import datetime, timezone

from .config import settings

_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.database_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    with _lock, _connect() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS users (
                phone       TEXT PRIMARY KEY,
                first_name  TEXT,
                last_name   TEXT,
                created_at  TEXT,
                active      INTEGER DEFAULT 1
            )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS courses (
                crn          TEXT,
                term         TEXT,
                title        TEXT,
                last_seats   INTEGER,
                last_checked TEXT,
                PRIMARY KEY (crn, term)
            )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS watch (
                phone      TEXT,
                crn        TEXT,
                term       TEXT,
                created_at TEXT,
                PRIMARY KEY (phone, crn, term)
            )"""
        )
        c.commit()


# ---------------------------------------------------------------- users

def upsert_user(phone: str, first_name: str, last_name: str) -> None:
    with _lock, _connect() as c:
        c.execute(
            """INSERT INTO users (phone, first_name, last_name, created_at, active)
               VALUES (?, ?, ?, ?, 1)
               ON CONFLICT(phone) DO UPDATE SET
                 first_name=excluded.first_name,
                 last_name=excluded.last_name,
                 active=1""",
            (phone, first_name, last_name, _now()),
        )
        c.commit()


def get_user(phone: str):
    with _connect() as c:
        return c.execute("SELECT * FROM users WHERE phone=?", (phone,)).fetchone()


def set_active(phone: str, active: bool) -> None:
    with _lock, _connect() as c:
        c.execute("UPDATE users SET active=? WHERE phone=?", (1 if active else 0, phone))
        c.commit()


def delete_user(phone: str) -> None:
    with _lock, _connect() as c:
        c.execute("DELETE FROM watch WHERE phone=?", (phone,))
        c.execute("DELETE FROM users WHERE phone=?", (phone,))
        c.commit()
    _prune_orphan_courses()


# ------------------------------------------------------------- courses

def ensure_course(crn: str, term: str) -> None:
    """Add a CRN to the distinct check-list if it isn't there yet.
    last_seats stays NULL until the first poll, so the first check never
    fires a false 'seat opened' alert."""
    with _lock, _connect() as c:
        c.execute(
            """INSERT INTO courses (crn, term, last_seats, last_checked)
               VALUES (?, ?, NULL, NULL)
               ON CONFLICT(crn, term) DO NOTHING""",
            (crn, term),
        )
        c.commit()


def update_course(crn: str, term: str, seats: int, title: str | None = None) -> None:
    with _lock, _connect() as c:
        if title:
            c.execute(
                "UPDATE courses SET last_seats=?, last_checked=?, title=? WHERE crn=? AND term=?",
                (seats, _now(), title, crn, term),
            )
        else:
            c.execute(
                "UPDATE courses SET last_seats=?, last_checked=? WHERE crn=? AND term=?",
                (seats, _now(), crn, term),
            )
        c.commit()


def get_course(crn: str, term: str):
    with _connect() as c:
        return c.execute("SELECT * FROM courses WHERE crn=? AND term=?", (crn, term)).fetchone()


def distinct_courses() -> list[sqlite3.Row]:
    """The poller's work-list: every distinct course someone is watching."""
    with _connect() as c:
        return c.execute(
            """SELECT c.crn, c.term, c.title, c.last_seats
               FROM courses c
               WHERE EXISTS (SELECT 1 FROM watch w
                             WHERE w.crn=c.crn AND w.term=c.term)
               ORDER BY c.term, c.crn"""
        ).fetchall()


def remove_course(crn: str, term: str) -> None:
    """Remove a course entirely — every watch on it and the course row itself.
    Used by admin to purge a bad/invalid CRN for everyone."""
    with _lock, _connect() as c:
        c.execute("DELETE FROM watch WHERE crn=? AND term=?", (crn, term))
        c.execute("DELETE FROM courses WHERE crn=? AND term=?", (crn, term))
        c.commit()


def _prune_orphan_courses() -> None:
    """Drop courses nobody watches anymore so we stop checking them."""
    with _lock, _connect() as c:
        c.execute(
            """DELETE FROM courses
               WHERE NOT EXISTS (SELECT 1 FROM watch w
                                 WHERE w.crn=courses.crn AND w.term=courses.term)"""
        )
        c.commit()


# --------------------------------------------------------------- watch

def add_watch(phone: str, crn: str, term: str) -> None:
    ensure_course(crn, term)
    with _lock, _connect() as c:
        c.execute(
            """INSERT INTO watch (phone, crn, term, created_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(phone, crn, term) DO NOTHING""",
            (phone, crn, term, _now()),
        )
        c.commit()


def remove_watch(phone: str, crn: str, term: str | None = None) -> None:
    with _lock, _connect() as c:
        if term:
            c.execute("DELETE FROM watch WHERE phone=? AND crn=? AND term=?", (phone, crn, term))
        else:
            c.execute("DELETE FROM watch WHERE phone=? AND crn=?", (phone, crn))
        c.commit()
    _prune_orphan_courses()


def watches_for_user(phone: str) -> list[sqlite3.Row]:
    with _connect() as c:
        return c.execute(
            """SELECT w.crn, w.term, c.title, c.last_seats, c.last_checked
               FROM watch w LEFT JOIN courses c
                 ON c.crn=w.crn AND c.term=w.term
               WHERE w.phone=? ORDER BY w.crn""",
            (phone,),
        ).fetchall()


def subscribers_for(crn: str, term: str) -> list[str]:
    """Who do we text when this seat opens — the whole point of the app."""
    with _connect() as c:
        rows = c.execute(
            """SELECT w.phone FROM watch w
               JOIN users u ON u.phone=w.phone
               WHERE w.crn=? AND w.term=? AND u.active=1""",
            (crn, term),
        ).fetchall()
    return [r["phone"] for r in rows]


# --------------------------------------------------------------- admin

def all_users() -> list[sqlite3.Row]:
    with _connect() as c:
        return c.execute(
            """SELECT u.*, COUNT(w.crn) AS course_count
               FROM users u LEFT JOIN watch w ON w.phone=u.phone
               GROUP BY u.phone ORDER BY u.created_at DESC"""
        ).fetchall()


def all_courses() -> list[sqlite3.Row]:
    with _connect() as c:
        return c.execute(
            """SELECT c.*, COUNT(w.phone) AS watcher_count
               FROM courses c LEFT JOIN watch w
                 ON w.crn=c.crn AND w.term=c.term
               GROUP BY c.crn, c.term ORDER BY c.last_checked DESC NULLS LAST"""
        ).fetchall()


def counts() -> dict:
    with _connect() as c:
        u = c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
        co = c.execute("SELECT COUNT(*) n FROM courses").fetchone()["n"]
        w = c.execute("SELECT COUNT(*) n FROM watch").fetchone()["n"]
    return {"users": u, "courses": co, "watches": w}
