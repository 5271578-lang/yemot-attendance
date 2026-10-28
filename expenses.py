"""The line's own monthly expenses, added automatically every Kollel Chatzos month as a separate bonus
(gifts table, note NOTE) to the one who pays them. Set on the admin page "Bonuses".

Settings: line_exp_emp (employee id), line_exp_items (JSON [[name, amount], ...]), line_exp_from ("year-month",
the first night month it is added in; months counted from Nisan = 1).
"""
import json

import db
import schedule

NOTE = "הוצאות הקו"
DEFAULT_ITEMS = [["NBS - ערוצי חסימת פרסומות", 9.90], ["Render - השרת (7.25$)", 27.00], ["מנוי Claude Pro", 59.00]]
DEFAULT_FROM = (5787, 9)            # Kislev 5787 (Cheshvan was handled by hand)


def items():
    try:
        return [(str(n), float(a)) for n, a in json.loads(db.get_setting("line_exp_items") or "null") or DEFAULT_ITEMS]
    except (ValueError, TypeError):
        return [(n, a) for n, a in DEFAULT_ITEMS]


def total():
    return round(sum(a for _n, a in items()), 2)


def first_month():
    v = db.get_setting("line_exp_from")
    if v:
        y, m = v.split("-")
        return int(y), int(m)
    return DEFAULT_FROM


def employee_id():
    v = db.get_setting("line_exp_emp")
    if v:
        return int(v) if v.isdigit() else None
    with db.connect() as c:                       # first time: the manager Yisrael Tenji, by name
        hits = [e for e in c.execute("SELECT id, name FROM employees WHERE active=1").fetchall()
                if "ישראל" in e["name"] and "טנג" in e["name"]]
    if len(hits) == 1:
        db.set_setting("line_exp_emp", str(hits[0]["id"]))
        return hits[0]["id"]
    return None


def save(emp_id, item_lines, first):
    db.set_setting("line_exp_emp", str(emp_id or ""))
    db.set_setting("line_exp_items", json.dumps(item_lines, ensure_ascii=False))
    db.set_setting("line_exp_from", f"{first[0]}-{first[1]}")


def parse_lines(text):
    """'name = 27' per line -> [[name, 27.0]]"""
    out = []
    for line in (text or "").splitlines():
        if "=" not in line:
            continue
        name, amount = line.rsplit("=", 1)
        try:
            out.append([name.strip(), round(float(amount.strip().replace(",", "")), 2)])
        except ValueError:
            continue
    return out


def month_key(y, m):
    """Order of Hebrew months in a year (Tishrei = 7 first; Adar II = 13 before Nisan = 1)."""
    return y, (m if m >= 7 else m + 13)


def run_once(now=None):
    """Add this night month's line expenses once (from the first month on). Returns the gift id or None."""
    now = now or db.now()
    hy, hm = schedule.night_month_of(schedule.session_date(now))
    eid, amount = employee_id(), total()
    if not eid or amount <= 0 or month_key(hy, hm) < month_key(*first_month()):
        return None
    with db.connect() as c:
        if not c.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?,?)",
                         (f"line_exp_added_{hy}_{hm}", now.isoformat())).rowcount:
            return None
        note = NOTE + ": " + ", ".join(f"{n} {a:g}" for n, a in items())
        return c.execute("INSERT INTO gifts(employee_id, heb_year, heb_month, amount, note, created_at, added_by) "
                         "VALUES (?,?,?,?,?,?,?)", (eid, hy, hm, amount, note, now.isoformat(), -1)).lastrowid
