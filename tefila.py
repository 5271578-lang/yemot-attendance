"""Mivtza Tefila B'Koach (key 8): every morning, whoever stands ready with tallis and tefillin before
Kaddish D'Rabbanan registers on the line - one raffle entry per day. Kept completely apart from the
trip registrations (own tables: tefila_entries, tefila_draws).

The key is open from 60 minutes before netz (15 minutes before davening, which is 45 minutes before netz)
until 30 minutes before netz. Netz = visible sunrise in Tzfas from the Itim LeBina table, in netz_tzfat.csv
(lines: YYYY-MM-DD,HH:MM[:SS]). A day without a netz time in the table is closed.
"""
import csv
import json
import os
import random
from datetime import date, datetime, timedelta

import db

CAMPAIGN = {
    "id": "tefila_bekoach_5787",
    "key": "8",
    "audience": "night",                       # Kollel Chatzos only
    "first": date(2026, 10, 18),
    "last": date(2026, 12, 12),                # Zos Chanuka, 2 Teves 5787
    "opens_before": 60,                        # minutes before netz
    "closes_before": 28,
    "prizes": ["כרטיס טיסה לאומן", "השלמת הסבסוד לנסיעה לאומן בסוף החורף",
               "200 ש\"ח", "200 ש\"ח", "200 ש\"ח"],
}

NETZ_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "netz_tzfat.csv")
_netz_cache = {"mtime": None, "data": {}}


def netz_table():
    try:
        mtime = os.path.getmtime(NETZ_FILE)
    except OSError:
        return {}
    if _netz_cache["mtime"] != mtime:
        data = {}
        with open(NETZ_FILE, encoding="utf-8-sig") as f:
            for row in csv.reader(f):
                if len(row) < 2 or not row[0].strip()[:1].isdigit():
                    continue
                d = date.fromisoformat(row[0].strip())
                parts = [int(x) for x in row[1].strip().split(":")]
                data[d] = (parts + [0, 0])[:3]
        _netz_cache.update(mtime=mtime, data=data)
    return _netz_cache["data"]


def netz(d):
    t = netz_table().get(d)
    return datetime(d.year, d.month, d.day, *t, tzinfo=db.TZ) if t else None


def window(d):
    n = netz(d)
    if n is None:
        return None
    return n - timedelta(minutes=CAMPAIGN["opens_before"]), n - timedelta(minutes=CAMPAIGN["closes_before"])


def for_employee(emp):
    return bool(emp["in_night"]) if CAMPAIGN["audience"] == "night" else True


def is_open(emp=None, now=None):
    now = now or db.now()
    d = now.date()
    if not (CAMPAIGN["first"] <= d <= CAMPAIGN["last"]):
        return False
    if emp is not None and (not emp["active"] or not for_employee(emp)):
        return False
    w = window(d)
    return bool(w and w[0] <= now < w[1])


def campaign_day(emp=None, now=None):
    """A day of the campaign (the key is in the menu all day; registering only inside the window)."""
    d = (now or db.now()).date()
    if not (CAMPAIGN["first"] <= d <= CAMPAIGN["last"]):
        return False
    return emp is None or (emp["active"] and for_employee(emp))


# ---- personal reminder call: N minutes (his choice) before davening ------------

DAVENING_BEFORE_NETZ = 30        # davening starts 30 minutes before netz
REMINDER_MIN = 10                # at least 10 minutes before davening
REMINDER_MAX = 120


def davening(d):
    n = netz(d)
    return n - timedelta(minutes=DAVENING_BEFORE_NETZ) if n else None


def set_reminder(emp_id, minutes):
    with db.connect() as c:
        c.execute("UPDATE employees SET tefila_reminder=? WHERE id=?", (minutes or None, emp_id))


def reminders_due(now=None):
    """[(employee row, minutes)] whose reminder time has come now (within 10 minutes) and was not sent today.
    Each one is claimed, so it is sent once even with several workers. Not on Shabbos."""
    now = now or db.now()
    d = now.date()
    dav = davening(d)
    if dav is None or not (CAMPAIGN["first"] <= d <= CAMPAIGN["last"]) or d.weekday() == 5:
        return []
    out = []
    with db.connect() as c:
        rows = c.execute("SELECT * FROM employees WHERE active=1 AND tefila_reminder>0").fetchall()
        for e in rows:
            if not for_employee(e):
                continue
            at = dav - timedelta(minutes=e["tefila_reminder"])
            if at <= now < at + timedelta(minutes=10):
                if c.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?,?)",
                             (f"tefila_rem_{e['id']}_{d.isoformat()}", now.isoformat())).rowcount:
                    out.append((e, e["tefila_reminder"]))
    return out


def run_reminders(now=None):
    """Called every minute: phone everyone whose reminder is due."""
    import nbs
    import prompts
    due = reminders_due(now)
    if not due:
        return None
    texts = {e["phone"]: prompts.text(e["language"] or "he", "tefila_reminder_call", name=e["name"], minutes=m)
             for e, m in due if nbs.israeli(e["phone"] or "")}
    return nbs.run_tts_calls(texts) if texts else None


def entries_of(emp_id):
    with db.connect() as c:
        return c.execute("SELECT COUNT(*) FROM tefila_entries WHERE campaign=? AND employee_id=?",
                         (CAMPAIGN["id"], emp_id)).fetchone()[0]


def manager_period(now=None):
    """Managers can hear the status from the first day until two weeks after the end (the raffle)."""
    d = (now or db.now()).date()
    return CAMPAIGN["first"] <= d <= CAMPAIGN["last"] + timedelta(days=14)


def register(emp_id, now=None):
    """Registers today's entry. Returns (new, total entries of this avrech)."""
    now = now or db.now()
    with db.connect() as c:
        new = c.execute("INSERT OR IGNORE INTO tefila_entries(campaign, employee_id, day, created_at) VALUES (?,?,?,?)",
                        (CAMPAIGN["id"], emp_id, now.date().isoformat(), now.isoformat())).rowcount
        total = c.execute("SELECT COUNT(*) FROM tefila_entries WHERE campaign=? AND employee_id=?",
                          (CAMPAIGN["id"], emp_id)).fetchone()[0]
    return bool(new), total


def add_manual(emp_id, day, by_id, now=None):
    """A manager adds an avrech's entry for a day (he did not manage to register). Returns True if new."""
    now = now or db.now()
    with db.connect() as c:
        return bool(c.execute("INSERT OR IGNORE INTO tefila_entries(campaign, employee_id, day, created_at, added_by) "
                              "VALUES (?,?,?,?,?)", (CAMPAIGN["id"], emp_id, day.isoformat(), now.isoformat(), by_id)).rowcount)


def totals():
    """[(row with name etc., entries)] most entries first."""
    with db.connect() as c:
        return c.execute("SELECT e.id, e.name, e.emp_number, e.phone, COUNT(*) AS entries, MAX(t.day) AS last_day "
                         "FROM tefila_entries t JOIN employees e ON e.id = t.employee_id WHERE t.campaign=? "
                         "GROUP BY e.id ORDER BY entries DESC, e.name", (CAMPAIGN["id"],)).fetchall()


def by_day():
    with db.connect() as c:
        return c.execute("SELECT day, COUNT(*) AS n FROM tefila_entries WHERE campaign=? GROUP BY day ORDER BY day DESC",
                         (CAMPAIGN["id"],)).fetchall()


def entries_of_day(day):
    with db.connect() as c:
        return c.execute("SELECT t.*, e.name, e.emp_number FROM tefila_entries t JOIN employees e ON e.id = t.employee_id "
                         "WHERE t.campaign=? AND t.day=? ORDER BY t.created_at", (CAMPAIGN["id"], day)).fetchall()


def draw():
    """Weighted raffle (every day = one ticket); each avrech can win once. Saved with its time, so every draw is on record."""
    tickets = []
    for r in totals():
        tickets += [(r["id"], r["name"])] * r["entries"]
    winners = []
    rng = random.SystemRandom()
    for prize in CAMPAIGN["prizes"]:
        left = [t for t in tickets if t[0] not in {w["id"] for w in winners}]
        if not left:
            break
        eid, name = rng.choice(left)
        winners.append({"prize": prize, "id": eid, "name": name})
    with db.connect() as c:
        c.execute("INSERT INTO tefila_draws(campaign, created_at, tickets, result) VALUES (?,?,?,?)",
                  (CAMPAIGN["id"], db.now().isoformat(), len(tickets), json.dumps(winners, ensure_ascii=False)))
    return winners


def draws():
    with db.connect() as c:
        rows = c.execute("SELECT * FROM tefila_draws WHERE campaign=? ORDER BY id DESC", (CAMPAIGN["id"],)).fetchall()
    return [dict(r, winners=json.loads(r["result"])) for r in rows]
