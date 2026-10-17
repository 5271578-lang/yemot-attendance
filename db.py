import json
import os
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "attendance.db"))
TZ = ZoneInfo(os.environ.get("TIMEZONE", "Asia/Jerusalem"))

ROLES = ("employee", "onsite_manager", "tech_manager")
MANAGER_ROLES = ("onsite_manager", "tech_manager")
DEFAULT_RATES = {"employee": 16.0, "onsite_manager": 25.0, "tech_manager": None}   # per hour of a regular session

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
    shuttle_calls INTEGER,
    language TEXT NOT NULL DEFAULT 'he',
    joined_on TEXT,
    restored_on TEXT,
    id_number TEXT,
    phone2 TEXT,
    phone2_mode TEXT NOT NULL DEFAULT 'none',
    phone2_calls INTEGER NOT NULL DEFAULT 1,
    in_night INTEGER NOT NULL DEFAULT 1,
    in_day INTEGER NOT NULL DEFAULT 0,
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
    exit_confirmed INTEGER NOT NULL DEFAULT 0,
    early_exit INTEGER NOT NULL DEFAULT 0,
    meron INTEGER NOT NULL DEFAULT 0,
    actual_in TEXT,
    program TEXT NOT NULL DEFAULT 'night',
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
    listened_at TEXT,
    announced_at TEXT
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
CREATE TABLE IF NOT EXISTS uman (
    employee_id INTEGER NOT NULL REFERENCES employees(id),
    heb_year INTEGER NOT NULL,
    went INTEGER NOT NULL,
    answered_at TEXT NOT NULL,
    percent REAL,
    nights TEXT,
    PRIMARY KEY (employee_id, heb_year)
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trip_signups (
    trip TEXT NOT NULL,
    employee_id INTEGER NOT NULL REFERENCES employees(id),
    choice TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (trip, employee_id)
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


# Columns added after the first release. Existing databases get them on start-up.
MIGRATIONS = {
    "employees": [("shuttle_calls", "INTEGER"), ("language", "TEXT NOT NULL DEFAULT 'he'"),
                  ("joined_on", "TEXT"), ("restored_on", "TEXT"), ("id_number", "TEXT"),
                  ("in_night", "INTEGER NOT NULL DEFAULT 1"), ("in_day", "INTEGER NOT NULL DEFAULT 0"),
                  ("phone2", "TEXT"), ("phone2_mode", "TEXT NOT NULL DEFAULT 'none'"),
                  ("phone2_calls", "INTEGER NOT NULL DEFAULT 1")],
    "message_recipients": [("announced_at", "TEXT")],
    "attendance": [("exit_confirmed", "INTEGER NOT NULL DEFAULT 0"), ("early_exit", "INTEGER NOT NULL DEFAULT 0"),
                   ("meron", "INTEGER NOT NULL DEFAULT 0"), ("actual_in", "TEXT"),
                   ("program", "TEXT NOT NULL DEFAULT 'night'")],
}


def init_db():
    with connect() as c:
        c.executescript(SCHEMA)
        for table, cols in MIGRATIONS.items():
            have = {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
            for name, decl in cols:
                if name not in have:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
        # whoever has no join date counts from the first time this version runs
        c.execute("UPDATE employees SET joined_on=? WHERE joined_on IS NULL", (now().date().isoformat(),))


def get_setting(key, default=None):
    with connect() as c:
        row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    with connect() as c:
        c.execute("INSERT INTO settings(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (key, str(value)))


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


_KEEP = object()


def save_session(call_sid, employee_id=_KEEP, **data):
    s = get_session(call_sid)
    if employee_id is not _KEEP:
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
