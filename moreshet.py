"""Moreshet HaNachal (key 00): a separate line inside the kollel line, only for its people.

Who: employees with moreshet_role 'mashgiach' or 'manager' (set on the admin page). moreshet_key is the
digit others press to leave a message for him (3, 4, 5, 6...). Someone in no kollel goes straight to it.

Messages use the messages tables with channel='moreshet' (the kollel's key 4 never sees them).
Expenses (key 7) and unusual income (key 9): amount, Hebrew date (DDMM), a recording of what for.
A summary is e-mailed on the 15th of every (Gregorian) month to MORESHET_EMAIL.
"""
import os
import re
from datetime import date, timedelta

import db
import hebcal

ROLES = {"mashgiach": "משגיח", "manager": "מנהל"}
EMAIL = os.environ.get("MORESHET_EMAIL", "5271578@gmail.com")
REPORT_DAY = 15

# One-time setup by name (the admin page can change it at any time).
DEFAULT_PEOPLE = [
    (("נפתלי", "שפירא"), "mashgiach", "3"),
    (("נתן", "יארבלום"), "mashgiach", "4"),
    (("נתן", "ירבלום"), "mashgiach", "4"),
    (("שמחה", "בורשטיין"), "manager", "5"),
    (("שמחהלע", "בורשטיין"), "manager", "5"),
    (("ישראל", "טנג"), "manager", "6"),
]


def _words(name):
    return [w for w in re.sub(r"[\"'`׳״\-.,]", "", name or "").split() if w]


def assign_defaults():
    """Mark the people above by their names, once. Returns the names that were found."""
    if db.get_setting("moreshet_defaults_done"):
        return []
    found = []
    with db.connect() as c:
        people = c.execute("SELECT id, name, moreshet_role FROM employees WHERE active=1").fetchall()
        for words, role, key in DEFAULT_PEOPLE:
            hits = [p for p in people if all(any(w.startswith(x) for w in _words(p["name"])) for x in words)]
            if len(hits) == 1 and not hits[0]["moreshet_role"]:
                c.execute("UPDATE employees SET moreshet_role=?, moreshet_key=? WHERE id=?", (role, key, hits[0]["id"]))
                found.append(hits[0]["name"])
    db.set_setting("moreshet_defaults_done", "1")
    return found


def is_member(emp):
    return bool(emp is not None and emp["active"] and (emp["moreshet_role"] or "") in ROLES)


def only_member(emp):
    """In Moreshet HaNachal and in no kollel: goes straight to it."""
    return is_member(emp) and not emp["in_night"] and not emp["in_day"]


def members():
    with db.connect() as c:
        rows = c.execute("SELECT * FROM employees WHERE active=1 AND moreshet_role IN ('mashgiach','manager')").fetchall()
    return sorted(rows, key=lambda e: (e["moreshet_key"] or "99", e["name"]))


def recipients(target, sender_id):
    people = [e for e in members() if e["id"] != sender_id]
    t = target["type"]
    if t == "m_mashgichim":
        return [e for e in people if e["moreshet_role"] == "mashgiach"]
    if t == "m_managers":
        return [e for e in people if e["moreshet_role"] == "manager"]
    if t == "m_all":
        return people
    if t == "m_person":
        return [e for e in people if e["id"] == target["employee_id"]]
    return []


def label(target, rcpts):
    return {"m_mashgichim": "המשגיחים", "m_managers": "המנהלים", "m_all": "כולם"}.get(
        target["type"], rcpts[0]["name"] if rcpts else "")


# ---- expenses / income ----------------------------------------------------

def parse_amount(answer):
    """'125*50' -> 125.50 ; '125' -> 125.0"""
    a = (answer or "").replace("Digits-", "").strip("#")
    parts = [p for p in a.split("*")]
    if not parts or not parts[0].isdigit():
        return None
    shekels = int(parts[0])
    agorot = 0
    if len(parts) > 1 and parts[1]:
        if not parts[1].isdigit():
            return None
        agorot = int(parts[1][:2].ljust(2, "0"))
    amount = shekels + agorot / 100
    return amount if amount > 0 else None


def parse_heb_date(answer, today):
    """'0109' -> 1 Kislev of this Hebrew year (or last year, if that date is still ahead). Month numbers from
    Nisan = 1 (Tishrei = 7, Kislev = 9, Adar = 12, Adar II = 13). Returns a Gregorian date or None."""
    a = re.sub(r"\D", "", answer or "")
    if len(a) != 4:
        return None
    day, month = int(a[:2]), int(a[2:])
    if not (1 <= day <= 30 and 1 <= month <= 13):
        return None
    y = hebcal.from_gregorian(today)[0]
    for year in (y, y - 1):
        if month == 13 and not hebcal.is_leap(year):
            continue
        if month == 12 and hebcal.is_leap(year):
            pass                                     # Adar I in a leap year
        if day > hebcal.month_length(month, year):
            continue
        g = hebcal.to_gregorian(year, month, day)
        if g <= today:
            return g
    return None


def save_report(emp_id, kind, amount, spent_on, recording):
    with db.connect() as c:
        return c.execute("INSERT INTO moreshet_reports(employee_id, kind, amount, spent_on, heb_date, recording_path, created_at) "
                         "VALUES (?,?,?,?,?,?,?)", (emp_id, kind, amount, spent_on.isoformat(),
                                                   hebcal.format_date(spent_on, "he"), recording, db.now().isoformat())).lastrowid


def reports(first=None, last=None):
    q = "SELECT r.*, e.name FROM moreshet_reports r JOIN employees e ON e.id=r.employee_id"
    args = ()
    if first and last:
        q += " WHERE r.created_at>=? AND r.created_at<?"
        args = (first.isoformat(), last.isoformat())
    with db.connect() as c:
        return c.execute(q + " ORDER BY r.created_at", args).fetchall()


# ---- monthly e-mail on the 15th -------------------------------------------

def period(today):
    """Reports made from the 15th of last month up to the 14th of this month."""
    end = date(today.year, today.month, REPORT_DAY)
    start = date(end.year - (end.month == 1), 12 if end.month == 1 else end.month - 1, REPORT_DAY)
    return start, end


def build(today, base_url=""):
    from html import escape
    start, end = period(today)
    rows = reports(start, end)
    td = 'style="padding:5px 8px;border:1px solid #dde3ea;text-align:right"'
    th = 'style="background:#e8edf3;padding:6px 8px;border:1px solid #c9d1db;text-align:right"'
    out = []
    for kind, title in (("expense", "הוצאות"), ("income", "הכנסות חריגות")):
        rs = [x for x in rows if x["kind"] == kind]
        total = sum(x["amount"] for x in rs)
        out.append(f'<h3 style="color:#1f3a5f">{title}: {total:,.2f} ש"ח</h3>')
        if not rs:
            out.append("<p>אין</p>")
            continue
        out.append(f"<table style='border-collapse:collapse'><tr><th {th}>מי</th><th {th}>תאריך</th><th {th}>סכום</th>"
                   f"<th {th}>דווח</th><th {th}>הקלטה</th></tr>")
        for x in rs:
            link = f'<a href="{base_url}/admin/moreshet/recording/{x["id"]}">להאזנה</a>' if base_url else ""
            out.append(f"<tr><td {td}>{escape(x['name'])}</td><td {td}>{escape(x['heb_date'] or '')}</td>"
                       f"<td {td}>{x['amount']:,.2f}</td><td {td}>{escape(x['created_at'][:16].replace('T', ' '))}</td>"
                       f"<td {td}>{link}</td></tr>")
        out.append("</table>")
    rng = f"{start.strftime('%d/%m/%Y')} עד {(end - timedelta(days=1)).strftime('%d/%m/%Y')}"
    html = (f'<div dir="rtl" style="font-family:Arial,sans-serif"><h2 style="color:#1f3a5f">מורשת הנחל: דיווחי הוצאות והכנסות</h2>'
            f"<p>דיווחים שנעשו בקו מ־{rng}</p>{''.join(out)}</div>")
    csv_lines = ["﻿סוג,מי,תאריך,סכום,דווח"] + [
        f"{'הוצאה' if x['kind'] == 'expense' else 'הכנסה'},{x['name']},{x['heb_date']},{x['amount']},{x['created_at'][:16]}" for x in rows]
    return f"מורשת הנחל - סיכום {rng}", html, "\n".join(csv_lines)


def due(now, hour=8):
    return now.day >= REPORT_DAY and (now.day > REPORT_DAY or now.hour >= hour)
