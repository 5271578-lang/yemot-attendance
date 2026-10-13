"""Monthly summary of the whole kollel, e-mailed on the 28th of every Hebrew month.

Settings (Render -> Environment):
  REPORT_EMAIL     where to send (one or more addresses, comma separated)
  SMTP_USER        the Gmail address that sends it
  SMTP_PASSWORD    a Gmail "app password" (16 letters) - not the normal Gmail password
  SMTP_HOST / SMTP_PORT   default smtp.gmail.com / 587
  REPORT_DAY       Hebrew day of the month to send on (default 28)
  REPORT_HOUR      hour to send at (default 8:00)
"""
import csv
import io
import os
import smtplib
import threading
import time
from email.message import EmailMessage
from html import escape

import db
import hebcal
import schedule

REPORT_DAY = int(os.environ.get("REPORT_DAY", 28))
REPORT_HOUR = int(os.environ.get("REPORT_HOUR", 8))
CHECK_EVERY_SECONDS = 15 * 60


def settings():
    return {
        "to": [a.strip() for a in os.environ.get("REPORT_EMAIL", "").split(",") if a.strip()],
        "user": os.environ.get("SMTP_USER", ""),
        "password": os.environ.get("SMTP_PASSWORD", "").replace(" ", ""),
        "host": os.environ.get("SMTP_HOST", "smtp.gmail.com"),
        "port": int(os.environ.get("SMTP_PORT", 587)),
    }


def enabled():
    s = settings()
    return bool(s["to"] and s["user"] and s["password"])


def rows_for(hy, hm, until=None):
    with db.connect() as c:
        emps = c.execute("SELECT * FROM employees WHERE active=1 ORDER BY name").fetchall()
    out = []
    for e in emps:
        s = schedule.month_summary(e, hy, hm, until=until)
        out.append((e, s))
    return out


def build(hy, hm, until=None):
    """(subject, html, csv_text) for one Hebrew month."""
    month = f"{hebcal.month_name(hm, hy, 'he')} {hy}"
    rows = rows_for(hy, hm, until)
    upto = f" (נכון ל{hebcal.format_date(until, 'he')})" if until else ""
    paid = [(e, s) for e, s in rows if s["total"]]
    total = round(sum(s["total"] or 0 for _, s in rows), 2)
    meron = sum(s["meron"] for _, s in rows)
    driver = sum(s["driver"] for _, s in rows)
    not_eligible = [e["name"] for e, s in rows if e["role"] == "employee" and not s["eligible"]]
    removed = [e["name"] for e, _ in rows if schedule.removed_by_rule(e, until or hebcal.month_range(hy, hm)[1])]

    th = 'style="background:#e8edf3;padding:6px 8px;border:1px solid #c9d1db;text-align:right"'
    td = 'style="padding:5px 8px;border:1px solid #dde3ea;text-align:right"'
    lines = []
    for e, s in rows:
        stipend = f"{s['stipend']:.0f}" if s["eligible"] else f'<span style="color:#b00020">0</span> ({s["stipend"]:.0f})'
        total_cell = "" if s["total"] is None else f"{s['total']:.0f}"
        lines.append(
            f"<tr><td {td}>{escape(e['name'])}</td><td {td}>{s['nights']}</td><td {td}>{s['whole']}</td>"
            f"<td {td}>{round(s['percent'] * 100)}%</td><td {td}>{stipend}</td><td {td}>{s['meron']}</td>"
            f"<td {td}>{s['driver']}</td><td {td}><b>{total_cell}</b></td></tr>")
    html = f"""<div dir="rtl" style="font-family:Arial,sans-serif;font-size:14px;color:#222">
<h2 style="color:#1f3a5f;margin:0 0 8px">סיכום חודש {month}{upto}</h2>
<p>סך הכל לתשלום: <b>{total:,.0f} ש"ח</b>, ל־{len(paid)} אברכים. מתוך זה מירון {meron:,.0f} ש"ח והסעות {driver:,.0f} ש"ח.</p>
<table style="border-collapse:collapse" dir="rtl">
<tr><th {th}>שם</th><th {th}>לילות</th><th {th}>שלמים</th><th {th}>נוכחות</th><th {th}>מלגה</th>
<th {th}>מירון</th><th {th}>נהיגה</th><th {th}>סה"כ</th></tr>
{''.join(lines)}
</table>
{"<p><b>מתחת למינימום החודש:</b> " + escape(", ".join(not_eligible)) + "</p>" if not_eligible else ""}
{"<p><b>נמחקים לפי כללי הכולל (חודשיים ללא מלגה):</b> " + escape(", ".join(removed)) + "</p>" if removed else ""}
<p style="color:#777">מצורף קובץ לאקסל עם כל הפרטים. הפירוט המלא בממשק הניהול.</p>
</div>"""

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["מספר", "שם", "לילות", "לילות שלמים", "זריזין", "אחוז נוכחות", "לילות (ש\"ח)", "תוספת 70/80",
                "תוספות ידניות", "מלגה", "עומד במינימום", "לילות מירון", "מירון", "הסעות", "נהיגה", "סה\"כ"])
    for e, s in rows:
        w.writerow([e["emp_number"], e["name"], s["nights"], s["whole"], s["early"], round(s["percent"] * 100, 1),
                    round(s["base"] + s["timing"], 2), s["percent_bonus"], s["extras"], s["stipend"],
                    "כן" if s["eligible"] else "לא", s["meron_nights"], s["meron"], s["drives"], s["driver"],
                    s["total"] if s["total"] is not None else ""])
    return f"סיכום כולל חצות - {month}", html, "﻿" + buf.getvalue()


def send(hy, hm, until=None):
    s = settings()
    if not enabled():
        return False, "REPORT_EMAIL, SMTP_USER and SMTP_PASSWORD are not set"
    subject, html, csv_text = build(hy, hm, until)
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = s["user"]
    msg["To"] = ", ".join(s["to"])
    msg.set_content("סיכום החודש מצורף. לתצוגה מלאה יש לפתוח את המייל בתצוגת HTML.")
    msg.add_alternative(html, subtype="html")
    msg.add_attachment(csv_text.encode("utf-8"), maintype="text", subtype="csv",
                       filename=f"summary-{hy}-{hm}.csv")
    try:
        with smtplib.SMTP(s["host"], s["port"], timeout=30) as smtp:
            smtp.starttls()
            smtp.login(s["user"], s["password"])
            smtp.send_message(msg)
    except (smtplib.SMTPException, OSError) as ex:
        return False, str(ex)
    return True, "sent"


def due(now):
    """(hy, hm, today) if the monthly report should go out now, else None."""
    today = now.date()
    hy, hm, hd = hebcal.from_gregorian(today)
    if hd >= REPORT_DAY and (hd > REPORT_DAY or now.hour >= REPORT_HOUR):
        return hy, hm, today
    return None


def run_once():
    """Send this month's report if due and not sent yet. Safe with several workers."""
    if not enabled():
        return None
    when = due(db.now())
    if not when:
        return None
    hy, hm, today = when
    key = f"report_sent_{hy}_{hm}"
    with db.connect() as c:
        claimed = c.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, 'sending')", (key,)).rowcount
    if not claimed:
        return None
    ok, info = send(hy, hm, until=today)
    if ok:
        db.set_setting(key, db.now().isoformat())
    else:
        with db.connect() as c:                       # failed: let the next check try again
            c.execute("DELETE FROM settings WHERE key=?", (key,))
        db.set_setting("report_last_error", f"{db.now().isoformat()} {info}")
    return ok


def start_background():
    def loop():
        while True:
            try:
                run_once()
            except Exception as ex:                   # never let the loop die
                db.set_setting("report_last_error", f"{db.now().isoformat()} {ex}")
            time.sleep(CHECK_EVERY_SECONDS)
    threading.Thread(target=loop, daemon=True, name="monthly-report").start()
