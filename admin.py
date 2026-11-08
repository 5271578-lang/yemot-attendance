"""Web admin: employees, attendance, payroll (by Hebrew month), message log."""
import csv
import io
import os
from datetime import datetime, timedelta
from functools import wraps

from flask import Blueprint, Response, abort, g, redirect, render_template_string, request, url_for
from werkzeug.security import check_password_hash, generate_password_hash

import db
import hebcal
import nbs
import schedule
import report
import importer
import moreshet
import tefila
import trips
import base64
import json
import secrets

bp = Blueprint("admin", __name__, url_prefix="/admin")
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
ROLE_NAMES = {"employee": "אברך", "onsite_manager": "מנהל במקום", "tech_manager": "מנהל טכני"}
LANGS = {"he": "עברית", "en": "אנגלית", "yi": "אידיש"}
PAYERS = {"ohr": "אור הצדיק", "jerusalem": "כולל חצות ירושלים", "shared": "משותף (חצי חצי)"}
STATUS_HE = {   # texts kept in English in the database, shown in Hebrew
    "pending": "ממתין", "approved": "אושר", "rejected": "נדחה", "sent": "נשלח", "removed by admin": "הוסר ידנית",
    "called": "התקשרו", "tzintuk": "צינתוק", "call failed": "השיחה נכשלה", "calling every 15 min": "מתקשרים כל רבע שעה",
    "new": "חדש", "exists": "כבר קיים", "duplicate": "כפול בקבצים", "no match": "לא נמצא אברך", "bad date": "תאריך לא תקין",
    "night": "חצות", "day": "הוראה", "manual": "ידני",
    "clock-in changed": "הכניסה עודכנה", "clock-out set": "היציאה עודכנה",
    "night added (exit counts as 7:30 unless corrected)": "נוסף לילה (היציאה נחשבת 7:30 אם לא תוקנה)",
    "clock-in would be after that day's clock-out": "הכניסה הייתה יוצאת אחרי היציאה של אותו יום",
    "no clock-in that day before this time - approve a clock-in correction first": "אין כניסה באותו יום לפני השעה הזאת - קודם לאשר תיקון כניסה",
}


def he(x):
    if not x:
        return x or ""
    x = str(x)
    if x in STATUS_HE:
        return STATUS_HE[x]
    if x.endswith(" (Meron)") and x[:-8] in STATUS_HE:
        return STATUS_HE[x[:-8]] + " (מירון)"
    return x
DEFAULT_PIN = "1480"            # initial password for every new avrech; he changes it with 9 -> 2


def remember_address():
    """The site's own address, for links in the e-mails (no need to set PUBLIC_URL by hand)."""
    try:
        if not db.get_setting("public_url") and request.host_url.startswith("https://"):
            db.set_setting("public_url", request.host_url.rstrip("/"))
    except Exception:
        pass


# A second login that sees only Kollel Horaah (set on the Kollel Horaah page by the main admin).
HORAAH_PAGES = {"admin.day_payroll", "admin.send_day_report", "admin.employees", "admin.edit_employee",
                "admin.employee_month", "admin.add_attendance", "admin.edit_attendance", "admin.corrections",
                "admin.horaah_guide"}


def is_full():
    return getattr(g, "role", "admin") == "admin"


def horaah_login_ok(user, password):
    u, h = db.get_setting("horaah_user"), db.get_setting("horaah_pw")
    return bool(u and h and user == u and check_password_hash(h, password))


@bp.before_request
def auth():
    remember_address()
    a = request.authorization
    if ADMIN_PASSWORD and a and a.username == ADMIN_USER and a.password == ADMIN_PASSWORD:
        g.role = "admin"
        return None
    if a and horaah_login_ok(a.username, a.password):
        g.role = "horaah"
        if request.endpoint not in HORAAH_PAGES:
            return redirect(url_for("admin.day_payroll"))
        return None
    return Response("נדרשת כניסה" if ADMIN_PASSWORD else "יש להגדיר ADMIN_PASSWORD בשרת",
                    401, {"WWW-Authenticate": 'Basic realm="Attendance"'})


def horaah_only_emp(e):
    """The Kollel Horaah login: only avrechim of Kollel Horaah."""
    if not is_full() and (e is None or not e["in_day"]):
        abort(403)


BASE = """<!doctype html><html lang="he" dir="rtl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>קו הנוכחות</title><style>
body{font-family:system-ui,Arial,sans-serif;margin:0;background:#f5f5f2;color:#222}
nav{background:#1f3a5f;padding:10px 16px;line-height:2}nav a{color:#fff;margin-left:16px;text-decoration:none;font-weight:600;white-space:nowrap}
main{padding:16px;max-width:1100px;margin:auto}table{border-collapse:collapse;width:100%;background:#fff}
th,td{border-bottom:1px solid #e3e3e3;padding:7px 9px;text-align:right;font-size:14px}th{background:#eef1f5}
.warn{color:#b00020;font-weight:600}.box{background:#fff;padding:14px;margin:14px 0;border:1px solid #ddd}
input,select,textarea{padding:5px;margin:3px 0 3px 6px;font-family:inherit}button{padding:6px 12px;background:#1f3a5f;color:#fff;border:0;cursor:pointer;font-family:inherit}
.muted{color:#777}.right{text-align:left}h1{font-size:22px}.wrap{overflow-x:auto}.num{direction:ltr;unicode-bidi:embed}
</style></head><body><nav>{% if not full %}<a href="{{ url_for('admin.day_payroll') }}">כולל הוראה</a>
<a href="{{ url_for('admin.employees', k='day') }}">אברכי הוראה</a><a href="{{ url_for('admin.corrections') }}">תיקונים</a>
<a href="{{ url_for('admin.horaah_guide') }}">הוראות</a>{% else %}<a href="{{ url_for('admin.payroll') }}">כולל חצות</a><a href="{{ url_for('admin.day_payroll') }}">כולל הוראה</a>
<a href="{{ url_for('admin.employees', k='night') }}">אברכי חצות</a><a href="{{ url_for('admin.employees', k='day') }}">אברכי הוראה</a><a href="{{ url_for('admin.corrections') }}">תיקונים</a>
<a href="{{ url_for('admin.messages') }}">הודעות</a><a href="{{ url_for('admin.shuttles') }}">הסעות</a>
<a href="{{ url_for('admin.import_page') }}">ייבוא</a><a href="{{ url_for('admin.registrations') }}">הרשמות</a><a href="{{ url_for('admin.tefila_page') }}">תפלה בכח</a><a href="{{ url_for('admin.gifts_page') }}">בונוסים</a><a href="{{ url_for('admin.moreshet_page') }}">מורשת הנחל</a><a href="{{ url_for('admin.requests_page') }}">בקשות</a><a href="{{ url_for('admin.horaah_guide') }}">הוראות להוראה</a>{% endif %}</nav>
<main>{% block body %}{% endblock %}</main></body></html>"""


def page(body, **ctx):
    ctx.setdefault("data_months", data_months)
    ctx.setdefault("program", "night")
    ctx.setdefault("he", he)
    ctx.setdefault("full", is_full())
    ctx.setdefault("payers", PAYERS)
    return render_template_string(BASE.replace("{% block body %}{% endblock %}", body), **ctx)


def data_months(program="night"):
    """[(heb_year, heb_month, name)] of every month that has attendance records, newest first.
    Night months run from the 27th to the 26th."""
    with db.connect() as c:
        if program == "night":
            got = {schedule.night_month_of(schedule.session_date(db.parse(x["clock_in"])))
                   for x in c.execute("SELECT clock_in FROM attendance WHERE program='night'")}
        else:
            got = {(x[0], x[1]) for x in c.execute("SELECT DISTINCT heb_year, heb_month FROM attendance WHERE program=?", (program,))}
    order = lambda m: (m[1] - 7) % 13            # Tishrei first within a year
    got = sorted(got, key=lambda m: (m[0], order(m)), reverse=True)
    return [(y, m, hebcal.heb_month_title(m, y)) for y, m in got]


def current_heb_month(night=True):
    if night:
        return schedule.night_month_of(schedule.session_date(db.now()))
    y, m, _ = hebcal.from_gregorian(db.now().date())
    return y, m


def month_args(night=True):
    cy, cm = current_heb_month(night)
    return int(request.args.get("hy", cy)), int(request.args.get("hm", cm))


def fmt(ts):
    return db.parse(ts).strftime("%d/%m/%Y %H:%M") if ts else ""


def hours_of(row):
    if not row["clock_out"]:
        return 0.0
    return round((db.parse(row["clock_out"]) - db.parse(row["clock_in"])).total_seconds() / 3600, 2)


def month_nights(emp, hy, hm, att):
    """[night_pay dicts] for one employee from that month's attendance rows."""
    by_night = {}
    for a in att:
        if a["employee_id"] == emp["id"]:
            by_night.setdefault(schedule.session_date(db.parse(a["clock_in"])), []).append(a)
    return [schedule.night_pay(emp, recs, d) for d, recs in sorted(by_night.items())], by_night


def payroll_rows(hy, hm):
    with db.connect() as c:
        emps = c.execute("SELECT * FROM employees ORDER BY active DESC, name").fetchall()
    out = []
    for e in emps:
        m = schedule.month_summary(e, hy, hm)
        if not m["nights"] and not m["extras"] and not m["gifts"] and (not e["active"] or not schedule.in_night(e)):
            continue
        out.append(dict(emp=e, nightly=schedule.nightly_base(e), **m))
    return out


@bp.route("/")
def payroll():
    hy, hm = month_args()
    rows = payroll_rows(hy, hm)
    first, last = schedule.night_month_range(hy, hm)
    py, pm = hebcal.prev_month(hy, hm)
    ny, nm = hebcal.next_month(hy, hm)
    body = """
<h1>כולל חצות · {{ title }}</h1>
<p class="muted"><span class="num">{{ first.strftime('%d/%m/%Y') }} – {{ last.strftime('%d/%m/%Y') }}</span> ·
<a href="?hy={{py}}&hm={{pm}}">▶ חודש קודם</a> · <a href="?hy={{ny}}&hm={{nm}}">חודש הבא ◀</a> ·
<br>חודשים עם נתונים: {% for y, m, name in data_months(program) %}<a href="?hy={{y}}&hm={{m}}" {% if y == hy and m == hm %}style="font-weight:700;text-decoration:none"{% endif %}>{{ name }}</a>{{ ' · ' if not loop.last }}{% endfor %} ·
<a href="{{ url_for('admin.export', hy=hy, hm=hm) }}">הורדה לאקסל (CSV)</a> ·
<form method="post" action="{{ url_for('admin.send_report', hy=hy, hm=hm) }}" style="display:inline">
<button>לשלוח את הסיכום במייל עכשיו</button></form>{% if msg %} <span class="warn">{{ msg }}</span>{% endif %}</p>
<div class="wrap"><table><tr><th>מס'</th><th>שם</th><th class="right">לילות</th><th class="right">שלמים</th>
<th class="right">נוכחות</th><th class="right">לפי לילה</th><th class="right">70/80%</th><th class="right">תוספות</th>
<th class="right">מלגה</th><th class="right">מירון</th><th class="right">נהג</th><th class="right">בונוס</th><th class="right">סה"כ ₪</th></tr>
{% for r in rows %}<tr><td>{{ r.emp.emp_number }}</td>
<td><a href="{{ url_for('admin.employee_month', eid=r.emp.id, hy=hy, hm=hm) }}">{{ r.emp.name }}</a>
<span class="muted">{{ roles[r.emp.role] if r.emp.role != 'employee' else '' }}</span></td>
<td class="right">{{ r.nights }}</td><td class="right">{{ r.whole }}</td><td class="right">{{ (r.percent * 100)|round(1) }}%</td>
<td class="right">{{ '%.2f'|format(r.base + r.timing) }}</td><td class="right">{{ r.percent_bonus }}</td>
<td class="right">{{ '%.2f'|format(r.extras) }}</td>
<td class="right">{% if r.eligible %}{{ '%.2f'|format(r.stipend) }}{% else %}<span class="warn" title="מתחת למינימום">0</span>
<span class="muted">({{ '%.0f'|format(r.stipend) }})</span>{% endif %}</td>
<td class="right">{{ r.meron }}</td><td class="right">{{ r.driver }}</td><td class="right">{{ r.gifts or '' }}</td>
<td class="right"><b>{% if r.total is none %}<span class="warn">לא נקבע תעריף</span>{% else %}{{ '%.2f'|format(r.total) }}{% endif %}</b></td>
</tr>{% endfor %}
</table></div>
<div class="box"><b>סיכום:</b> כולל {{ '{:,.0f}'.format(t.kollel) }} · מירון {{ '{:,.0f}'.format(t.meron) }} ·
{% if t.gifts %}בונוסים נפרדים {{ '{:,.0f}'.format(t.gifts) }} · {% endif %}
הכל ביחד <b>{{ '{:,.0f}'.format(t.total) }}</b> · לכולל חצות ירושלים (סניף צפת) <b>{{ '{:,.0f}'.format(t.jerusalem) }}</b> ·
אור הצדיק <b>{{ '{:,.0f}'.format(t.ohr_hatzadik) }}</b>
<div class="muted">מירון (35 ללילה, 60 לנהג) ← ירושלים. בונוסי 70/80% ← אור הצדיק. בונוסים נפרדים ← לפי מה שסומן בכל בונוס. השאר: חצי חצי.</div></div>
<p class="muted">חודש המלגה של הלילה: מראש חודש (גם א' דראש חודש) עד כ"ט. לפי לילה: 80 × החלק מזמן הכולל שנכח (<span class="num">2:30–7:30</span>, בין הזמנים <span class="num">3:30–7:30</span>), +20 עד 10 דקות איחור,
+30 לפני ההתחלה. לילה בלי יציאה נחשב עד 7:30. אחוז הנוכחות לפי זמן בלבד.
70% ← +200, 80% ← עוד +100. פחות מ־2 לילות שלמים או פחות מ־500 (בלי מירון) ← אין מלגה (0 באדום).
מירון: 35 ללילה. נהג: 60 ללילה. תוספות = לילות אומן ותוספות למלגה שהוכנסו ידנית (חלק מהמלגה).
בונוס = בונוס נפרד, לא חלק מהמלגה: משולם תמיד, ולא נספר למינימום ולאחוזים.</p>"""
    title = hebcal.heb_month_title(hm, hy)
    return page(body, rows=rows, hy=hy, hm=hm, py=py, pm=pm, ny=ny, nm=nm, first=first, last=last,
                title=title, roles=ROLE_NAMES, msg=request.args.get("msg"),
                t=report.totals([(r["emp"], r) for r in rows]))


@bp.route("/send-report", methods=["POST"])
def send_report():
    hy, hm = month_args()
    ok, info = report.send(hy, hm)
    msg = "הסיכום נשלח במייל." if ok else f"לא נשלח: {info}"
    return redirect(url_for("admin.payroll", hy=hy, hm=hm, msg=msg))


@bp.route("/export.csv")
def export():
    hy, hm = month_args()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["מס' אברך", "שם", "תפקיד", "לילות", "לילות שלמים", "% נוכחות", "לפי לילה", "בונוס 70/80%",
                "תוספות", "מלגה", "זכאי", "לילות מירון", "מירון", "נסיעות", "נהג", "סה\"כ"])
    for r in payroll_rows(hy, hm):
        w.writerow([r["emp"]["emp_number"], r["emp"]["name"], ROLE_NAMES[r["emp"]["role"]], r["nights"], r["whole"],
                    round(r["percent"] * 100, 1), round(r["base"] + r["timing"], 2), r["percent_bonus"], r["extras"],
                    r["stipend"], "כן" if r["eligible"] else "לא", r["meron_nights"], r["meron"], r["drives"],
                    r["driver"], r["total"] if r["total"] is not None else ""])
    name = f"payroll-{hebcal.month_name(hm, hy).replace(' ', '')}-{hy}.csv"
    return Response("﻿" + buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={name}"})


@bp.route("/employee/<int:eid>")
def employee_month(eid):
    hy, hm = month_args()
    with db.connect() as c:
        e = c.execute("SELECT * FROM employees WHERE id=?", (eid,)).fetchone()
        horaah_only_emp(e)
        nfirst, nlast = schedule.night_month_range(hy, hm)
        recs = [x for x in c.execute("SELECT * FROM attendance WHERE employee_id=? ORDER BY clock_in", (eid,)).fetchall()
                if (x["program"] == "night" and nfirst <= schedule.session_date(db.parse(x["clock_in"])) <= nlast)
                or (x["program"] == "day" and (x["heb_year"], x["heb_month"]) == (hy, hm))]
        if not is_full():
            recs = [x for x in recs if x["program"] == "day"]
        uman = c.execute("SELECT * FROM uman WHERE employee_id=? ORDER BY heb_year DESC", (eid,)).fetchall()
        bons = c.execute("SELECT * FROM bonuses WHERE employee_id=? AND heb_year=? AND heb_month=?",
                         (eid, hy, hm)).fetchall()
        gifts = c.execute("SELECT * FROM gifts WHERE employee_id=? AND heb_year=? AND heb_month=? ORDER BY created_at",
                          (eid, hy, hm)).fetchall()
    body = """
<h1>{{ e.name }} · {{ title }}</h1><p><a href="{{ url_for('admin.payroll' if full else 'admin.day_payroll', hy=hy, hm=hm) }}">→ חזרה</a> ·
<a href="?hy={{py[0]}}&hm={{py[1]}}">▶ חודש קודם</a> · <a href="?hy={{ny[0]}}&hm={{ny[1]}}">חודש הבא ◀</a>
{% if full %}<span class="muted"> · חודש הלילה {{ heb(nfirst) }} – {{ heb(nlast) }}{% if e.in_day %}; כולל הוראה: לפי החודש העברי{% endif %}</span>{% endif %}</p>
<form method="post" action="{{ url_for('admin.set_meron', eid=e.id, hy=hy, hm=hm) }}">
<div class="wrap"><table><tr><th>תאריך</th><th>כניסה</th><th>יציאה</th><th class="right">דקות</th>
<th class="right">בסיס</th><th class="right">בזמן</th><th class="right">לילה ₪</th><th>מירון</th>{% if e.is_driver %}<th>נהג</th>{% endif %}<th>הערה</th><th></th></tr>
{% for a in recs %}{% set n = pay.get(a.id) %}<tr><td>{{ gem(a.heb_day) }} {{ title }}{% if n and n.bhz %} · <span class="muted">בין הזמנים</span>{% endif %}</td>
<td class="num">{{ fmt(a.clock_in) }}{% if a.program == 'day' %} <span class="muted">· כולל הוראה</span>{% endif %}{% if a.meron %} <span class="muted">· מירון</span>{% endif %}
{% if a.actual_in and a.actual_in != a.clock_in %}<br><span class="muted">התקשר {{ fmt(a.actual_in) }}</span>{% endif %}</td>
<td>{% if a.clock_out %}<span class="num">{{ fmt(a.clock_out) }}</span>{% if a.early_exit %} <span class="muted">מוקדם</span>{% endif %}{% else %}<span class="muted">7:30 (ברירת מחדל)</span>{% endif %}
{% if not a.exit_confirmed %} <span class="muted">· לא אושר</span>{% endif %}</td>
<td class="right">{{ n.minutes if n else '' }}</td><td class="right">{{ n.base if n else '' }}</td>
<td class="right">{{ n.bonus if n else '' }}</td><td class="right"><b>{{ n.total if n else '' }}</b></td>
<td>{% if n %}<input type="checkbox" name="meron" value="{{ a.id }}" {% if n.meron %}checked{% endif %}>{% endif %}</td>
{% if e.is_driver %}<td>{% if n %}<input type="checkbox" name="drove" value="{{ a.id }}" {% if session_of(a) in drove %}checked{% endif %}>{% endif %}</td>{% endif %}
<td>{{ a.note or '' }}</td>
<td><a href="{{ url_for('admin.edit_attendance', aid=a.id) }}">עריכה</a></td></tr>{% endfor %}</table></div>
{% if pay %}<button style="margin-top:8px">שמירת מירון{{ ' / נהיגה' if e.is_driver else '' }}</button>
<span class="muted"> מסמנים את הלילות שנסע למירון{{ ' / נהג בהסעה' if e.is_driver else '' }} ולוחצים שמירה.</span>{% endif %}</form>
{% if full %}<div class="box"><b>החודש:</b> {{ sm.nights }} לילות · {{ sm.whole }} שלמים · {{ sm.early }} מוקדמים ·
נוכחות {{ (sm.percent * 100)|round(1) }}% · מלגה {{ '%.2f'|format(sm.stipend) }}{% if not sm.eligible %} <span class="warn">(מתחת למינימום ← 0)</span>{% endif %}
· מירון {{ sm.meron_nights }} ({{ sm.meron }}) · נהג {{ sm.drives }} ({{ sm.driver }}) · <b>סה"כ {{ sm.total }}</b></div>{% endif %}
{% if dsm %}<div class="box"><b>כולל הוראה:</b> {% if dsm.vacation %}אין כולל החודש, ממוצע 3 החודשים האחרונים{% else %}
{{ dsm.days }}/{{ dsm.held }} ימים · בזמן {{ dsm.kept }} · {{ dsm.hours }} שעות{% endif %} · <b>{{ '%.2f'|format(dsm.total) }}</b></div>{% endif %}
{% if uman and full %}<div class="box"><b>אומן</b><table>{% for u in uman %}<tr><td>{{ u.heb_year }}</td>
<td>{{ 'נסע' if u.went else 'לא נסע' }}</td><td>{% if u.went %}{{ (u.percent * 100)|round(1) }}% · לילות: {{ u.nights or 'לא נמצאו' }}{% endif %}</td>
</tr>{% endfor %}</table></div>{% endif %}
{% if full and e.in_night %}<div class="box"><form method="post" action="{{ url_for('admin.fill_month', eid=e.id, hy=hy, hm=hm) }}"
onsubmit="return confirm('לרשום ל{{ e.name }} לילה שלם בכל לילה שהכולל התקיים בחודש הזה (עד היום)? כניסות שכבר קיימות יוקדמו לתחילת הכולל, והיציאה תהיה בסוף.')">
<button>נוכחות מלאה לכל החודש</button> <span class="muted">לילה שלם בכל לילה שהכולל התקיים בחודש הזה, עד היום.</span></form></div>{% endif %}
<div class="box"><b>הוספת משמרת ידנית</b><form method="post" action="{{ url_for('admin.add_attendance', eid=e.id) }}">
כניסה <input type="datetime-local" name="clock_in" required> יציאה <input type="datetime-local" name="clock_out">
הערה <input name="note"> <button>הוספה</button>{% if not full %} <span class="muted">(רק בשעות כולל הוראה, 14:00–20:00)</span>{% endif %}</form></div>
{% if full %}<div class="box"><b>בונוס נפרד</b> <span class="muted">(לא חלק מהמלגה: משולם תמיד, ולא נספר למינימום ולאחוזים)</span>
<table>{% for g in gifts %}<tr><td>{{ g.note or '' }}</td><td class="right">{{ '%.2f'|format(g.amount) }} ₪</td>
<td><form method="post" action="{{ url_for('admin.gift_payer', gid=g.id) }}" style="margin:0">משלם: <select name="payer" onchange="this.form.submit()">{% for k, v in payers.items() %}<option value="{{ k }}" {{ 'selected' if (g.payer or 'ohr') == k else '' }}>{{ v }}</option>{% endfor %}</select></form></td>
<td class="muted">{{ fmt(g.created_at) }}{{ ' · בטלפון' if g.added_by and g.added_by > 0 else '' }}</td>
<td><form method="post" action="{{ url_for('admin.delete_gift', gid=g.id) }}"><button>הסרה</button></form></td></tr>{% endfor %}</table>
<form method="post" action="{{ url_for('admin.add_gift', eid=e.id, hy=hy, hm=hm) }}">
סכום ₪ <input name="amount" type="number" step="0.01" required> עבור <input name="note">
משלם <select name="payer">{% for k, v in payers.items() %}<option value="{{ k }}">{{ v }}</option>{% endfor %}</select> <button>הוספת בונוס נפרד</button></form></div>
<div class="box"><b>תוספות למלגה החודש</b> <span class="muted">(חלק מהמלגה)</span><table>{% for b in bons %}<tr><td>{{ b.note or '' }}</td>
<td class="right">{{ '%.2f'|format(b.amount) }} ₪</td><td><form method="post" action="{{ url_for('admin.delete_bonus', bid=b.id) }}">
<button>הסרה</button></form></td></tr>{% endfor %}</table>
<form method="post" action="{{ url_for('admin.add_bonus', eid=e.id, hy=hy, hm=hm) }}">
סכום ₪ <input name="amount" type="number" step="0.01" required> עבור <input name="note" placeholder="לדוגמה: נהג הסעה"> <button>הוספת תוספת</button></form></div>{% endif %}"""
    nights, by_night = month_nights(e, hy, hm, [x for x in recs if x["program"] == "night"])
    dsm = schedule.day_month_summary(e, hy, hm) if schedule.in_day(e) else None
    pay = {}
    for n, (d, rs) in zip(nights, sorted(by_night.items())):
        pay[rs[0]["id"]] = n
    sm = schedule.month_summary(e, hy, hm)
    drove = set(schedule.driver_nights(eid, nfirst, nlast))
    return page(body, e=e, recs=recs, bons=bons, gifts=gifts, uman=uman, pay=pay, sm=sm, dsm=dsm, hy=hy, hm=hm, drove=drove,
                session_of=lambda a: schedule.session_date(db.parse(a["clock_in"])),
                title=hebcal.heb_month_title(hm, hy), fmt=fmt, heb=hebcal.heb_date, gem=hebcal.gematria, nfirst=nfirst, nlast=nlast,
                py=hebcal.prev_month(hy, hm), ny=hebcal.next_month(hy, hm))


@bp.route("/employee/<int:eid>/meron", methods=["POST"])
def set_meron(eid):
    """Meron nights (and shuttle drives) ticked by hand - for nights the phone line did not record them."""
    hy, hm = month_args()
    meron = {int(x) for x in request.form.getlist("meron")}
    drove = {int(x) for x in request.form.getlist("drove")}
    first, last = schedule.night_month_range(hy, hm)
    nights = schedule.nights_for(eid, first, last)
    with db.connect() as c:
        e = c.execute("SELECT * FROM employees WHERE id=?", (eid,)).fetchone()
        for d, rs in nights.items():
            ids = {r["id"] for r in rs}
            on = bool(ids & meron)
            c.execute(f"UPDATE attendance SET meron=? WHERE id IN ({','.join('?' * len(ids))})", (1 if on else 0, *ids))
        if e["is_driver"]:
            had = set(schedule.driver_nights(eid, first, last))
            want = {d for d, rs in nights.items() if {r["id"] for r in rs} & drove}
            for d in want - had:
                c.execute("INSERT INTO shuttle_runs(driver_id, created_at, shuttle_group, recipients, campaign_id, status) "
                          "VALUES (?,?,?,?,?,'sent')", (eid, datetime.combine(d, datetime.min.time(), tzinfo=db.TZ)
                                                         .replace(hour=1).isoformat(), e["shuttle_group"] or 0, "[]", "manual"))
            for d in had - want:
                for r in c.execute("SELECT id, created_at FROM shuttle_runs WHERE driver_id=? AND status='sent'", (eid,)).fetchall():
                    if schedule.session_date(db.parse(r["created_at"])) == d:
                        c.execute("UPDATE shuttle_runs SET status='removed by admin' WHERE id=?", (r["id"],))
    return redirect(url_for("admin.employee_month", eid=eid, hy=hy, hm=hm))


def parse_local(value):
    if not value:
        return None
    return datetime.fromisoformat(value).replace(tzinfo=db.TZ)


def program_of(dt):
    return "day" if 14 <= dt.hour < 20 else "night"


def page_month(dt):
    """The month page a record shows on: night records by the 27th-26th night month, day records by the calendar."""
    if program_of(dt) == "night":
        return schedule.night_month_of(schedule.session_date(dt))
    return hebcal.from_gregorian(dt.date())[:2]


def heb_fields(dt):
    return hebcal.from_gregorian(schedule.session_date(dt))


@bp.route("/employee/<int:eid>/fill-month", methods=["POST"])
def fill_month(eid):
    import corrections
    hy, hm = month_args()
    corrections.full_month(eid, hy, hm)
    return redirect(url_for("admin.employee_month", eid=eid, hy=hy, hm=hm))


@bp.route("/employee/<int:eid>/attendance", methods=["POST"])
def add_attendance(eid):
    t_in, t_out = parse_local(request.form["clock_in"]), parse_local(request.form.get("clock_out"))
    with db.connect() as c:
        horaah_only_emp(c.execute("SELECT * FROM employees WHERE id=?", (eid,)).fetchone())
    if not is_full() and program_of(t_in) != "day":
        abort(403)
    y, m, d = heb_fields(t_in)
    with db.connect() as c:
        rec, meron = meron_rule(c, eid, t_in)          # riders: 2:25, with Meron inside the shuttle window
        c.execute("INSERT INTO attendance(employee_id, clock_in, clock_out, actual_in, meron, heb_year, heb_month, heb_day, "
                  "note, exit_confirmed, program) VALUES (?,?,?,?,?,?,?,?,?,1,?)",
                  (eid, rec.isoformat(), t_out.isoformat() if t_out else None, t_in.isoformat() if rec != t_in else None,
                   meron, y, m, d, request.form.get("note") or "added by admin", program_of(t_in)))
    hy, hm = page_month(t_in)
    return redirect(url_for("admin.employee_month", eid=eid, hy=hy, hm=hm))


@bp.route("/attendance/<int:aid>", methods=["GET", "POST"])
def edit_attendance(aid):
    with db.connect() as c:
        a = c.execute("SELECT * FROM attendance WHERE id=?", (aid,)).fetchone()
        if not is_full():
            horaah_only_emp(c.execute("SELECT * FROM employees WHERE id=?", (a["employee_id"],)).fetchone())
            if a["program"] != "day" or (request.method == "POST" and request.form.get("clock_in")
                                         and program_of(parse_local(request.form["clock_in"])) != "day"):
                abort(403)
        if request.method == "POST":
            if request.form.get("delete"):
                c.execute("DELETE FROM attendance WHERE id=?", (aid,))
            else:
                t_in, t_out = parse_local(request.form["clock_in"]), parse_local(request.form.get("clock_out"))
                y, m, d = heb_fields(t_in)
                actual, meron = a["actual_in"], a["meron"]
                if t_in != db.parse(a["clock_in"]).replace(second=0, microsecond=0):    # the clock-in was changed
                    rec, meron = meron_rule(c, a["employee_id"], t_in)
                    actual = t_in.isoformat() if rec != t_in else None
                    t_in = rec
                c.execute("UPDATE attendance SET clock_in=?, clock_out=?, actual_in=?, meron=?, heb_year=?, heb_month=?, "
                          "heb_day=?, note=?, exit_confirmed=1 WHERE id=?",
                          (t_in.isoformat(), t_out.isoformat() if t_out else None, actual, meron, y, m, d,
                           request.form.get("note"), aid))
            hy, hm = page_month(db.parse(a["clock_in"]))
            return redirect(url_for("admin.employee_month", eid=a["employee_id"], hy=hy, hm=hm))
    local = lambda ts: db.parse(ts).strftime("%Y-%m-%dT%H:%M") if ts else ""
    body = """<h1>עריכת משמרת</h1><div class="box"><form method="post">
כניסה <input type="datetime-local" name="clock_in" value="{{ local(a.clock_in) }}" required>
יציאה <input type="datetime-local" name="clock_out" value="{{ local(a.clock_out) }}">
הערה <input name="note" value="{{ a.note or '' }}"> <button>שמירה</button></form>
<form method="post" onsubmit="return confirm('למחוק את המשמרת?')"><input type="hidden" name="delete" value="1">
<button style="background:#b00020;margin-top:10px">מחיקת המשמרת</button></form></div>"""
    return page(body, a=a, local=local)


@bp.route("/employee/<int:eid>/bonus", methods=["POST"])
def add_bonus(eid):
    hy, hm = month_args()
    with db.connect() as c:
        c.execute("INSERT INTO bonuses(employee_id, heb_year, heb_month, amount, note) VALUES (?,?,?,?,?)",
                  (eid, hy, hm, float(request.form["amount"]), request.form.get("note")))
    return redirect(url_for("admin.employee_month", eid=eid, hy=hy, hm=hm))


@bp.route("/employee/<int:eid>/gift", methods=["POST"])
def add_gift(eid):
    hy, hm = month_args()
    with db.connect() as c:
        c.execute("INSERT INTO gifts(employee_id, heb_year, heb_month, amount, note, created_at, added_by, payer) VALUES (?,?,?,?,?,?,?,?)",
                  (eid, hy, hm, float(request.form["amount"]), request.form.get("note"), db.now().isoformat(), -1, payer_arg()))
    return redirect(url_for("admin.employee_month", eid=eid, hy=hy, hm=hm))


def payer_arg():
    p = request.form.get("payer", "ohr")
    return p if p in PAYERS else "ohr"


@bp.route("/gift/<int:gid>/payer", methods=["POST"])
def gift_payer(gid):
    with db.connect() as c:
        g = c.execute("SELECT * FROM gifts WHERE id=?", (gid,)).fetchone()
        c.execute("UPDATE gifts SET payer=? WHERE id=?", (payer_arg(), gid))
    if request.form.get("back") == "list":
        return redirect(url_for("admin.gifts_page", hy=g["heb_year"], hm=g["heb_month"]))
    return redirect(url_for("admin.employee_month", eid=g["employee_id"], hy=g["heb_year"], hm=g["heb_month"]))


@bp.route("/gift/<int:gid>/delete", methods=["POST"])
def delete_gift(gid):
    with db.connect() as c:
        g = c.execute("SELECT * FROM gifts WHERE id=?", (gid,)).fetchone()
        c.execute("DELETE FROM gifts WHERE id=?", (gid,))
    if request.form.get("back") == "list":
        return redirect(url_for("admin.gifts_page", hy=g["heb_year"], hm=g["heb_month"]))
    return redirect(url_for("admin.employee_month", eid=g["employee_id"], hy=g["heb_year"], hm=g["heb_month"]))


@bp.route("/requests", methods=["GET", "POST"])
def requests_page():
    """Requests for changes in the system, said on the phone (9 -> 9 -> 2)."""
    import assistant
    if request.method == "POST":
        with db.connect() as c:
            c.execute("UPDATE change_requests SET status=?, note=? WHERE id=?",
                      (request.form.get("status", "open"), request.form.get("note", ""), int(request.form["id"])))
        return redirect(url_for("admin.requests_page"))
    rows = assistant.requests_list()
    body = """<h1>בקשות מהקו</h1>
<p class="muted">מנהלים: מקישים 9, אחר כך 9, אחר כך 2, ואומרים את השינוי המבוקש. כשהוא בוצע, מסמנים כאן "בוצע".
פעולות בדיבור (9, 9, 1): {{ 'פעילות' if on else 'כבויות - צריך להגדיר ANTHROPIC_API_KEY בשרת' }}.</p>
<p>כתובת לקריאה בלבד של הבקשות הפתוחות (לתת לClaude): <code dir="ltr">{{ link }}</code></p>
<table><tr><th>מתי</th><th>מי</th><th>בקשה</th><th>מצב</th><th></th></tr>
{% for x in rows %}<tr><td class="num">{{ fmt(x.created_at) }}</td><td>{{ x.name }}</td><td>{{ x.text }}</td>
<td><form method="post" style="margin:0"><input type="hidden" name="id" value="{{ x.id }}">
<select name="status"><option value="open" {{ 'selected' if x.status=='open' else '' }}>פתוחה</option>
<option value="done" {{ 'selected' if x.status=='done' else '' }}>בוצע</option>
<option value="no" {{ 'selected' if x.status=='no' else '' }}>לא לביצוע</option></select>
<input name="note" value="{{ x.note or '' }}" placeholder="הערה"></td><td><button>שמירה</button></form></td></tr>
{% else %}<tr><td colspan="5" class="muted">אין בקשות עדיין.</td></tr>{% endfor %}</table>"""
    link = request.host_url.rstrip("/").replace("http://", "https://") + "/open-requests?key=" + assistant.read_key()
    return page(body, rows=rows, fmt=fmt, on=assistant.enabled(), link=link)


@bp.route("/bonuses", methods=["GET", "POST"])
def gifts_page():
    """Separate bonuses (not part of the stipend) of one Kollel Chatzos month, for all avrechim."""
    import expenses
    hy, hm = month_args()
    if request.method == "POST" and request.form.get("line_exp"):
        fy, fm = (int(x) for x in request.form["from"].split("-"))
        expenses.save(request.form.get("emp"), expenses.parse_lines(request.form.get("items")), (fy, fm))
        return redirect(url_for("admin.gifts_page", hy=hy, hm=hm))
    if request.method == "POST":
        amount = float(request.form["amount"])
        with db.connect() as c:
            for eid in request.form.getlist("employee_id"):
                c.execute("INSERT INTO gifts(employee_id, heb_year, heb_month, amount, note, created_at, added_by, payer) "
                          "VALUES (?,?,?,?,?,?,?,?)", (int(eid), hy, hm, amount, request.form.get("note"), db.now().isoformat(), -1, payer_arg()))
        return redirect(url_for("admin.gifts_page", hy=hy, hm=hm))
    with db.connect() as c:
        rows = c.execute("SELECT g.*, e.name, e.emp_number, b.name AS by_name FROM gifts g JOIN employees e ON e.id=g.employee_id "
                         "LEFT JOIN employees b ON b.id=g.added_by WHERE g.heb_year=? AND g.heb_month=? ORDER BY g.created_at DESC",
                         (hy, hm)).fetchall()
        avrechim = c.execute("SELECT id, name, emp_number FROM employees WHERE active=1 ORDER BY name").fetchall()
    py, pm = hebcal.prev_month(hy, hm)
    ny, nm = hebcal.next_month(hy, hm)
    body = """<h1>בונוסים נפרדים · {{ title }}</h1>
<p class="muted">לא חלק מהמלגה: משולמים תמיד, ולא נספרים למינימום ולאחוזים. ליד כל בונוס בוחרים מי משלם: אור הצדיק, כולל חצות ירושלים, או משותף חצי חצי.
<a href="?hy={{py}}&hm={{pm}}">▶ חודש קודם</a> · <a href="?hy={{ny}}&hm={{nm}}">חודש הבא ◀</a></p>
<div class="box"><b>הוספה</b><form method="post">
<select name="employee_id" multiple size="7" style="min-width:280px" required>{% for e in avrechim %}<option value="{{ e.id }}">{{ e.name }} ({{ e.emp_number }})</option>{% endfor %}</select>
סכום ₪ <input name="amount" type="number" step="0.01" required> עבור <input name="note">
משלם <select name="payer">{% for k, v in payers.items() %}<option value="{{ k }}">{{ v }}</option>{% endfor %}</select> <button>הוספת בונוס</button>
<span class="muted">Ctrl + לחיצה כדי לתת את אותו בונוס לכמה אברכים.</span></form></div>
<div class="box"><b>הוצאות הקו, נוספות אוטומטית כל חודש</b>
<form method="post"><input type="hidden" name="line_exp" value="1">
למי: <select name="emp"><option value="">- לאף אחד (הופסק) -</option>{% for e in avrechim %}<option value="{{ e.id }}" {{ 'selected' if e.id == lx_emp else '' }}>{{ e.name }}</option>{% endfor %}</select>
מהחודש: <select name="from">{% for y, m, label in lx_months %}<option value="{{ y }}-{{ m }}" {{ 'selected' if (y, m) == lx_from else '' }}>{{ label }}</option>{% endfor %}</select><br>
<textarea name="items" rows="4" style="width:420px">{% for n, a in lx_items %}{{ n }} = {{ '%g'|format(a) }}
{% endfor %}</textarea><br>
<span class="muted">הוצאה אחת בכל שורה: שם = סכום בש"ח. סה"כ עכשיו: ₪{{ '%.2f'|format(lx_total) }}.
נוסף פעם אחת בתחילת כל חודש של כולל חצות כבונוס נפרד ("הוצאות הקו"). שינוי חל מהחודש הבא שיתווסף.</span>
<button>שמירה</button></form></div>
<div class="wrap"><table><tr><th>מס'</th><th>שם</th><th class="right">סכום</th><th>עבור</th><th>משלם</th><th>נוסף</th><th></th></tr>
{% for g in rows %}<tr><td>{{ g.emp_number }}</td><td><a href="{{ url_for('admin.employee_month', eid=g.employee_id, hy=hy, hm=hm) }}">{{ g.name }}</a></td>
<td class="right">{{ '%.2f'|format(g.amount) }}</td><td>{{ g.note or '' }}</td>
<td><form method="post" action="{{ url_for('admin.gift_payer', gid=g.id) }}" style="margin:0"><input type="hidden" name="back" value="list"><select name="payer" onchange="this.form.submit()">{% for k, v in payers.items() %}<option value="{{ k }}" {{ 'selected' if (g.payer or 'ohr') == k else '' }}>{{ v }}</option>{% endfor %}</select></form></td>
<td class="muted">{{ fmt(g.created_at) }}{% if g.by_name %} · ע"י {{ g.by_name }} (בטלפון){% endif %}</td>
<td><form method="post" action="{{ url_for('admin.delete_gift', gid=g.id) }}"><input type="hidden" name="back" value="list"><button>הסרה</button></form></td></tr>
{% else %}<tr><td colspan="7" class="muted">אין בונוסים החודש.</td></tr>{% endfor %}
{% if rows %}<tr><td></td><td><b>סה"כ</b></td><td class="right"><b>{{ '%.2f'|format(rows|sum(attribute='amount')) }}</b></td><td colspan="4" class="muted">
{% for k, v in payers.items() %}{% set n = rows|selectattr('payer', 'equalto', k)|sum(attribute='amount') %}{% if n %}{{ v }}: {{ '%.2f'|format(n) }} · {% endif %}{% endfor %}</td></tr>{% endif %}
</table></div>"""
    months, (y, m) = [], (hy, hm)
    for _ in range(14):
        months.append((y, m, hebcal.heb_month_title(m, y)))
        y, m = hebcal.next_month(y, m)
    lx_from = expenses.first_month()
    if lx_from not in [(a, b) for a, b, _l in months]:
        months.insert(0, (lx_from[0], lx_from[1], hebcal.heb_month_title(lx_from[1], lx_from[0])))
    return page(body, rows=rows, avrechim=avrechim, hy=hy, hm=hm, py=py, pm=pm, ny=ny, nm=nm, fmt=fmt,
                title=hebcal.heb_month_title(hm, hy), lx_emp=expenses.employee_id(), lx_items=expenses.items(),
                lx_total=expenses.total(), lx_from=lx_from, lx_months=months)


@bp.route("/bonus/<int:bid>/delete", methods=["POST"])
def delete_bonus(bid):
    with db.connect() as c:
        b = c.execute("SELECT * FROM bonuses WHERE id=?", (bid,)).fetchone()
        c.execute("DELETE FROM bonuses WHERE id=?", (bid,))
    return redirect(url_for("admin.employee_month", eid=b["employee_id"], hy=b["heb_year"], hm=b["heb_month"]))


# ---- employees -----------------------------------------------------------

EMP_FORM = """<form method="post">
<input name="emp_number" placeholder="מספר אברך" value="{{ e.emp_number if e else '' }}" required>
<input name="name" placeholder="שם" value="{{ e.name if e else '' }}" required>
<input name="id_number" placeholder="ת.ז" value="{{ (e.id_number or '') if e else '' }}">
<input name="phone" placeholder="טלפון (השיחות יוצאות אליו)" value="{{ (e.phone or '') if e else '' }}">
<input name="phone2" placeholder="טלפון נוסף" value="{{ (e.phone2 or '') if e else '' }}">
<select name="phone2_mode" title="שיחות לטלפון הנוסף">{% for k, v in {'none': 'בלי שיחות אליו', 'shuttle': 'להתקשר אליו: רק הסעה', 'all': 'להתקשר אליו: הסעה והודעות'}.items() %}
<option value="{{k}}" {% if e and e.phone2_mode==k %}selected{% endif %}>{{v}}</option>{% endfor %}</select>
<input name="pin" placeholder="סיסמה (ריק = 1480)" value="{{ e.pin if e else '' }}" pattern="[0-9]{4,8}" title="4 עד 8 ספרות. ריק = 1480">
{% if full %}<select name="role">{% for k, v in roles.items() %}<option value="{{k}}" {% if e and e.role==k %}selected{% endif %}>{{v}}</option>{% endfor %}</select>
<input name="shift" placeholder="משמרת" value="{{ (e.shift or '') if e else '' }}">
<select name="shuttle_group"><option value="">בלי הסעה</option>{% for g in (1,2) %}<option value="{{g}}" {% if e and e.shuttle_group==g %}selected{% endif %}>{{ 'מאור חיים (ר׳ אוריאל דרשן)' if g == 1 else 'דרום (ר׳ נפתלי שפירא)' }}</option>{% endfor %}</select>
<input name="hourly_rate" type="number" step="0.01" placeholder="תעריף מיוחד (לא חובה)" value="{{ e.hourly_rate if e and e.hourly_rate is not none else '' }}">{% endif %}
{% if full %}<label><input type="checkbox" name="in_night" {% if (not e and not form_day) or (e and e.in_night) %}checked{% endif %}> כולל חצות</label>
<label><input type="checkbox" name="in_day" {% if (not e and form_day) or (e and e.in_day) %}checked{% endif %}> כולל הוראה</label>
<label><input type="checkbox" name="is_driver" {% if e and e.is_driver %}checked{% endif %}> נהג הסעה</label>
<select name="moreshet_role" title="מורשת הנחל (שלוחה 00)"><option value="">לא במורשת הנחל</option>
{% for k, v in {'mashgiach': 'מורשת: משגיח', 'manager': 'מורשת: מנהל'}.items() %}<option value="{{k}}" {% if e and e.moreshet_role==k %}selected{% endif %}>{{v}}</option>{% endfor %}</select>
<input name="moreshet_key" placeholder="מקש במורשת (3-6, 8, 9)" title="הספרה שאחרים מקישים כדי להשאיר לו הודעה (7 = לכולם)" size="16" value="{{ (e.moreshet_key or '') if e else '' }}" pattern="[3-689]?">
<input name="moreshet_name" placeholder="השם שנאמר במורשת (לדוגמה: נפתלי)" size="24" value="{{ (e.moreshet_name or '') if e else '' }}">{% endif %}
<select name="language">{% for k, v in langs.items() %}<option value="{{k}}" {% if e and e.language==k %}selected{% endif %}>{{v}}</option>{% endfor %}</select>
{% if full %}<input name="shuttle_calls" type="number" min="1" max="5" placeholder="שיחות הסעה (1-5)" title="כמה פעמים להתקשר ביציאת ההסעה - האברך קובע בטלפון" value="{{ e.shuttle_calls if e and e.shuttle_calls else '' }}">{% endif %}
{% if e %}<label><input type="checkbox" name="active" {% if e.active %}checked{% endif %}> פעיל</label>{% endif %}
<button>{{ 'שמירה' if e else 'הוספת אברך' }}</button></form>"""


def emp_values(form):
    return (form["emp_number"].strip(), form["name"].strip(), form.get("phone", "").strip() or None,
            form.get("pin", "").strip() or DEFAULT_PIN, form["role"], form.get("shift", "").strip() or None,
            int(form["shuttle_group"]) if form.get("shuttle_group") else None,
            float(form["hourly_rate"]) if form.get("hourly_rate") else None,
            1 if form.get("is_driver") else 0,
            form.get("language") if form.get("language") in LANGS else "he",
            int(form["shuttle_calls"]) if form.get("shuttle_calls") else None,
            form.get("id_number", "").strip() or None,
            1 if form.get("in_night") else 0, 1 if form.get("in_day") else 0,
            form.get("phone2", "").strip() or None,
            form.get("phone2_mode") if form.get("phone2_mode") in ("none", "shuttle", "all") else "none",
            form.get("moreshet_role") if form.get("moreshet_role") in moreshet.ROLES else None,
            form.get("moreshet_key", "").strip() or None,
            form.get("moreshet_name", "").strip() or None)


def limit_values(vals, old=None):
    """The Kollel Horaah login cannot change the role, Chatzos, shuttle, driver or Moreshet fields."""
    if is_full():
        return vals
    v = list(vals)
    keep = {4: "role", 5: "shift", 6: "shuttle_group", 7: "hourly_rate", 8: "is_driver", 10: "shuttle_calls",
            12: "in_night", 15: "phone2_mode", 16: "moreshet_role", 17: "moreshet_key", 18: "moreshet_name"}
    for i, col in keep.items():
        v[i] = old[col] if old is not None else {4: "employee", 8: 0, 12: 0, 15: "none"}.get(i)
    v[13] = 1                                         # in Kollel Horaah
    return tuple(v)


def phone_taken(phone, eid=None):
    p = db.normalize_phone(phone or "")
    if not p:
        return False
    with db.connect() as c:
        return any(p in (db.normalize_phone(x["phone"]), db.normalize_phone(x["phone2"])) for x in
                   c.execute("SELECT phone, phone2 FROM employees WHERE id<>?", (eid or -1,)))


@bp.route("/employees", methods=["GET", "POST"])
def employees():
    error = None
    kollel = request.args.get("k") if request.args.get("k") in ("night", "day", "none") else "night"
    if not is_full():
        kollel = "day"
    if request.method == "POST":
        try:
            if phone_taken(request.form.get("phone")) or phone_taken(request.form.get("phone2")):
                raise ValueError("מספר הטלפון הזה כבר שייך לאברך אחר")
            with db.connect() as c:
                c.execute("INSERT INTO employees(emp_number,name,phone,pin,role,shift,shuttle_group,hourly_rate,is_driver,"
                          "language,shuttle_calls,id_number,in_night,in_day,phone2,phone2_mode,moreshet_role,moreshet_key,moreshet_name,joined_on) "
                          "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          limit_values(emp_values(request.form)) + (db.now().date().isoformat(),))
            return redirect(url_for("admin.employees", k="day" if request.form.get("in_day") and not request.form.get("in_night") else kollel))
        except Exception as ex:                       # duplicate number etc.
            error = str(ex)
    with db.connect() as c:
        allemps = c.execute("SELECT * FROM employees ORDER BY active DESC, name").fetchall()
    where = {"night": lambda x: x["in_night"], "day": lambda x: x["in_day"],
             "none": lambda x: not x["in_night"] and not x["in_day"]}[kollel]
    emps = [x for x in allemps if where(x)]
    orphans = sum(1 for x in allemps if not x["in_night"] and not x["in_day"])
    no_id = sum(1 for x in emps if x["active"] and not x["id_number"])
    body = """<h1>{{ {'night': 'אברכי כולל חצות', 'day': 'אברכי כולל הוראה', 'none': 'אברכים שלא בשום כולל'}[kollel] }}</h1>
<p>{% if full %}<a href="{{ url_for('admin.employees', k='night') }}">כולל חצות</a> · <a href="{{ url_for('admin.employees', k='day') }}">כולל הוראה</a>{% endif %}
{% if full and (orphans or kollel == 'none') %} · <a class="warn" href="{{ url_for('admin.employees', k='none') }}">לא בשום כולל ({{ orphans }})</a>{% endif %}
<span class="muted"> · {{ emps|length }} כאן.{% if full %} אברך ששייך לשני הכוללים מופיע בשני הדפים.{% endif %}</span></p>
{% if no_id %}<p class="warn">ל־{{ no_id }} אברכים פעילים כאן אין ת.ז.</p>{% endif %}
{% if error %}<p class="warn">{{ error }}</p>{% endif %}
<div class="box"><b>הוספת אברך</b>""" + EMP_FORM + """</div>
<div class="wrap"><table><tr><th>מס'</th><th>שם</th><th>ת.ז</th><th>טלפון</th><th>תפקיד</th><th>משמרת</th><th>הסעה</th><th>תעריף לשעה</th><th>שפה</th><th>מצב</th><th></th></tr>
{% for x in emps %}<tr class="{{ '' if x.active else 'muted' }}"><td>{{ x.emp_number }}</td><td>{{ x.name }}</td><td>{% if x.id_number %}{{ x.id_number }}{% else %}<span class="warn">חסר</span>{% endif %}</td><td>{{ x.phone or '' }}{% if x.phone2 %}<br><span class="muted">{{ x.phone2 }}</span>{% endif %}</td>
<td>{{ roles[x.role] }}{% if x.is_driver %} · נהג{% endif %}
<span class="muted">{{ 'חצות' if x.in_night else '' }}{{ ' + ' if x.in_night and x.in_day else '' }}{{ 'הוראה' if x.in_day else '' }}</span></td><td>{{ x.shift or '' }}</td><td>{{ {1: 'מאור חיים', 2: 'דרום'}.get(x.shuttle_group, '') }}</td>
<td>{% set r = rate(x) %}{% if r is none %}<span class="warn">לא נקבע</span>{% else %}{{ r }}{% endif %}</td>
<td>{{ langs[x.language] }}{% if x.shuttle_group %} · {{ x.shuttle_calls or '?' }} שיחות{% endif %}</td>
<td>{% if removed(x) %}<span class="warn">הוסר (חודשיים)</span>
<form method="post" action="{{ url_for('admin.restore', eid=x.id) }}" style="display:inline"><button>החזרה</button></form>{% endif %}</td>
<td><a href="{{ url_for('admin.edit_employee', eid=x.id) }}">עריכה</a></td></tr>{% endfor %}</table></div>"""
    today = schedule.session_date(db.now())
    return page(body, emps=emps, e=None, roles=ROLE_NAMES, rate=db.rate_for, error=error, langs=LANGS,
                kollel=kollel, orphans=orphans, no_id=no_id, form_day=kollel == "day",
                removed=lambda x: x["active"] and schedule.removed_by_rule(x, today))


@bp.route("/employees/<int:eid>/restore", methods=["POST"])
def restore(eid):
    with db.connect() as c:
        c.execute("UPDATE employees SET restored_on=? WHERE id=?", (db.now().date().isoformat(), eid))
    return redirect(url_for("admin.employees", k="night"))


@bp.route("/employees/<int:eid>", methods=["GET", "POST"])
def edit_employee(eid):
    error = None
    with db.connect() as c:
        old = c.execute("SELECT * FROM employees WHERE id=?", (eid,)).fetchone()
    horaah_only_emp(old)
    if request.method == "POST":
        if phone_taken(request.form.get("phone"), eid) or phone_taken(request.form.get("phone2"), eid):
            error = "מספר הטלפון הזה כבר שייך לאברך אחר"
        else:
            with db.connect() as c:
                c.execute("UPDATE employees SET emp_number=?,name=?,phone=?,pin=?,role=?,shift=?,shuttle_group=?,"
                          "hourly_rate=?, is_driver=?, language=?, shuttle_calls=?, id_number=?, in_night=?, in_day=?, phone2=?, phone2_mode=?, "
                          "moreshet_role=?, moreshet_key=?, moreshet_name=?, active=? WHERE id=?",
                          limit_values(emp_values(request.form), old) + (1 if request.form.get("active") else 0, eid))
            return redirect(url_for("admin.employees", k="day" if not is_full() else "night" if request.form.get("in_night") else
                                    "day" if request.form.get("in_day") else "none"))
    with db.connect() as c:
        e = c.execute("SELECT * FROM employees WHERE id=?", (eid,)).fetchone()
    return page("<h1>עריכת אברך</h1>{% if error %}<p class='warn'>{{ error }}</p>{% endif %}<div class='box'>"
                + EMP_FORM + "</div>", e=e, roles=ROLE_NAMES, langs=LANGS, error=error)


# ---- messages ------------------------------------------------------------

@bp.route("/messages")
def messages():
    since = (db.now() - timedelta(days=int(request.args.get("days", 30)))).isoformat()
    with db.connect() as c:
        msgs = c.execute("SELECT m.*, e.name sender FROM messages m JOIN employees e ON e.id=m.sender_id "
                         "WHERE m.created_at>=? AND m.channel='kollel' ORDER BY m.created_at DESC", (since,)).fetchall()
        rcpts = c.execute("SELECT r.*, e.name FROM message_recipients r JOIN employees e ON e.id=r.employee_id "
                          "JOIN messages m ON m.id=r.message_id WHERE m.created_at>=? AND m.channel='kollel'", (since,)).fetchall()
    by_msg = {}
    for r in rcpts:
        by_msg.setdefault(r["message_id"], []).append(r)
    body = """<h1>הודעות (30 הימים האחרונים)</h1>{% if request.args.get('msg') %}<p class="warn">{{ request.args.get('msg') }}</p>{% endif %}
<form method="post" action="{{ url_for('admin.forward_messages') }}" id="fwd">
<div class="box"><b>העברה לכולם</b> - מסמנים למטה את ההודעות (הן מחוברות לפי הסדר שבו הושארו, הראשונה ראשונה),
ואז: <select name="audience"><option value="night">כולל חצות</option><option value="day">כולל הוראה</option><option value="both">שני הכוללים</option></select>
<label><input type="checkbox" name="call" checked> להתקשר אליהם עכשיו, ושוב כל רבע שעה עד שיענו</label>
<br>לשלוח גם אליי: <select name="copy_to"><option value="">-</option>{% for e in people %}<option value="{{ e.id }}" {{ 'selected' if e.id|string == copy_to else '' }}>{{ e.name }}</option>{% endfor %}</select>
<button onclick="return confirm('לשלוח את ההודעות המסומנות לכל מי שנבחר?')">העברה</button>
<div class="muted">כשאברך מתקשר לקו, ההודעה מושמעת לו מיד אחרי הברכה, לפני התפריט.</div></div></form>
{% for m in msgs %}<div class="box"><label><input type="checkbox" name="mid" value="{{ m.id }}" form="fwd"> <b>{{ m.sender }}</b> ← {{ m.recipient_label }}</label>
<span class="muted"> · {{ m.heb_date }} · <span class="num">{{ fmt(m.created_at) }}</span></span>
{% if m.urgent %}<div class="warn">מושמעת בתחילת כל שיחה עד ששומעים אותה.
<form method="post" action="{{ url_for('admin.message_action', mid=m.id) }}" style="display:inline">
<button name="do" value="stop">להפסיק להשמיע בתחילת השיחה</button>
<button name="do" value="call" onclick="return confirm('להתקשר שוב לכל מי שעוד לא שמע?')">להתקשר שוב למי שלא שמע</button></form></div>{% endif %}
{% if m.call_result %}<div class="muted" dir="ltr" style="text-align:right">שיחות: {{ m.call_result }}</div>{% endif %}
<div><audio controls preload="none" src="{{ url_for('admin.recording', mid=m.id) }}"></audio></div>
<table><tr><th>נמען</th><th>שיחת עדכון</th><th>שמע</th></tr>{% for r in by_msg.get(m.id, []) %}
<tr><td>{{ r.name }}</td><td>{{ he(r.delivery_status) }}</td><td class="num">{{ fmt(r.listened_at) if r.listened_at else '—' }}</td></tr>{% endfor %}
</table></div>{% else %}<p class="muted">אין הודעות עדיין.</p>{% endfor %}"""
    with db.connect() as c:
        people = c.execute("SELECT id, name FROM employees WHERE active=1 ORDER BY role<>'tech_manager', role NOT LIKE '%manager', name").fetchall()
    return page(body, msgs=msgs, by_msg=by_msg, fmt=fmt, request=request, people=people,
                copy_to=db.get_setting("forward_copy_to") or "")


@bp.route("/messages/<int:mid>/action", methods=["POST"])
def message_action(mid):
    import threading
    import ivr
    if request.form.get("do") == "stop":
        with db.connect() as c:
            c.execute("UPDATE messages SET urgent=0 WHERE id=?", (mid,))
        return redirect(url_for("admin.messages", msg="ההודעה כבר לא מושמעת בתחילת השיחה (היא עדיין בהודעות, שלוחה 4)."))
    threading.Thread(target=ivr.notify_urgent, args=(mid,), daemon=True).start()
    return redirect(url_for("admin.messages", msg="מתקשרים שוב. רעננו את הדף בעוד דקה כדי לראות את התוצאה מתחת להודעה."))


@bp.route("/messages/forward", methods=["POST"])
def forward_messages():
    """Join the ticked messages (oldest first) into one urgent message to a whole kollel, and phone everyone."""
    import threading
    import ivr
    ids = [int(x) for x in request.form.getlist("mid")]
    if not ids:
        return redirect(url_for("admin.messages", msg="יש לסמן לפחות הודעה אחת"))
    with db.connect() as c:
        ms = c.execute(f"SELECT * FROM messages WHERE id IN ({','.join('?' * len(ids))}) ORDER BY created_at", ids).fetchall()
        everyone = c.execute("SELECT * FROM employees WHERE active=1").fetchall()
        who = request.form.get("audience", "night")
        rcpts = [e for e in everyone if (who in ("night", "both") and e["in_night"]) or (who in ("day", "both") and e["in_day"])]
        copy_to = request.form.get("copy_to", "")
        rcpts += [e for e in everyone if str(e["id"]) == copy_to and e not in rcpts]
        label = {"night": "כולל חצות", "day": "כולל הוראה", "both": "שני הכוללים"}[who] + " (הודעה מועברת)"
        t = db.now()
        mid = c.execute("INSERT INTO messages(sender_id, created_at, heb_date, recipient_type, recipient_label, recording_path, "
                        "channel, urgent) VALUES (?,?,?,?,?,?,'kollel',1)",
                        (ms[0]["sender_id"], t.isoformat(), hebcal.format_date(t.date(), "he"), who, label,
                         "|".join(m["recording_path"] for m in ms))).lastrowid
        c.executemany("INSERT INTO message_recipients(message_id, employee_id) VALUES (?,?)", [(mid, e["id"]) for e in rcpts])
    db.set_setting("forward_copy_to", copy_to)
    info = ""
    if request.form.get("call"):
        threading.Thread(target=ivr.notify_urgent, args=(mid,), daemon=True).start()
        info = " מתקשרים אליהם עכשיו. רעננו את הדף בעוד דקה כדי לראות את התוצאה מתחת להודעה."
    return redirect(url_for("admin.messages", msg=f"הועבר ל־{len(rcpts)} אברכים.{info}"))


@bp.route("/recording/<int:mid>")
def recording(mid):
    with db.connect() as c:
        m = c.execute("SELECT recording_path FROM messages WHERE id=?", (mid,)).fetchone()
    data = nbs.join_wavs([nbs.download_recording(p) for p in m["recording_path"].split("|") if p]) if m else None
    if not data:
        return Response("ההקלטה לא זמינה", 404)
    return Response(data, mimetype="audio/wav")


# ---- Moreshet HaNachal (key 00) ---------------------------------------------

@bp.route("/moreshet")
def moreshet_page():
    people = moreshet.members()
    rows = list(reversed(moreshet.reports()))
    with db.connect() as c:
        msgs = c.execute("SELECT m.*, e.name sender FROM messages m JOIN employees e ON e.id=m.sender_id "
                         "WHERE m.channel='moreshet' ORDER BY m.created_at DESC LIMIT 100").fetchall()
    body = """<h1>מורשת הנחל</h1>
<div class="box"><b>מי</b> (שלוחה 00 - רק הם שומעים אותה; מי שלא בשום כולל מגיע אליה ישר)
<table><tr><th>שם</th><th>תפקיד</th><th>מקש להודעה אליו</th><th>נאמר בתור</th><th>טלפון</th><th></th></tr>
{% for e in people %}<tr><td>{{ e.name }}</td><td>{{ roles[e.moreshet_role] }}</td><td>{{ e.moreshet_key or '' }}</td><td>{{ said(e) }}</td><td>{{ e.phone or '' }}</td>
<td><a href="{{ url_for('admin.edit_employee', eid=e.id) }}">עריכה</a></td></tr>{% else %}<tr><td colspan="6" class="warn">אין עדיין אף אחד</td></tr>{% endfor %}</table>
<p class="muted">כדי להוסיף מישהו: אברכים ← עריכה ← "מורשת: משגיח / מנהל" והמקש שלו. מי שלא באף כולל:
מוסיפים אותו בדף האברכים, מורידים את הסימון משני הכוללים, ובוחרים לו תפקיד במורשת.</p></div>
<div class="box"><b>כספי הפעילות</b> (שלוחה 8 של הגזבר במורשת; שיחת תזכורת מה־16 בשעה 14:00 עד שמאשרים)
<p style="font-size:1.2em">אצל {{ tname or '(לא נקבע גזבר)' }}: <b class="num">₪{{ '%.2f'|format(bal) }}</b></p>
{% for cy in pend %}<p class="warn">להעביר עבור {{ cy.cycle }}: נשארו ₪{{ '%.2f'|format(cy.left) }}
({% for x in cy.due %}{{ x.name }} ₪{{ '%.2f'|format(x.amount) }}{{ ', ' if not loop.last }}{% endfor %}){% if cy.paid %} · הועברו ₪{{ '%.2f'|format(cy.paid) }}{% endif %}
<form method="post" action="{{ url_for('admin.money_post') }}" style="display:inline"><input type="hidden" name="do" value="paid_all"><input type="hidden" name="cycle" value="{{ cy.cycle }}"><button>הכל הועבר</button></form></p>{% endfor %}
<form method="post" action="{{ url_for('admin.money_post') }}"><input type="hidden" name="do" value="add">
הוספה: <select name="kind"><option value="income">נכנס (+)</option><option value="expense">הוצאה (-)</option><option value="transfer">העברה למישהו (-)</option></select>
₪ <input name="amount" type="number" step="0.01" required style="width:90px"> תאריך <input name="day" type="date" value="{{ today }}">
עבור <input name="note" required style="width:260px"> <button>הוספה</button></form>
<table><tr><th>תאריך</th><th>מה</th><th class="right">סכום</th><th></th></tr>
{% for x in led %}<tr><td class="num">{{ x.day }}</td><td>{{ x.note or '' }}</td><td class="right num">{{ '%+.2f'|format(x.amount) }}</td>
<td><form method="post" action="{{ url_for('admin.money_post') }}" onsubmit="return confirm('למחוק את השורה?')"><input type="hidden" name="do" value="delete"><input type="hidden" name="id" value="{{ x.id }}"><button>הסרה</button></form></td></tr>
{% else %}<tr><td colspan="4" class="muted">אין עדיין.</td></tr>{% endfor %}</table>
<form method="post" action="{{ url_for('admin.money_post') }}"><input type="hidden" name="do" value="settings">
גזבר: <select name="treasurer"><option value="">-</option>{% for e in people %}<option value="{{ e.id }}" {{ 'selected' if e.id == tid else '' }}>{{ e.name }}</option>{% endfor %}</select>
נכנס כל חודש ₪ <input name="monthly" type="number" step="0.01" value="{{ monthly }}" style="width:90px"><br>
משכורות חודשיות: {% for e in people %}{% if e.id != tid %}{{ e.name }} ₪ <input name="sal_{{ e.id }}" type="number" step="0.01" value="{{ e.moreshet_salary or '' }}" style="width:80px"> {% endif %}{% endfor %}
<button>שמירה</button><div class="muted">ב־15 לחודש: משכורת + ההוצאות שלו − ההכנסות החריגות שלו (מה שדווח מה־15 בחודש הקודם עד ה־14).
הדיווחים של הגזבר עצמו בשלוחות 7 ו־9 (כולל הוראות קבע של ההורים שמדווחות ב־9) נכנסים ישר לחשבון הזה.</div></form></div>
<div class="box"><b>הוצאות והכנסות חריגות</b> (שלוחות 7 ו־9). נשלח במייל ב־15 לכל חודש אל <span dir="ltr">{{ email }}</span>.
<table><tr><th>דווח</th><th>מי</th><th>סוג</th><th>תאריך</th><th class="right">סכום</th><th>עבור מה</th></tr>
{% for x in rows %}<tr><td class="num">{{ fmt(x.created_at) }}</td><td>{{ x.name }}</td><td>{{ 'הוצאה' if x.kind == 'expense' else 'הכנסה' }}</td>
<td>{{ x.heb_date }}</td><td class="right">{{ '%.2f'|format(x.amount) }}</td>
<td>{% if x.recording_path %}<audio controls preload="none" src="{{ url_for('admin.moreshet_recording', rid=x.id) }}"></audio>{% endif %}</td></tr>
{% else %}<tr><td colspan="6" class="muted">אין דיווחים עדיין.</td></tr>{% endfor %}</table></div>
<div class="box"><b>הודעות</b><table><tr><th>מתי</th><th>מאת</th><th>אל</th><th></th></tr>
{% for m in msgs %}<tr><td class="num">{{ fmt(m.created_at) }}</td><td>{{ m.sender }}</td><td>{{ m.recipient_label }}</td>
<td><audio controls preload="none" src="{{ url_for('admin.recording', mid=m.id) }}"></audio></td></tr>
{% else %}<tr><td colspan="4" class="muted">אין הודעות.</td></tr>{% endfor %}</table></div>"""
    import money
    tid = money.treasurer_id()
    tname = next((e["name"] for e in people if e["id"] == tid), None)
    return page(body, people=people, rows=rows, msgs=msgs, fmt=fmt, roles=moreshet.ROLES, email=moreshet.EMAIL,
                said=moreshet.spoken_name, bal=money.balance(), pend=money.pending(), led=money.ledger(),
                tid=tid, tname=tname, monthly=money.monthly_in(), today=db.now().date().isoformat())


@bp.route("/moreshet/money", methods=["POST"])
def money_post():
    import money
    from datetime import date as _date
    f = request.form
    if f["do"] == "paid_all":
        money.record_transfer(f["cycle"])
    elif f["do"] == "add":
        money.add(f["kind"], float(f["amount"]), f["note"], _date.fromisoformat(f["day"]) if f.get("day") else None)
    elif f["do"] == "delete":
        with db.connect() as c:
            c.execute("DELETE FROM moreshet_ledger WHERE id=?", (int(f["id"]),))
    elif f["do"] == "settings":
        db.set_setting("money_treasurer", f.get("treasurer", ""))
        db.set_setting("money_monthly_in", f.get("monthly") or str(money.MONTHLY_IN))
        with db.connect() as c:
            for k, v in f.items():
                if k.startswith("sal_"):
                    c.execute("UPDATE employees SET moreshet_salary=? WHERE id=?", (float(v) if v else None, int(k[4:])))
    return redirect(url_for("admin.moreshet_page"))


@bp.route("/moreshet/recording/<int:rid>")
def moreshet_recording(rid):
    with db.connect() as c:
        m = c.execute("SELECT recording_path FROM moreshet_reports WHERE id=?", (rid,)).fetchone()
    data = nbs.download_recording(m["recording_path"]) if m and m["recording_path"] else None
    if not data:
        return Response("ההקלטה לא זמינה", 404)
    return Response(data, mimetype="audio/wav")


# ---- attendance corrections (extension 5) --------------------------------

from corrections import apply_correction, meron_rule  # noqa: E402  (shared with the phone)


def horaah_correction(c, k):
    e = c.execute("SELECT in_day FROM employees WHERE id=?", (k["employee_id"],)).fetchone()
    return bool(e and e["in_day"]) and program_of(db.parse(k["requested_time"])) == "day"


@bp.route("/corrections", methods=["GET", "POST"])
def corrections():
    msg = None
    if request.method == "POST":
        cid, action = int(request.form["id"]), request.form["action"]
        with db.connect() as c:
            corr = c.execute("SELECT * FROM corrections WHERE id=? AND status='pending'", (cid,)).fetchone()
            if corr and not is_full() and not horaah_correction(c, corr):
                abort(403)
            if corr and action == "approve":
                ok, msg = apply_correction(c, corr)
                if ok:
                    c.execute("UPDATE corrections SET status='approved', result=? WHERE id=?", (msg, cid))
            elif corr:
                c.execute("UPDATE corrections SET status='rejected' WHERE id=?", (cid,))
    with db.connect() as c:
        rows = c.execute("SELECT k.*, e.name, e.emp_number FROM corrections k JOIN employees e ON e.id=k.employee_id "
                         "ORDER BY k.status='pending' DESC, k.created_at DESC LIMIT 200").fetchall()
        if not is_full():
            rows = [k for k in rows if horaah_correction(c, k)]
    body = """<h1>תיקוני נוכחות</h1>{% if msg %}<p class="warn">{{ he(msg) }}</p>{% endif %}
<p class="muted">בקשות שאברכים ביקשו בטלפון (שלוחה 5). שום דבר לא משתנה עד שמאשרים.
אם נשכחו גם כניסה וגם יציאה באותו יום, מאשרים קודם את הכניסה.</p>
<div class="wrap"><table><tr><th>התבקש</th><th>אברך</th><th>תיקון</th><th>שעה</th><th>מצב</th><th></th></tr>
{% for k in rows %}<tr><td class="num">{{ fmt(k.created_at) }}</td><td>{{ k.name }} ({{ k.emp_number }})</td>
<td>{{ 'כניסה' if k.kind=='in' else 'יציאה' }}</td><td class="num">{{ fmt(k.requested_time) }}</td>
<td>{{ he(k.status) }}{% if k.result %} · <span class="muted">{{ he(k.result) }}</span>{% endif %}</td>
<td>{% if k.status=='pending' %}<form method="post" style="display:inline"><input type="hidden" name="id" value="{{k.id}}">
<button name="action" value="approve">אישור</button> <button name="action" value="reject" style="background:#777">דחייה</button>
</form>{% endif %}</td></tr>{% else %}<tr><td colspan="6" class="muted">אין בקשות.</td></tr>{% endfor %}</table></div>"""
    return page(body, rows=rows, fmt=fmt, msg=msg)


# ---- shuttle departures (key 3) ------------------------------------------

@bp.route("/shuttles", methods=["GET", "POST"])
def shuttles():
    msg = None
    if request.method == "POST":
        ok, info = nbs.set_redial(int(request.form.get("attempts", 3)), int(request.form.get("wait", 120)))
        msg = "הגדרות החיוג החוזר נשמרו." if ok else f"לא נשמר: {info}"
    with db.connect() as c:
        rows = c.execute("SELECT s.*, e.name FROM shuttle_runs s JOIN employees e ON e.id=s.driver_id "
                         "ORDER BY s.created_at DESC LIMIT 100").fetchall()
    body = """<h1>יציאות הסעה</h1>{% if msg %}<p class="warn">{{ msg }}</p>{% endif %}
<div class="wrap"><table><tr><th>שעה</th><th>נהג</th><th>קבוצה</th><th>התקשרו אל</th><th>תוצאה</th></tr>
{% for x in rows %}<tr><td class="num">{{ fmt(x.created_at) }}</td><td>{{ x.name }}</td><td>{{ {1: 'מאור חיים', 2: 'דרום'}.get(x.shuttle_group, x.shuttle_group) }}</td>
<td dir="ltr" style="text-align:right">{{ x.recipients }}</td><td>{{ he(x.status) }}</td></tr>{% else %}<tr><td colspan="5" class="muted">אין עדיין.</td></tr>{% endfor %}
</table></div>
<div class="box"><b>חיוג חוזר למי שלא ענה</b> (חל על הקמפיין שמוגדר ב־NBS_TEMPLATE_ID)
<form method="post">ניסיונות <input name="attempts" type="number" value="3" min="1" max="5">
המתנה בין ניסיונות <input name="wait" type="number" value="120" step="30" min="60"> שניות
<button>שמירה</button></form></div>"""
    return page(body, rows=rows, fmt=fmt, msg=msg)


# ---- import past attendance ---------------------------------------------

@bp.route("/import", methods=["GET", "POST"])
def import_page():
    msg, rows, token, warns, errors = None, None, None, [], []
    if request.method == "POST":
        token = request.form.get("token")
        files = [f for f in request.files.getlist("file") if f.filename]
        if files:
            token = secrets.token_hex(8)
            db.set_setting(f"import_{token}", json.dumps(
                [[f.filename, base64.b64encode(f.read()).decode()] for f in files]))
        stored = json.loads(db.get_setting(f"import_{token}", "") or "[]") if token else []
        parsed = []
        for fname, b64 in stored:
            got, err = importer.read_rows(base64.b64decode(b64))
            if err:
                errors.append(f"{fname}: {err}")
            for r in got:
                r["sheet"] = f"{fname} / {r['sheet']}" if r["sheet"] else fname
            parsed += got
        if not stored:
            msg = "לא נבחר קובץ"
        elif parsed:
            rows = importer.match(parsed)
            action = request.form.get("action")
            if action in ("import", "create_and_import"):
                made = importer.create_missing(rows) if action == "create_and_import" else 0
                rows = importer.match(rows)
                added = importer.commit(rows)
                with db.connect() as c:
                    c.execute("DELETE FROM settings WHERE key=?", (f"import_{token}",))
                msg = f"יובאו {added} רשומות." + (f" נוצרו {made} אברכים חדשים (סיסמה 1480, צריך להוסיף להם טלפון)." if made else "")
                rows, token = None, None
            else:
                warns = importer.warnings(rows)
    counts = {}
    for r in rows or []:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    body = """<h1>ייבוא נוכחות מהמערכת הישנה</h1>{% if msg %}<p class="warn">{{ msg }}</p>{% endif %}
{% for e in errors %}<p class="warn">לא הצלחתי לקרוא {{ e }}</p>{% endfor %}
<div class="box"><form method="post" enctype="multipart/form-data">
קבצים מהמערכת הישנה (CSV או אקסל, אפשר לבחור כמה ביחד): <input type="file" name="file" multiple required accept=".csv,.txt,.xlsx"> <button>בדיקה</button>
</form><p class="muted">עמודות: זהות, שם משפחה, שם פרטי, כניסה, יציאה. האברכים מזוהים לפי ת.ז, ואם אין - לפי שם.
רשומות בין 14:00 ל־20:00 שייכות לכולל הוראה, והשאר לכולל חצות.
שום דבר לא נשמר עד שלוחצים ייבוא. רשומות שכבר קיימות במערכת מדולגות.</p></div>
{% if rows %}
{% if warns %}<div class="box" style="border-color:#c62828"><b>כדאי לבדוק:</b><ul>{% for w in warns %}<li>{{ w }}</li>{% endfor %}</ul></div>{% endif %}
<div class="box">
<b>{{ counts.get('new', 0) }}</b> רשומות לייבוא · {{ counts.get('exists', 0) }} כבר קיימות ·
{{ counts.get('duplicate', 0) }} חוזרות בקבצים ·
<span class="{{ 'warn' if counts.get('no match') else '' }}">{{ counts.get('no match', 0) }} בלי אברך מתאים</span> ·
{{ counts.get('bad date', 0) }} לא קריאות
<form method="post" style="margin-top:10px"><input type="hidden" name="token" value="{{ token }}">
<button name="action" value="import">ייבוא הרשומות שזוהו</button>
{% if counts.get('no match') %}<button name="action" value="create_and_import" style="background:#2e7d32">ליצור את האברכים החסרים ולייבא הכל</button>{% endif %}
</form></div>
<div class="wrap"><table><tr><th>קובץ</th><th>ת.ז</th><th>השם בקובץ</th><th>שויך אל</th><th>כולל</th><th>כניסה</th><th>יציאה</th><th>מצב</th></tr>
{% for r in rows %}<tr><td class="muted">{{ r.sheet }}</td><td>{{ r.id }}</td><td>{{ r.name }}</td><td>{{ r.emp.name if r.emp else '' }}</td>
<td class="{{ 'warn' if r.emp and r.program and not r.emp['in_' + r.program] else '' }}">{{ he(r.program) }}</td>
<td class="num">{{ r['in'].strftime('%d/%m/%Y %H:%M') if r['in'] else '' }}</td><td class="num">{{ r.out.strftime('%d/%m/%Y %H:%M') if r.out else '' }}</td>
<td class="{{ 'warn' if r.status in ('no match', 'bad date') else 'muted' if r.status in ('exists', 'duplicate') else '' }}">{{ he(r.status) }}</td></tr>{% endfor %}
</table></div>{% endif %}"""
    return page(body, rows=rows, token=token, counts=counts, msg=msg, warns=warns, errors=errors)


# ---- Mivtza Tefila B'Koach (tefila.py) --------------------------------------

@bp.route("/tefila", methods=["GET", "POST"])
def tefila_page():
    winners = None
    if request.method == "POST" and request.form.get("action") == "draw":
        winners = tefila.draw()
    elif request.method == "POST" and request.form.get("action") == "add":
        day = request.form.get("day") or db.now().date().isoformat()
        added = 0
        for eid in request.form.getlist("employee_id"):
            added += tefila.add_manual(int(eid), datetime.fromisoformat(day).date(), -1)   # -1 = admin page
        return redirect(url_for("admin.tefila_page", day=day, msg=f"נוספו {added}"))
    elif request.method == "POST" and request.form.get("action") == "remove":
        with db.connect() as c:
            c.execute("DELETE FROM tefila_entries WHERE campaign=? AND employee_id=? AND day=?",
                      (tefila.CAMPAIGN["id"], int(request.form["employee_id"]), request.form["day"]))
        return redirect(url_for("admin.tefila_page", day=request.form["day"]))
    now = db.now()
    today = now.date()
    w = tefila.window(today)
    day = request.args.get("day") or today.isoformat()
    camp = tefila.CAMPAIGN
    missing = [camp["first"] + timedelta(days=i) for i in range((camp["last"] - camp["first"]).days + 1)]
    missing = [d for d in missing if d >= today and d.weekday() != 5 and tefila.netz(d) is None]
    body = """<h1>מבצע תפלה בכח</h1>
<div class="box">שלוחה {{ camp.key }} · <span class="num">{{ camp.first.strftime('%d/%m/%Y') }} – {{ camp.last.strftime('%d/%m/%Y') }}</span> (זאת חנוכה) · כולל חצות בלבד.
פתוח מ־{{ camp.opens_before }} עד {{ camp.closes_before }} דקות לפני הנץ (צפת, עיתים לבינה).<br>
היום: {% if w %}נץ {{ netz.strftime('%H:%M:%S') }} · פתוח {{ w[0].strftime('%H:%M') }}–{{ w[1].strftime('%H:%M') }}{% else %}<span class="warn">אין זמן נץ להיום - סגור</span>{% endif %}
{% if missing %}<br><span class="warn">חסרים זמני נץ ל־{{ missing|length }} ימים (הראשון: {{ missing[0].strftime('%d/%m/%Y') }}) - בימים האלה השלוחה סגורה.</span>{% endif %}</div>
{% if winners is not none %}<div class="box" style="border-color:#2e7d32"><b>תוצאות ההגרלה (נשמרו):</b><ol>{% for x in winners %}<li>{{ x.prize }}: <b>{{ x.name }}</b></li>{% endfor %}</ol></div>{% endif %}
<div class="box"><b>כניסות לכל אברך</b> (כל בוקר = כניסה אחת) · סה"כ <b>{{ totals|sum(attribute='entries') }}</b>
<div class="wrap"><table><tr><th>מס'</th><th>שם</th><th>טלפון</th><th class="right">כניסות</th><th>אחרונה</th></tr>
{% for x in totals %}<tr><td>{{ x.emp_number }}</td><td>{{ x.name }}</td><td>{{ x.phone or '' }}</td><td class="right"><b>{{ x.entries }}</b></td><td class="num">{{ x.last_day }}</td></tr>
{% else %}<tr><td colspan="5" class="muted">אין כניסות עדיין.</td></tr>{% endfor %}</table></div>
<form method="post" onsubmit="return confirm('להגריל עכשיו את הזוכים? כל הגרלה נשמרת.')" style="margin-top:10px">
<button name="action" value="draw" style="background:#2e7d32">הגרלת הזוכים</button>
<span class="muted"> {{ camp.prizes|length }} פרסים, לפי מספר הכניסות, כל אברך זוכה פעם אחת לכל היותר.</span></form></div>
<div class="box"><b>הוספת אברכים ידנית</b> <span class="muted">(למי שלא הספיק להירשם)</span>
<form method="post"><input type="hidden" name="action" value="add">
יום <input type="date" name="day" value="{{ day }}"> <select name="employee_id" multiple size="6" style="min-width:260px">
{% for e in avrechim %}<option value="{{ e.id }}">{{ e.name }} ({{ e.emp_number }})</option>{% endfor %}</select>
<button>הוספה</button> <span class="muted">Ctrl + לחיצה כדי לבחור כמה.</span></form>
{% if request.args.get('msg') %}<p class="warn">{{ request.args.get('msg') }}</p>{% endif %}</div>
<div class="box"><b>לפי יום</b>: {% for x in days %}<a href="?day={{ x.day }}" class="num">{{ x.day }}</a> ({{ x.n }}){{ ' · ' if not loop.last }}{% endfor %}
<p><b class="num">{{ day }}</b></p><table><tr><th>שם</th><th>נרשם בשעה</th><th></th></tr>
{% for x in list %}<tr><td>{{ x.name }}</td><td><span class="num">{{ fmt(x.created_at) }}</span>{% if x.added_by is not none %} <span class="muted">· נוסף ידנית</span>{% endif %}</td><td><form method="post" onsubmit="return confirm('להסיר את הכניסה?')">
<input type="hidden" name="action" value="remove"><input type="hidden" name="employee_id" value="{{ x.employee_id }}">
<input type="hidden" name="day" value="{{ x.day }}"><button>הסרה</button></form></td></tr>
{% else %}<tr><td colspan="3" class="muted">אף אחד.</td></tr>{% endfor %}</table></div>
{% if draws %}<div class="box"><b>הגרלות קודמות</b>{% for d in draws %}<p><span class="num">{{ fmt(d.created_at) }}</span> · {{ d.tickets }} כניסות:
{% for x in d.winners %}<span>{{ x.prize }}: <b>{{ x.name }}</b></span>{{ ' · ' if not loop.last }}{% endfor %}</p>{% endfor %}</div>{% endif %}"""
    with db.connect() as c:
        avrechim = c.execute("SELECT id, name, emp_number FROM employees WHERE active=1 AND in_night=1 ORDER BY name").fetchall()
    return page(body, camp=camp, w=w, netz=tefila.netz(today), missing=missing, winners=winners, avrechim=avrechim, request=request,
                totals=tefila.totals(), days=tefila.by_day(), day=day, list=tefila.entries_of_day(day),
                draws=tefila.draws(), fmt=fmt)


# ---- temporary registrations (trips.py) -----------------------------------

@bp.route("/registrations", methods=["GET", "POST"])
def registrations():
    if request.method == "POST":
        if request.form.get("add"):
            t = trips.by_id(request.form["trip"])
            choice = request.form.get("choice") or (next(iter(t["options"])) if t else None)
            if t and choice in t["options"]:
                for eid in request.form.getlist("employee_id"):
                    trips.register(t["id"], int(eid), choice)
            return redirect(url_for("admin.registrations"))
        with db.connect() as c:
            c.execute("DELETE FROM trip_signups WHERE trip=? AND employee_id=?",
                      (request.form["trip"], int(request.form["employee_id"])))
        return redirect(url_for("admin.registrations"))
    with db.connect() as c:
        avrechim = c.execute("SELECT id, name, emp_number FROM employees WHERE active=1 ORDER BY name").fetchall()
    now = db.now()
    data = []
    for t in reversed(trips.TRIPS):
        rows = trips.signups(t["id"])
        counts = {k: sum(1 for x in rows if x["choice"] == k) for k in t["options"]}
        data.append((t, rows, counts, now < t["until"]))
    body = """<h1>הרשמות</h1>
{% for t, rows, counts, is_open in data %}<div class="box"><b>{{ t.title }}</b><br>
<span class="{{ '' if is_open else 'muted' }}">שלוחה {{ t.key }} · {{ 'פתוח עד' if is_open else 'נסגר ב' }} <span class="num">{{ t.until.strftime('%d/%m/%Y %H:%M') }}</span></span>
<p><b>{{ rows|length }}</b> נרשמו{% for k, o in t.options.items() %} · {{ o.he }}: <b>{{ counts[k] }}</b>{% endfor %}
 · <a href="{{ url_for('admin.registrations_csv', trip=t.id) }}">הורדה לאקסל (CSV)</a></p>
<div class="wrap"><table><tr><th>מס'</th><th>שם</th><th>טלפון</th><th>בחירה</th><th>נרשם</th><th></th></tr>
{% for x in rows %}<tr><td>{{ x.emp_number }}</td><td>{{ x.name }}</td><td>{{ x.phone or '' }}</td>
<td>{{ t.options[x.choice].he }}</td><td class="num">{{ fmt(x.created_at) }}</td>
<td><form method="post" onsubmit="return confirm('להסיר את ההרשמה?')"><input type="hidden" name="trip" value="{{ t.id }}">
<input type="hidden" name="employee_id" value="{{ x.employee_id }}"><button>הסרה</button></form></td></tr>{% endfor %}
</table></div>
<form method="post" style="margin-top:8px"><input type="hidden" name="add" value="1"><input type="hidden" name="trip" value="{{ t.id }}">
הוספה ידנית: <select name="employee_id" multiple size="4" style="min-width:240px">{% for e in avrechim %}<option value="{{ e.id }}">{{ e.name }} ({{ e.emp_number }})</option>{% endfor %}</select>
{% if t.options|length > 1 %}<select name="choice">{% for k, o in t.options.items() %}<option value="{{ k }}">{{ o.he }}</option>{% endfor %}</select>{% endif %}
<button>רישום</button> <span class="muted">Ctrl + לחיצה כדי לבחור כמה.</span></form></div>{% else %}<p class="muted">אין הרשמות.</p>{% endfor %}"""
    return page(body, data=data, fmt=fmt, avrechim=avrechim)


@bp.route("/registrations/<trip>.csv")
def registrations_csv(trip):
    t = trips.by_id(trip)
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["מס'", "שם", "טלפון", "בחירה", "נרשם"])
    for x in trips.signups(trip):
        w.writerow([x["emp_number"], x["name"], x["phone"] or "", t["options"][x["choice"]]["he"], fmt(x["created_at"])])
    return Response("\ufeff" + out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={trip}.csv"})


# ---- Kollel Horaah ------------------------------------------------------------

def day_rows(hy, hm, until=None):
    with db.connect() as c:
        emps = c.execute("SELECT * FROM employees ORDER BY name").fetchall()
    out = []
    for e in emps:
        if not schedule.in_day(e):
            continue
        out.append(dict(emp=e, **schedule.day_month_summary(e, hy, hm, until)))
    return out


@bp.route("/day")
def day_payroll():
    hy, hm = month_args(night=False)
    rows = day_rows(hy, hm)
    first, last = hebcal.month_range(hy, hm)
    py, pm = hebcal.prev_month(hy, hm)
    ny, nm = hebcal.next_month(hy, hm)
    days = schedule.day_dates(hy, hm)
    total = round(sum(r["total"] for r in rows), 2)
    body = """<h1>כולל הוראה · {{ title }}</h1>
<p class="muted"><span class="num">{{ first.strftime('%d/%m/%Y') }} – {{ last.strftime('%d/%m/%Y') }}</span> · {{ days|length }} ימי כולל (א'–ה', <span class="num">16:00–19:00</span>, כניסה מ־15:00) ·
<a href="?hy={{py}}&hm={{pm}}">▶ חודש קודם</a> · <a href="?hy={{ny}}&hm={{nm}}">חודש הבא ◀</a> ·
<br>חודשים עם נתונים: {% for y, m, name in data_months(program) %}<a href="?hy={{y}}&hm={{m}}" {% if y == hy and m == hm %}style="font-weight:700;text-decoration:none"{% endif %}>{{ name }}</a>{{ ' · ' if not loop.last }}{% endfor %} ·
<form method="post" action="{{ url_for('admin.send_day_report', hy=hy, hm=hm) }}" style="display:inline">
<button>לשלוח את הסיכום במייל עכשיו</button></form>{% if msg %} <span class="warn">{{ msg }}</span>{% endif %}</p>
<div class="box"><b>סה"כ: {{ '{:,.0f}'.format(total) }}</b> · הכל משולם ע"י אור הצדיק</div>
{% if full %}<div class="box"><b>כניסה לאתר למנהל כולל הוראה</b> <span class="muted">(רואה רק את כולל הוראה: הדף הזה, אברכי הוראה, תיקונים של כולל הוראה, והוראות)</span>
<form method="post" action="{{ url_for('admin.horaah_login_set') }}">שם משתמש <input name="user" value="{{ h_user }}" required dir="ltr">
סיסמה חדשה <input name="password" type="text" dir="ltr" placeholder="{{ 'לא לשנות' if h_user else '' }}" {{ '' if h_user else 'required' }}>
<button>שמירה</button>{% if h_user %} <button name="off" value="1" style="background:#777" onclick="return confirm('לבטל את הכניסה של מנהל כולל הוראה?')">ביטול הכניסה</button>{% endif %}</form></div>{% endif %}
<div class="wrap"><table><tr><th>שם</th><th class="right">ימים</th><th class="right">בזמן</th><th class="right">שעות</th>
<th class="right">נוכחות</th><th class="right">תשלום שעות</th><th class="right">תשלום הגעה בזמן</th><th class="right">סה"כ ₪</th></tr>
{% for r in rows %}<tr><td><a href="{{ url_for('admin.employee_month', eid=r.emp.id, hy=hy, hm=hm) }}">{{ r.emp.name }}</a></td>
{% if r.vacation %}<td colspan="6" class="muted">אין כולל הוראה החודש – ממוצע 3 החודשים האחרונים</td>
{% else %}<td class="right">{{ r.days }} / {{ r.held }}</td><td class="right">{{ r.kept }}</td><td class="right">{{ r.hours }}</td>
<td class="right">{{ (r.percent * 100)|round(1) }}%</td><td class="right">{{ '%.2f'|format(r.base) }}</td>
<td class="right">{{ '%.2f'|format(r.keep) }}{% if r.average %} <span class="muted">+ {{ '%.2f'|format(r.average) }} ממוצע</span>{% endif %}</td>{% endif %}<td class="right"><b>{{ '%.2f'|format(r.total) }}</b></td></tr>
{% else %}<tr><td colspan="8" class="muted">עדיין אין אברכים שמסומנים "כולל הוראה" (אברכים ← עריכה).</td></tr>{% endfor %}
</table></div>
<p class="muted">800 ש"ח לחודש מחולקים בשעות הכולל של החודש, לפי השעות שנכח. 200 ש"ח לחודש מחולקים בימי הכולל של החודש,
לכל יום שהגיע עד 16:00. ניסן ותשרי: ממוצע 3 החודשים האחרונים. אב: א'–ז' לפי נוכחות, ושאר החודש לפי הממוצע.
אין כולל בחנוכה, פורים, שושן פורים, צומות, ערב שבועות, אסרו חג שבועות, ל"ג בעומר, ומח' אב עד א' אלול.</p>"""
    return page(body, program="day", rows=rows, hy=hy, hm=hm, py=py, pm=pm, ny=ny, nm=nm, first=first, last=last, days=days,
                total=total, title=hebcal.heb_month_title(hm, hy), msg=request.args.get("msg"),
                h_user=db.get_setting("horaah_user") or "")


@bp.route("/day/login", methods=["POST"])
def horaah_login_set():
    """Only the main admin gets here (the Horaah login is sent back to its own pages)."""
    if request.form.get("off"):
        db.set_setting("horaah_user", "")
        db.set_setting("horaah_pw", "")
        return redirect(url_for("admin.day_payroll", msg="הכניסה של מנהל כולל הוראה בוטלה"))
    user = request.form.get("user", "").strip()
    if not user or user == ADMIN_USER:
        return redirect(url_for("admin.day_payroll", msg="שם משתמש לא תקין"))
    db.set_setting("horaah_user", user)
    if request.form.get("password"):
        db.set_setting("horaah_pw", generate_password_hash(request.form["password"]))
    return redirect(url_for("admin.day_payroll", msg="נשמר. מנהל כולל הוראה נכנס עם השם והסיסמה האלה"))


@bp.route("/guide/horaah")
def horaah_guide():
    """Instructions for the Kollel Horaah manager: the phone and this site. Printable."""
    return page(HORAAH_GUIDE)


HORAAH_GUIDE = """<style>@media print{nav,.noprint{display:none}body{background:#fff}.box{border:0;padding:0}}
.guide h2{font-size:18px;color:#1f3a5f;margin:22px 0 6px}.guide li{margin:4px 0}.guide .k{display:inline-block;min-width:22px;text-align:center;
background:#1f3a5f;color:#fff;border-radius:4px;padding:0 5px;font-weight:700}</style>
<div class="guide"><h1>הוראות למנהל כולל הוראה</h1>
<p class="noprint"><button onclick="window.print()">הדפסה</button></p>
<div class="box"><h2>בטלפון</h2>
<p>מתקשרים לקו הנוכחות מהטלפון שרשום במערכת. אם מתקשרים מטלפון אחר, מקישים מספר אברך וסיסמה.
בכל תפריט: <span class="k">0</span> חוזר לתפריט הראשי. במקום שמקלידים מספר: <span class="k">*</span> חוזר לתפריט הראשי.</p>
<ul>
<li><span class="k">1</span> <b>עדכון כניסה</b> לכולל (אפשר מ־15:00). <span class="k">2</span> יציאה חריגה, אם יוצאים לפני הסוף.</li>
<li><span class="k">4</span> <b>הודעות</b>:
 <span class="k">1</span> לשמוע הודעות שקיבלת · <span class="k">2</span> לשלוח הודעה · <span class="k">3</span> לשמוע את ההודעות ששלחת ומי שמע אותן.<br>
 בשליחת הודעה: <span class="k">2</span> לכל כולל הוראה · <span class="k">6</span> לאברך אחד (אומרים את שמו, או מקישים מספר אברך או ת.ז) ·
 <span class="k">7</span> לכמה אברכים · <span class="k">8</span> למי שנוכח עכשיו. אחרי ההקלטה אפשר לשמוע אותה, לאשר או להקליט מחדש.
 ההודעה מגיעה גם אליך, כדי שתדע שהיא יצאה.</li>
<li><span class="k">5</span> <b>תיקוני נוכחות</b>: <span class="k">1</span> תיקון שלך · <span class="k">2</span> אישור הבקשות של האברכים.
 לכל בקשה: <span class="k">1</span> אישור · <span class="k">2</span> דחייה · <span class="k">3</span> לבקשה הבאה.
 אם אברך שכח גם כניסה וגם יציאה באותו יום, מאשרים קודם את הכניסה.</li>
<li><span class="k">6</span> <b>מצב</b>: <span class="k">1</span> המצב שלך · <span class="k">2</span> מצב של אברך אחר (אומרים את שמו) ·
 <span class="k">3</span> מי נוכח עכשיו. שם: <span class="k">1</span> מי לא נוכח · <span class="k">2</span> הודעה למי שנוכח.</li>
<li><span class="k">9</span> <b>הגדרות וכלים</b>:
 <span class="k">2</span> שינוי סיסמה · <span class="k">3</span> שפה ·
 <span class="k">6</span> רישום אברך חדש (טלפון, ת.ז, כולל - <span class="k">2</span> לכולל הוראה, ושם בדיבור; מספר האברך נקבע לבד ומתחיל ב־1, הסיסמה הראשונה 1480) ·
 <span class="k">7</span> עריכת פרטי אברך · <span class="k">8</span> בונוס נפרד לאברך ·
 <span class="k">9</span> פעולה בדיבור: אומרים מה לעשות, למשל "מי נמצא בכולל", "מה המצב של פלוני", "תאשר את התיקון של פלוני". שום דבר לא משתנה בלי הקשה 1 לאישור.</li>
</ul>
<p class="muted">אתה רואה ושולח רק לאברכי כולל הוראה.</p></div>
<div class="box"><h2>באתר</h2>
<p>נכנסים לכתובת האתר עם שם המשתמש והסיסמה שקיבלת. רואים רק את כולל הוראה.</p>
<ul>
<li><b>כולל הוראה</b>: סיכום החודש לכל אברך - ימים, הגעה בזמן, שעות, אחוז נוכחות והסכום. מעבר בין חודשים: "חודש קודם" / "חודש הבא".
 לוחצים על שם אברך כדי לראות את כל הכניסות והיציאות שלו בחודש.</li>
<li><b>בדף של אברך</b>: "עריכה" ליד כניסה - לתקן שעה או למחוק. "הוספת משמרת ידנית" - כניסה ויציאה שנשכחו (רק בשעות כולל הוראה).</li>
<li><b>אברכי הוראה</b>: רשימת האברכים. "הוספת אברך" למעלה - מספר אברך, שם, ת.ז, טלפון, סיסמה (ריק = 1480). "עריכה" ליד אברך - לתקן פרטים, או להוריד את הסימון "פעיל" כשהוא עוזב.</li>
<li><b>תיקונים</b>: בקשות התיקון שאברכי כולל הוראה ביקשו בטלפון (שלוחה 5). "אישור" או "דחייה". שום דבר לא משתנה עד שמאשרים.</li>
</ul></div>
<div class="box"><h2>כללי כולל הוראה</h2>
<ul><li>ימים א'–ה', <span class="num">16:00–19:00</span>. אפשר להיכנס מ־15:00.</li>
<li>800 ש"ח לחודש לפי השעות שנכח, ו־200 ש"ח לחודש לפי הימים שהגיע עד 16:00.</li>
<li>ניסן ותשרי: ממוצע 3 החודשים האחרונים. אב: א'–ז' לפי נוכחות, ושאר החודש לפי הממוצע.</li>
<li>אין כולל בחנוכה, פורים, שושן פורים, צומות, ערב שבועות, אסרו חג שבועות, ל"ג בעומר, ומח' אב עד א' אלול.</li></ul></div></div>"""


@bp.route("/day/send-report", methods=["POST"])
def send_day_report():
    hy, hm = month_args(night=False)
    ok, info = report.send_day(hy, hm)
    msg = "הסיכום נשלח במייל." if ok else f"לא נשלח: {info}"
    return redirect(url_for("admin.day_payroll", hy=hy, hm=hm, msg=msg))
