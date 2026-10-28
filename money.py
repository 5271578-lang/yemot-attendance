"""Moreshet HaNachal money, kept by the treasurer (the manager whose bank account the money comes into).

Every month 3200 comes into his account for the activity. On the 15th the e-mail says how much to transfer
on the 16th to each one: his salary + his reported expenses - his reported unusual income (reports from the
15th of last month to the 14th). From the 16th, 14:00, every day until it is all transferred: a reminder call,
and when he calls the line (until midnight) he goes straight to the transfer confirmation (Moreshet key 8).

Ledger (moreshet_ledger): money of the activity in his hands. + comes in (the monthly 3200, parents' standing
orders and other income he reports on key 9), - goes out (transfers, expenses he reports on key 7).
The balance is what he holds for the activity ("a debt of 400 on me").
"""
import json
from datetime import date, datetime, timedelta

import db

MONTHLY_IN = 3200
TRANSFER_DAY = 16
REMIND_HOUR = 14
SALARIES = {"3": 1200, "4": 1600}          # by Moreshet key: Naftali, Natan (first setup; then on the admin page)


# ---- who / settings ----------------------------------------------------------

def monthly_in():
    try:
        return float(db.get_setting("money_monthly_in") or MONTHLY_IN)
    except ValueError:
        return MONTHLY_IN


def treasurer_id():
    v = db.get_setting("money_treasurer")
    if v:
        return int(v) if v.isdigit() else None
    with db.connect() as c:
        hits = [e for e in c.execute("SELECT id, name FROM employees WHERE active=1").fetchall()
                if "ישראל" in e["name"] and "טנג" in e["name"]]
    if len(hits) == 1:
        db.set_setting("money_treasurer", str(hits[0]["id"]))
        return hits[0]["id"]
    return None


def is_treasurer(emp):
    return emp is not None and emp["id"] == treasurer_id()


def assign_salaries():
    """Once: Naftali 1200, Natan 1600 (by their Moreshet keys) when no salary was set yet."""
    with db.connect() as c:
        for e in c.execute("SELECT id, moreshet_key, moreshet_salary FROM employees WHERE moreshet_role IS NOT NULL").fetchall():
            if e["moreshet_salary"] is None and e["moreshet_key"] in SALARIES:
                c.execute("UPDATE employees SET moreshet_salary=? WHERE id=?", (SALARIES[e["moreshet_key"]], e["id"]))


# ---- the monthly cycle -----------------------------------------------------------

def cycle_of(d):
    """'YYYY-MM' of the 15th whose transfer this is (on the 15th and after: this month's)."""
    if d.day < 15:
        d = (d.replace(day=1) - timedelta(days=1))
    return f"{d.year}-{d.month:02d}"


def _period(cycle):
    y, m = (int(x) for x in cycle.split("-"))
    end = date(y, m, 15)
    start = date(y - (m == 1), 12 if m == 1 else m - 1, 15)
    return start, end


def compute_due(cycle):
    """[{id, name, salary, expenses, income, amount}] for everyone but the treasurer who has a salary or reports."""
    start, end = _period(cycle)
    tid = treasurer_id()
    with db.connect() as c:
        people = c.execute("SELECT * FROM employees WHERE active=1 AND moreshet_role IN ('mashgiach','manager')").fetchall()
        reps = c.execute("SELECT employee_id, kind, SUM(amount) s FROM moreshet_reports WHERE created_at>=? AND created_at<? "
                         "GROUP BY employee_id, kind", (start.isoformat(), end.isoformat())).fetchall()
    by = {}
    for x in reps:
        by.setdefault(x["employee_id"], {})[x["kind"]] = x["s"] or 0
    out = []
    for e in people:
        if e["id"] == tid:
            continue
        sal = e["moreshet_salary"] or 0
        exp = round(by.get(e["id"], {}).get("expense", 0), 2)
        inc = round(by.get(e["id"], {}).get("income", 0), 2)
        if sal or exp or inc:
            out.append({"id": e["id"], "name": e["name"], "key": e["moreshet_key"], "salary": sal, "expenses": exp,
                        "income": inc, "amount": round(sal + exp - inc, 2)})
    return sorted(out, key=lambda x: (x["key"] or "99", x["name"]))


def open_cycle(today=None):
    """On the 15th (and after, if missed): fix this month's amounts and record the month's 3200. Once."""
    today = today or db.now().date()
    if today.day < 15:
        return None
    cycle = cycle_of(today)
    with db.connect() as c:
        if c.execute("SELECT 1 FROM moreshet_cycles WHERE cycle=?", (cycle,)).fetchone():
            return None
    due = compute_due(cycle)
    with db.connect() as c:
        if not c.execute("INSERT OR IGNORE INTO moreshet_cycles(cycle, due_json, due_total, created_at) VALUES (?,?,?,?)",
                         (cycle, json.dumps(due, ensure_ascii=False), round(sum(x["amount"] for x in due), 2),
                          db.now().isoformat())).rowcount:
            return None
        if treasurer_id():
            c.execute("INSERT INTO moreshet_ledger(created_at, day, kind, amount, note, cycle) VALUES (?,?,?,?,?,?)",
                      (db.now().isoformat(), date(*(int(x) for x in cycle.split("-")), 15).isoformat(), "funding",
                       monthly_in(), "כניסה חודשית לפעילות", cycle))
    return cycle


def cycle(cyc):
    with db.connect() as c:
        row = c.execute("SELECT * FROM moreshet_cycles WHERE cycle=?", (cyc,)).fetchone()
        if row is None:
            return None
        paid = c.execute("SELECT COALESCE(-SUM(amount), 0) FROM moreshet_ledger WHERE cycle=? AND kind='transfer'",
                         (cyc,)).fetchone()[0]
    d = dict(row)
    d["due"] = json.loads(row["due_json"])
    d["paid"] = round(paid, 2)
    d["left"] = round(row["due_total"] - paid, 2)
    return d


def pending():
    """The open cycles with something left to transfer, oldest first."""
    with db.connect() as c:
        cycles = [x["cycle"] for x in c.execute("SELECT cycle FROM moreshet_cycles WHERE done_at IS NULL ORDER BY cycle")]
    return [x for x in (cycle(k) for k in cycles) if x and x["left"] > 0.004]


def record_transfer(cyc, amount=None, by_id=None):
    """amount None = everything left of that month (one line per person); else a partial amount."""
    cy = cycle(cyc)
    if cy is None:
        return 0
    now = db.now()
    with db.connect() as c:
        if amount is None:
            paid_to = {r["employee_id"]: -r["s"] for r in c.execute(
                "SELECT employee_id, SUM(amount) s FROM moreshet_ledger WHERE cycle=? AND kind='transfer' "
                "AND employee_id IS NOT NULL GROUP BY employee_id", (cyc,))}
            general = cy["paid"] - sum(paid_to.values())
            for x in cy["due"]:
                rest = round(x["amount"] - paid_to.get(x["id"], 0), 2)
                take = min(rest, max(general, 0))
                rest, general = round(rest - take, 2), general - take
                if rest > 0:
                    c.execute("INSERT INTO moreshet_ledger(created_at, day, kind, amount, note, cycle, employee_id) "
                              "VALUES (?,?,?,?,?,?,?)", (now.isoformat(), now.date().isoformat(), "transfer", -rest,
                                                         f"העברה ל{x['name']}", cyc, x["id"]))
            amount = cy["left"]
        else:
            c.execute("INSERT INTO moreshet_ledger(created_at, day, kind, amount, note, cycle) VALUES (?,?,?,?,?,?)",
                      (now.isoformat(), now.date().isoformat(), "transfer", -round(amount, 2), "העברת משכורות (חלקית)", cyc))
    if cycle(cyc)["left"] <= 0.004:
        with db.connect() as c:
            c.execute("UPDATE moreshet_cycles SET done_at=? WHERE cycle=?", (now.isoformat(), cyc))
    return amount


# ---- the ledger --------------------------------------------------------------------

def add(kind, amount, note, day=None, report_id=None, employee_id=None):
    """kind: funding / income (+) ; transfer / expense (-) ; manual (as given). amount is positive except manual."""
    sign = {"funding": 1, "income": 1, "transfer": -1, "expense": -1}.get(kind, 1)
    with db.connect() as c:
        return c.execute("INSERT INTO moreshet_ledger(created_at, day, kind, amount, note, report_id, employee_id) "
                         "VALUES (?,?,?,?,?,?,?)", (db.now().isoformat(), (day or db.now().date()).isoformat(), kind,
                                                     round(sign * abs(amount) if kind != "manual" else amount, 2),
                                                     note, report_id, employee_id)).lastrowid


def from_report(emp, kind, amount, day, report_id):
    """The treasurer's own reports (key 7 / 9) are money out of / into his hands."""
    if is_treasurer(emp):
        add("expense" if kind == "expense" else "income", amount,
            "הוצאה שדווחה בקו" if kind == "expense" else "הכנסה שדווחה בקו", day, report_id)


def balance():
    with db.connect() as c:
        return round(c.execute("SELECT COALESCE(SUM(amount), 0) FROM moreshet_ledger").fetchone()[0], 2)


def ledger(limit=200):
    with db.connect() as c:
        return c.execute("SELECT l.*, e.name FROM moreshet_ledger l LEFT JOIN employees e ON e.id=l.employee_id "
                         "ORDER BY l.day DESC, l.id DESC LIMIT ?", (limit,)).fetchall()


# ---- reminders ---------------------------------------------------------------------

def remind_now(now=None):
    """From the 16th at 14:00, every day until all is transferred."""
    now = now or db.now()
    ps = pending()
    if not ps or now.hour < REMIND_HOUR:
        return False
    first = date(*(int(x) for x in ps[0]["cycle"].split("-")), TRANSFER_DAY)
    return now.date() >= first


def route_now(emp, now=None):
    """When he calls between 14:00 and midnight on a reminder day: straight to the transfer confirmation."""
    now = now or db.now()
    return is_treasurer(emp) and remind_now(now) and not db.get_setting(f"money_snooze_{now.date().isoformat()}")


def snooze(now=None):
    """'Not yet' / a partial transfer: no more straight routing today; tomorrow at 14:00 again."""
    now = now or db.now()
    db.set_setting(f"money_snooze_{now.date().isoformat()}", "1")


def run_reminder(now=None):
    import nbs
    import prompts
    now = now or db.now()
    if not remind_now(now) or now.hour >= 21:
        return None
    tid = treasurer_id()
    with db.connect() as c:
        e = c.execute("SELECT * FROM employees WHERE id=?", (tid,)).fetchone()
        if e is None or not nbs.israeli(e["phone"] or ""):
            return None
        if not c.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?,?)",
                         (f"money_rem_{now.date().isoformat()}", now.isoformat())).rowcount:
            return None
    return nbs.run_tts_calls({e["phone"]: prompts.text(e["language"] or "he", "money_reminder_call", name=e["name"])})


# ---- the state when this started (28/10/2026) ----------------------------------------

def seed_once():
    if db.get_setting("money_seeded") or not treasurer_id():
        return
    assign_salaries()
    with db.connect() as c:
        names = {e["moreshet_key"]: (e["id"], e["name"]) for e in c.execute(
            "SELECT id, name, moreshet_key FROM employees WHERE moreshet_role IS NOT NULL")}
    nf = names.get("3", (None, "נפתלי"))
    nt = names.get("4", (None, "נתן"))
    due = [{"id": nf[0], "name": nf[1], "key": "3", "salary": 1200, "expenses": 0, "income": 0, "amount": 1200},
           {"id": nt[0], "name": nt[1], "key": "4", "salary": 1600, "expenses": 0, "income": 0, "amount": 1600}]
    t = db.now().isoformat()
    with db.connect() as c:
        c.execute("INSERT OR IGNORE INTO moreshet_cycles(cycle, due_json, due_total, created_at, done_at) VALUES (?,?,?,?,?)",
                  ("2026-10", json.dumps(due, ensure_ascii=False), 2800, t, t))
        rows = [("2026-10-15", "funding", 3200, "כניסה חודשית לפעילות", "2026-10", None),
                ("2026-10-16", "transfer", -1200, f"משכורת ל{nf[1]}", "2026-10", nf[0]),
                ("2026-10-16", "transfer", -1600, f"משכורת ל{nt[1]}", "2026-10", nt[0]),
                ("2026-10-28", "income", 4100, "הוראות קבע של ההורים", None, None),
                ("2026-10-28", "expense", -160.80, "משנת יוסף - קניות לפעילות", None, None),
                ("2026-10-28", "expense", -55.20, "יש - 8 × 6.90", None, None),
                ("2026-10-28", "expense", -240.00, "הדפסת 30 חוברות: 32 עמ' A5 = 16 עמ' A4 × 0.50", None, None),
                ("2026-10-28", "expense", -100.00, "זמן מחשב להכנת החוברות", None, None)]
        c.executemany("INSERT INTO moreshet_ledger(created_at, day, kind, amount, note, cycle, employee_id) VALUES (?,?,?,?,?,?,?)",
                      [(t, d, k, a, n, cy, e) for d, k, a, n, cy, e in rows])
    db.set_setting("money_seeded", "1")
