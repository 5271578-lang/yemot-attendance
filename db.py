import json
import os
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "attendance.db"))
TZ = ZoneInfo(os.environ.get("TIMEZONE", "Asia/Jerusalem"))

ROLES = ("employee", "onsite_manager", "tech_manager")
MANAGER_ROLES = ("onsite_manager", "tech_manager")
DEFAULT_RATES = {"employee": 16.0, "onsite_manager": 25.0, "tech_manager": None}

SCHEMA = """
CREATE TABLE IF NOT EXISTS employees (
    id INTEGER PRIMARY KEY,
    emp_number TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    phone TEXT,
    pin TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'employee' CHECK (role IN ('employee','onsite_manager','tech_manager')),
    shift TEXT,
    shuttle_group INTEGER,
    hourly_rate REAL,
    is_driver INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS attendance (
    id INTEGER PRIMARY KEY,
    employee_id INTEGER NOT NULL REFERENCES employees(id),
    clock_in TEXT NOT NULL,
    clock_out TEXT,
    heb_year INTEGER NOT NULL,
    heb_month INTEGER NOT NULL,
    heb_day INTEGER NOT NULL,
    note TEXT
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    sender_id INTEGER NOT NULL REFERENCES employees(id),
    created_at TEXT NOT NULL,
    heb_date TEXT NOT NULL,
    recipient_type TEXT NOT NULL,
    recipient_label TEXT NOT NULL,
    recording_path TEXT NOT NULL,
    campaign_id TEXT
);
CREATE TABLE IF NOT EXISTS message_recipients (
    id INTEGER PRIMARY KEY,
    message_id INTEGER NOT NULL REFERENCES messages(id),
    employee_id INTEGER NOT NULL REFERENCES employees(id),
    delivery_status TEXT NOT NULL DEFAULT 'waiting',
    listened_at TEXT
);
CREATE TABLE IF NOT EXISTS corrections (
    id INTEGER PRIMARY KEY,
    employee_id INTEGER NOT NULL REFERENCES employees(id),
    created_at TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('in','out')),
    requested_time TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    result TEXT
);
CREATE TABLE IF NOT EXISTS shuttle_runs (
    id INTEGER PRIMARY KEY,
    driver_id INTEGER NOT NULL REFERENCES employees(id),
    created_at TEXT NOT NULL,
    shuttle_group INTEGER NOT NULL,
    recipients TEXT NOT NULL,
    campaign_id TEXT,
    status TEXT
);
CREATE TABLE IF NOT EXISTS bonuses (
    id INTEGER PRIMARY KEY,
    employee_id INTEGER NOT NULL REFERENCES employees(id),
    heb_year INTEGER NOT NULL,
    heb_month INTEGER NOT NULL,
    amount REAL NOT NULL,
    note TEXT
);
CREATE TABLE IF NOT EXISTS call_sessions (
    call_sid TEXT PRIMARY KEY,
    employee_id INTEGER,
    data TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_att_emp ON attendance(employee_id, clock_out);
CREATE INDEX IF NOT EXISTS ix_att_month ON attendance(heb_year, heb_month);
CREATE INDEX IF NOT EXISTS ix_rcpt_emp ON message_recipients(employee_id);
"""


def connect():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db():
    with connect() as c:
        c.executescript(SCHEMA)


def now():
    return datetime.now(TZ).replace(microsecond=0)


def parse(ts):
    return datetime.fromisoformat(ts)


def normalize_phone(p):
    """Turn +972-50-123-4567 / 0501234567 into 0501234567 for matching."""
    if not p:
        return ""
    digits = "".join(ch for ch in p if ch.isdigit())
    if digits.startswith("972"):
        digits = "0" + digits[3:]
    return digits


def rate_for(emp):
    if emp["hourly_rate"] is not None:
        return float(emp["hourly_rate"])
    return DEFAULT_RATES.get(emp["role"])


# ---- call sessions -------------------------------------------------------

def get_session(call_sid):
    with connect() as c:
        row = c.execute("SELECT * FROM call_sessions WHERE call_sid=?", (call_sid,)).fetchone()
    if not row:
        return {"employee_id": None, "data": {}}
    return {"employee_id": row["employee_id"], "data": json.loads(row["data"])}


def save_session(call_sid, employee_id=None, **data):
    s = get_session(call_sid)
    if employee_id is not None:
        s["employee_id"] = employee_id
    s["data"].update(data)
    with connect() as c:
        c.execute(
            "INSERT INTO call_sessions(call_sid, employee_id, data, updated_at) VALUES (?,?,?,?) "
            "ON CONFLICT(call_sid) DO UPDATE SET employee_id=excluded.employee_id, data=excluded.data, "
            "updated_at=excluded.updated_at",
            (call_sid, s["employee_id"], json.dumps(s["data"]), now().isoformat()))
    return s


def purge_old_sessions():
    from datetime import timedelta
    cutoff = (now() - timedelta(days=2)).isoformat()
    with connect() as c:
        c.execute("DELETE FROM call_sessions WHERE updated_at < ?", (cutoff,))
