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
    "closes_before": 30,
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


def register(emp_id, now=None):
    """Registers today's entry. Returns (new, total entries of this avrech)."""
    now = now or db.now()
    with db.connect() as c:
        new = c.execute("INSERT OR IGNORE INTO tefila_entries(campaign, employee_id, day, created_at) VALUES (?,?,?,?)",
                        (CAMPAIGN["id"], emp_id, now.date().isoformat(), now.isoformat())).rowcount
        total = c.execute("SELECT COUNT(*) FROM tefila_entries WHERE campaign=? AND employee_id=?",
                          (CAMPAIGN["id"], emp_id)).fetchone()[0]
    return bool(new), total


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
