"""Monthly summary of the whole kollel, e-mailed on the 28th of every Hebrew month.

Settings (Render -> Environment):
  REPORT_EMAIL     where to send (one or more addresses, comma separated)
  SMTP_USER        the SMTP login (Gmail: the address; Brevo: the login shown under SMTP & API)
  SMTP_FROM        the sender address shown on the e-mail (default: SMTP_USER). Must be a verified sender.
  SMTP_PASSWORD    a Gmail "app password" (16 letters) - not the normal Gmail password
  SMTP_HOST / SMTP_PORT   default smtp.gmail.com / 587
  (the night summary goes out on the morning the night month closes - the 29th)
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

REPORT_HOUR = int(os.environ.get("REPORT_HOUR", 8))
CHECK_EVERY_SECONDS = 15 * 60


def settings():
    return {
        "to": [a.strip() for a in os.environ.get("REPORT_EMAIL", "").split(",") if a.strip()],
        "user": os.environ.get("SMTP_USER", ""),
        "from": os.environ.get("SMTP_FROM", "") or os.environ.get("SMTP_USER", ""),
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


def totals(rows):
    keys = ("kollel", "meron", "total", "jerusalem", "ohr_hatzadik")
    out = dict.fromkeys(keys, 0.0)
    for _e, s in rows:
        sp = schedule.payer_split(s)
        if sp:
            for k in keys:
                out[k] += sp[k]
    return {k: round(v, 2) for k, v in out.items()}


def build(hy, hm, until=None):
    """(subject, html, csv_text) for one Hebrew month."""
    month = f"{hebcal.month_name(hm, hy, 'he')} {hy}"
    rows = rows_for(hy, hm, until)
    first, last = schedule.night_month_range(hy, hm)
    upto = f" ({hebcal.format_date(first, 'he')} עד {hebcal.format_date(last, 'he')})"
    if until and until < last:
        upto += f" נכון ל{hebcal.format_date(until, 'he')}"
    t = totals(rows)
    not_eligible = [e["name"] for e, s in rows if e["role"] == "employee" and not s["eligible"]]
    removed = [e["name"] for e, _ in rows if schedule.removed_by_rule(e, until or schedule.night_month_range(hy, hm)[1])]

    th = 'style="background:#e8edf3;padding:6px 8px;border:1px solid #c9d1db;text-align:right"'
    td = 'style="padding:5px 8px;border:1px solid #dde3ea;text-align:right"'
    tdb = 'style="padding:6px 10px;border:1px solid #dde3ea;text-align:right;font-weight:bold"'
    money = lambda x: f"{x:,.0f}"

    summary = f"""<table style="border-collapse:collapse;margin:8px 0 16px" dir="rtl">
<tr><td {td}>סך הכל כולל</td><td {tdb}>{money(t["kollel"])} ש"ח</td></tr>
<tr><td {td}>סך הכל מירון</td><td {tdb}>{money(t["meron"])} ש"ח</td></tr>
<tr><td {td}>סך הכל, הכל ביחד</td><td {tdb}>{money(t["total"])} ש"ח</td></tr>
<tr><td {td}>לשלוח לכולל חצות ירושלים, סניף צפת</td><td {tdb}>{money(t["jerusalem"])} ש"ח</td></tr>
<tr><td {td}>הוצאות כולל חצות אור הצדיק מגן אברהם</td><td {tdb}>{money(t["ohr_hatzadik"])} ש"ח</td></tr>
</table>"""

    lines = []
    for e, s in rows:
        sp = schedule.payer_split(s) or {"kollel": 0, "meron": 0, "total": 0}
        hours = s["present_min"] / 60
        kollel = money(sp["kollel"]) if s["eligible"] else f'<span style="color:#b00020">0</span>'
        lines.append(
            f"<tr><td {td}>{escape(e['name'])}</td><td {td}>{round(s['percent'] * 100)}%</td>"
            f"<td {td}>{hours:.1f}</td><td {td}>{s['nights']}</td><td {td}>{s['whole']}</td><td {td}>{s['early']}</td>"
            f"<td {td}>{s['meron_nights']}</td><td {td}>{s['percent_bonus']}</td>"
            f"<td {td}>{kollel}</td><td {td}>{money(sp['meron'])}</td><td {td}><b>{money(sp['total'])}</b></td></tr>")

    html = f"""<div dir="rtl" style="font-family:Arial,sans-serif;font-size:14px;color:#222">
<h2 style="color:#1f3a5f;margin:0 0 8px">סיכום חודש {month}{upto}</h2>
{summary}
<h3 style="color:#1f3a5f;margin:12px 0 6px">נוכחות לפי אברך</h3>
<table style="border-collapse:collapse" dir="rtl">
<tr><th {th}>שם</th><th {th}>נוכחות</th><th {th}>שעות</th><th {th}>לילות</th><th {th}>שלמים</th>
<th {th}>זריזין מקדימים</th><th {th}>לילות מירון</th><th {th}>בונוס אחוזים</th>
<th {th}>כולל</th><th {th}>מירון</th><th {th}>סה"כ</th></tr>
{''.join(lines)}
</table>
<p style="color:#555">כולל = מלגת כל הלילות, תוספות ובונוס אחוזים. מירון = 35 ש"ח ללילה ו־60 ש"ח לנהיגה.
חלוקה: מירון כולו על כולל חצות ירושלים. בונוס האחוזים כולו על כולל אור הצדיק. השאר חצי חצי.</p>
{"<p><b>מתחת למינימום החודש (0 בעמודת כולל):</b> " + escape(", ".join(not_eligible)) + "</p>" if not_eligible else ""}
{"<p><b>נמחקים לפי כללי הכולל (חודשיים ללא מלגה):</b> " + escape(", ".join(removed)) + "</p>" if removed else ""}
<p style="color:#777">מצורף קובץ לאקסל עם כל הפרטים.</p>
</div>"""

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["מספר", "שם", "אחוז נוכחות", "שעות", "לילות", "לילות שלמים", "זריזין מקדימים", "לילות מירון",
                "בונוס אחוזים", "עומד במינימום", "כולל", "מירון", "סה\"כ", "חלק ירושלים", "חלק אור הצדיק"])
    for e, s in rows:
        sp = schedule.payer_split(s) or {}
        w.writerow([e["emp_number"], e["name"], round(s["percent"] * 100, 1), round(s["present_min"] / 60, 1),
                    s["nights"], s["whole"], s["early"], s["meron_nights"], s["percent_bonus"],
                    "כן" if s["eligible"] else "לא", sp.get("kollel", ""), sp.get("meron", ""), sp.get("total", ""),
                    sp.get("jerusalem", ""), sp.get("ohr_hatzadik", "")])
    w.writerow([])
    for label, k in (("סך הכל כולל", "kollel"), ("סך הכל מירון", "meron"), ("סך הכל הכל ביחד", "total"),
                     ("לשלוח לכולל חצות ירושלים", "jerusalem"), ("הוצאות כולל אור הצדיק", "ohr_hatzadik")):
        w.writerow(["", label, t[k]])
    return f"סיכום כולל חצות - {month}", html, "\ufeff" + buf.getvalue()


def build_day(hy, hm):
    """(subject, html, csv_text): the day kollel, rosh chodesh to rosh chodesh. Paid by Ohr Hatzadik."""
    month = f"{hebcal.month_name(hm, hy, 'he')} {hy}"
    with db.connect() as c:
        emps = c.execute("SELECT * FROM employees ORDER BY name").fetchall()
    rows = [(e, schedule.day_month_summary(e, hy, hm)) for e in emps if schedule.in_day(e)]
    total = round(sum(s["total"] for _, s in rows), 2)
    days = len(schedule.day_dates(hy, hm))
    th = 'style="background:#e8edf3;padding:6px 8px;border:1px solid #c9d1db;text-align:right"'
    td = 'style="padding:5px 8px;border:1px solid #dde3ea;text-align:right"'
    lines = []
    for e, s in rows:
        if s["vacation"]:
            lines.append(f"<tr><td {td}>{escape(e['name'])}</td><td {td} colspan='5'>אין כולל החודש, ממוצע 3 חודשים</td>"
                         f"<td {td}><b>{s['total']:,.0f}</b></td></tr>")
        else:
            lines.append(f"<tr><td {td}>{escape(e['name'])}</td><td {td}>{round(s['percent'] * 100)}%</td>"
                         f"<td {td}>{s['hours']:.1f}</td><td {td}>{s['days']} / {s['held']}</td><td {td}>{s['kept']}</td>"
                         f"<td {td}>{s['base']:,.0f}</td><td {td}><b>{s['total']:,.0f}</b></td></tr>")
    html = f"""<div dir="rtl" style="font-family:Arial,sans-serif;font-size:14px;color:#222">
<h2 style="color:#1f3a5f;margin:0 0 8px">כולל יום, סיכום חודש {month}</h2>
<p>סך הכל לתשלום: <b>{total:,.0f} ש"ח</b>, הכל על ידי כולל אור הצדיק. ימי כולל בחודש: {days}.</p>
<table style="border-collapse:collapse" dir="rtl">
<tr><th {th}>שם</th><th {th}>נוכחות</th><th {th}>שעות</th><th {th}>ימים</th><th {th}>שמירת הזמן</th>
<th {th}>לפי שעות</th><th {th}>סה"כ</th></tr>
{''.join(lines)}
</table>
<p style="color:#555">800 ש"ח לחודש לחלוקה לפי שעות הכולל. 200 ש"ח לחודש לשמירת הזמן, לחלוקה לפי ימי הכולל.
ניסן ותשרי לפי ממוצע שלושת החודשים הקודמים. אב: עד ז' באב לפי נוכחות, ושאר החודש לפי הממוצע.</p>
</div>"""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["מספר", "שם", "אחוז נוכחות", "שעות", "ימים", "ימי כולל", "שמירת הזמן", "לפי שעות", "שמירת הזמן (ש\"ח)", "סה\"כ"])
    for e, s in rows:
        w.writerow([e["emp_number"], e["name"], round(s["percent"] * 100, 1), s["hours"], s["days"], s["held"], s["kept"],
                    s["base"], s["keep"], s["total"]])
    w.writerow([])
    w.writerow(["", "סך הכל, על ידי כולל אור הצדיק", total])
    return f"סיכום כולל יום - {month}", html, "\ufeff" + buf.getvalue()


def send_day(hy, hm):
    return send(hy, hm, builder=lambda: build_day(hy, hm))


def send(hy, hm, until=None, builder=None):
    s = settings()
    if not enabled():
        return False, "REPORT_EMAIL, SMTP_USER and SMTP_PASSWORD are not set"
    subject, html, csv_text = builder() if builder else build(hy, hm, until)
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = s["from"]
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
    last = schedule.night_month_range(hy, hm)[1]          # the 29th: the night month closes that morning
    if today > last or (today == last and now.hour >= REPORT_HOUR):
        return hy, hm, today
    return None


def claim(key):
    with db.connect() as c:
        return c.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, 'sending')", (key,)).rowcount


def release(key, info):
    with db.connect() as c:
        c.execute("DELETE FROM settings WHERE key=?", (key,))
    db.set_setting("report_last_error", f"{db.now().isoformat()} {info}")


def run_day_once():
    """On the 1st of a Hebrew month (from REPORT_HOUR): the day kollel summary of the month that just ended."""
    if not enabled():
        return None
    now = db.now()
    hy, hm, hd = hebcal.from_gregorian(now.date())
    if hd != 1 or now.hour < REPORT_HOUR:
        return None
    py, pm = hebcal.prev_month(hy, hm)
    with db.connect() as c:
        anyone = c.execute("SELECT 1 FROM employees WHERE in_day=1 AND active=1").fetchone()
    if not anyone:
        return None
    key = f"report_day_sent_{py}_{pm}"
    if not claim(key):
        return None
    ok, info = send_day(py, pm)
    if ok:
        db.set_setting(key, now.isoformat())
    else:
        release(key, info)
    return ok


def run_once():
    """Send this month's report if due and not sent yet. Safe with several workers."""
    run_day_once()
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
