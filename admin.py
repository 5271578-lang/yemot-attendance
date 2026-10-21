"""Web admin: employees, attendance, payroll (by Hebrew month), message log."""
import csv
import io
import os
from datetime import datetime, timedelta
from functools import wraps

from flask import Blueprint, Response, redirect, render_template_string, request, url_for

import db
import hebcal
import nbs
import schedule
import report
import importer
import tefila
import trips
import base64
import json
import secrets

bp = Blueprint("admin", __name__, url_prefix="/admin")
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
ROLE_NAMES = {"employee": "Employee", "onsite_manager": "On-Site Manager", "tech_manager": "Technical Manager"}
LANGS = {"he": "Hebrew", "en": "English", "yi": "Yiddish"}
DEFAULT_PIN = "1480"            # initial password for every new avrech; he changes it with 9 -> 2


@bp.before_request
def auth():
    a = request.authorization
    if not ADMIN_PASSWORD or not a or a.username != ADMIN_USER or a.password != ADMIN_PASSWORD:
        return Response("Login required" if ADMIN_PASSWORD else "Set ADMIN_PASSWORD to enable the admin panel",
                        401, {"WWW-Authenticate": 'Basic realm="Attendance"'})


BASE = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Attendance Line</title><style>
body{font-family:system-ui,Arial,sans-serif;margin:0;background:#f5f5f2;color:#222}
nav{background:#1f3a5f;padding:12px 16px}nav a{color:#fff;margin-right:18px;text-decoration:none;font-weight:600}
main{padding:16px;max-width:1100px;margin:auto}table{border-collapse:collapse;width:100%;background:#fff}
th,td{border-bottom:1px solid #e3e3e3;padding:7px 9px;text-align:left;font-size:14px}th{background:#eef1f5}
.warn{color:#b00020;font-weight:600}.box{background:#fff;padding:14px;margin:14px 0;border:1px solid #ddd}
input,select{padding:5px;margin:3px 6px 3px 0}button{padding:6px 12px;background:#1f3a5f;color:#fff;border:0;cursor:pointer}
.muted{color:#777}.right{text-align:right}h1{font-size:22px}.wrap{overflow-x:auto}
</style></head><body><nav><a href="{{ url_for('admin.payroll') }}">Kollel Chatzos</a><a href="{{ url_for('admin.day_payroll') }}">Kollel Horaah</a>
<a href="{{ url_for('admin.employees', k='night') }}">Chatzos avrechim</a><a href="{{ url_for('admin.employees', k='day') }}">Horaah avrechim</a><a href="{{ url_for('admin.corrections') }}">Corrections</a>
<a href="{{ url_for('admin.messages') }}">Messages</a><a href="{{ url_for('admin.shuttles') }}">Shuttles</a>
<a href="{{ url_for('admin.import_page') }}">Import</a><a href="{{ url_for('admin.registrations') }}">Registrations</a><a href="{{ url_for('admin.tefila_page') }}">Tefila B'Koach</a></nav>
<main>{% block body %}{% endblock %}</main></body></html>"""


def page(body, **ctx):
    ctx.setdefault("data_months", data_months)
    ctx.setdefault("program", "night")
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
    return [(y, m, f"{hebcal.month_name(m, y)} {y}") for y, m in got]


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
<h1>{{ title }}</h1>
<p class="muted">{{ first.strftime('%d/%m/%Y') }} – {{ last.strftime('%d/%m/%Y') }} ·
<a href="?hy={{py}}&hm={{pm}}">◀ previous month</a> · <a href="?hy={{ny}}&hm={{nm}}">next month ▶</a> ·
<br>Months with records: {% for y, m, name in data_months(program) %}<a href="?hy={{y}}&hm={{m}}" {% if y == hy and m == hm %}style="font-weight:700;text-decoration:none"{% endif %}>{{ name }}</a>{{ ' · ' if not loop.last }}{% endfor %} ·
<a href="{{ url_for('admin.export', hy=hy, hm=hm) }}">Download CSV</a> ·
<form method="post" action="{{ url_for('admin.send_report', hy=hy, hm=hm) }}" style="display:inline">
<button>E-mail this summary now</button></form>{% if msg %} <span class="warn">{{ msg }}</span>{% endif %}</p>
<div class="wrap"><table><tr><th>No.</th><th>Name</th><th class="right">Nights</th><th class="right">Whole</th>
<th class="right">Attendance</th><th class="right">Nightly</th><th class="right">70/80%</th><th class="right">Extras</th>
<th class="right">Stipend</th><th class="right">Meron</th><th class="right">Driver</th><th class="right">Bonus</th><th class="right">Total ₪</th></tr>
{% for r in rows %}<tr><td>{{ r.emp.emp_number }}</td>
<td><a href="{{ url_for('admin.employee_month', eid=r.emp.id, hy=hy, hm=hm) }}">{{ r.emp.name }}</a>
<span class="muted">{{ roles[r.emp.role] if r.emp.role != 'employee' else '' }}</span></td>
<td class="right">{{ r.nights }}</td><td class="right">{{ r.whole }}</td><td class="right">{{ (r.percent * 100)|round(1) }}%</td>
<td class="right">{{ '%.2f'|format(r.base + r.timing) }}</td><td class="right">{{ r.percent_bonus }}</td>
<td class="right">{{ '%.2f'|format(r.extras) }}</td>
<td class="right">{% if r.eligible %}{{ '%.2f'|format(r.stipend) }}{% else %}<span class="warn" title="below the minimum">0</span>
<span class="muted">({{ '%.0f'|format(r.stipend) }})</span>{% endif %}</td>
<td class="right">{{ r.meron }}</td><td class="right">{{ r.driver }}</td><td class="right">{{ r.gifts or '' }}</td>
<td class="right"><b>{% if r.total is none %}<span class="warn">rate not set</span>{% else %}{{ '%.2f'|format(r.total) }}{% endif %}</b></td>
</tr>{% endfor %}
</table></div>
<div class="box"><b>Totals:</b> kollel {{ '{:,.0f}'.format(t.kollel) }} · Meron {{ '{:,.0f}'.format(t.meron) }} ·
{% if t.gifts %}separate bonuses {{ '{:,.0f}'.format(t.gifts) }} · {% endif %}
all together <b>{{ '{:,.0f}'.format(t.total) }}</b> · to Kollel Chatzos Jerusalem (Tzfas) <b>{{ '{:,.0f}'.format(t.jerusalem) }}</b> ·
Ohr Hatzadik <b>{{ '{:,.0f}'.format(t.ohr_hatzadik) }}</b>
<div class="muted">Meron (35 a night, 60 for driving) → Jerusalem. 70/80% bonuses and separate bonuses → Ohr Hatzadik. The rest: half each.</div></div>
<p class="muted">The night month runs from the 27th of the month before to the 26th. Nightly: 80 × share of the kollel hours present (2:30–7:30, bein hazmanim 3:30–7:30), +20 up to 10 minutes late,
+30 before the start. A night without an exit counts until 7:30. Attendance % is by time only.
70% → +200, 80% → +100 more. Below 2 whole nights or below 500 (without Meron) → no stipend (red 0).
Meron: 35 a night. Driver: 60 a night. Extras = Uman nights and stipend additions added by hand (part of the stipend).
Bonus = a separate bonus, not part of the stipend: always paid, not counted for the minimum or the percents.</p>"""
    title = f"{hebcal.month_name(hm, hy)} {hy}"
    return page(body, rows=rows, hy=hy, hm=hm, py=py, pm=pm, ny=ny, nm=nm, first=first, last=last,
                title=title, roles=ROLE_NAMES, msg=request.args.get("msg"),
                t=report.totals([(r["emp"], r) for r in rows]))


@bp.route("/send-report", methods=["POST"])
def send_report():
    hy, hm = month_args()
    ok, info = report.send(hy, hm)
    msg = "The summary was e-mailed." if ok else f"Could not send: {info}"
    return redirect(url_for("admin.payroll", hy=hy, hm=hm, msg=msg))


@bp.route("/export.csv")
def export():
    hy, hm = month_args()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Employee no.", "Name", "Role", "Nights", "Whole nights", "Attendance %", "Nightly", "70/80% bonus",
                "Extras", "Stipend", "Eligible", "Meron nights", "Meron", "Drives", "Driver", "Total"])
    for r in payroll_rows(hy, hm):
        w.writerow([r["emp"]["emp_number"], r["emp"]["name"], ROLE_NAMES[r["emp"]["role"]], r["nights"], r["whole"],
                    round(r["percent"] * 100, 1), round(r["base"] + r["timing"], 2), r["percent_bonus"], r["extras"],
                    r["stipend"], "yes" if r["eligible"] else "no", r["meron_nights"], r["meron"], r["drives"],
                    r["driver"], r["total"] if r["total"] is not None else ""])
    name = f"payroll-{hebcal.month_name(hm, hy).replace(' ', '')}-{hy}.csv"
    return Response("﻿" + buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={name}"})


@bp.route("/employee/<int:eid>")
def employee_month(eid):
    hy, hm = month_args()
    with db.connect() as c:
        e = c.execute("SELECT * FROM employees WHERE id=?", (eid,)).fetchone()
        nfirst, nlast = schedule.night_month_range(hy, hm)
        recs = [x for x in c.execute("SELECT * FROM attendance WHERE employee_id=? ORDER BY clock_in", (eid,)).fetchall()
                if (x["program"] == "night" and nfirst <= schedule.session_date(db.parse(x["clock_in"])) <= nlast)
                or (x["program"] == "day" and (x["heb_year"], x["heb_month"]) == (hy, hm))]
        uman = c.execute("SELECT * FROM uman WHERE employee_id=? ORDER BY heb_year DESC", (eid,)).fetchall()
        bons = c.execute("SELECT * FROM bonuses WHERE employee_id=? AND heb_year=? AND heb_month=?",
                         (eid, hy, hm)).fetchall()
        gifts = c.execute("SELECT * FROM gifts WHERE employee_id=? AND heb_year=? AND heb_month=? ORDER BY created_at",
                          (eid, hy, hm)).fetchall()
    body = """
<h1>{{ e.name }} · {{ title }}</h1><p><a href="{{ url_for('admin.payroll', hy=hy, hm=hm) }}">← back</a> ·
<a href="?hy={{py[0]}}&hm={{py[1]}}">◀ previous month</a> · <a href="?hy={{ny[0]}}&hm={{ny[1]}}">next month ▶</a>
<span class="muted"> · night month {{ heb(nfirst) }} – {{ heb(nlast) }}{% if e.in_day %}; Kollel Horaah: the calendar month{% endif %}</span></p>
<form method="post" action="{{ url_for('admin.set_meron', eid=e.id, hy=hy, hm=hm) }}">
<div class="wrap"><table><tr><th>Hebrew date</th><th>Clock in</th><th>Exit</th><th class="right">Minutes</th>
<th class="right">Base</th><th class="right">On-time</th><th class="right">Night ₪</th><th>Meron</th>{% if e.is_driver %}<th>Drove</th>{% endif %}<th>Note</th><th></th></tr>
{% for a in recs %}{% set n = pay.get(a.id) %}<tr><td>{{ a.heb_day }} {{ title }}{% if n and n.bhz %} · <span class="muted">bein hazmanim</span>{% endif %}</td>
<td>{{ fmt(a.clock_in) }}{% if a.program == 'day' %} <span class="muted">· Kollel Horaah</span>{% endif %}{% if a.meron %} <span class="muted">· Meron</span>{% endif %}
{% if a.actual_in and a.actual_in != a.clock_in %}<br><span class="muted">called {{ fmt(a.actual_in) }}</span>{% endif %}</td>
<td>{% if a.clock_out %}{{ fmt(a.clock_out) }}{% if a.early_exit %} <span class="muted">early</span>{% endif %}{% else %}<span class="muted">7:30 (default)</span>{% endif %}
{% if not a.exit_confirmed %} <span class="muted">· not confirmed</span>{% endif %}</td>
<td class="right">{{ n.minutes if n else '' }}</td><td class="right">{{ n.base if n else '' }}</td>
<td class="right">{{ n.bonus if n else '' }}</td><td class="right"><b>{{ n.total if n else '' }}</b></td>
<td>{% if n %}<input type="checkbox" name="meron" value="{{ a.id }}" {% if n.meron %}checked{% endif %}>{% endif %}</td>
{% if e.is_driver %}<td>{% if n %}<input type="checkbox" name="drove" value="{{ a.id }}" {% if session_of(a) in drove %}checked{% endif %}>{% endif %}</td>{% endif %}
<td>{{ a.note or '' }}</td>
<td><a href="{{ url_for('admin.edit_attendance', aid=a.id) }}">edit</a></td></tr>{% endfor %}</table></div>
{% if pay %}<button style="margin-top:8px">Save Meron{{ ' / driving' if e.is_driver else '' }}</button>
<span class="muted"> Tick the nights he went to Meron{{ ' / drove the shuttle' if e.is_driver else '' }} and press Save.</span>{% endif %}</form>
<div class="box"><b>Month:</b> {{ sm.nights }} nights · {{ sm.whole }} whole · {{ sm.early }} early ·
attendance {{ (sm.percent * 100)|round(1) }}% · stipend {{ '%.2f'|format(sm.stipend) }}{% if not sm.eligible %} <span class="warn">(below minimum → 0)</span>{% endif %}
· Meron {{ sm.meron_nights }} ({{ sm.meron }}) · driver {{ sm.drives }} ({{ sm.driver }}) · <b>total {{ sm.total }}</b></div>
{% if dsm %}<div class="box"><b>Kollel Horaah:</b> {% if dsm.vacation %}no kollel this month, average of the last 3 months{% else %}
{{ dsm.days }}/{{ dsm.held }} days · on time {{ dsm.kept }} · {{ dsm.hours }} hours{% endif %} · <b>{{ '%.2f'|format(dsm.total) }}</b></div>{% endif %}
{% if uman %}<div class="box"><b>Uman</b><table>{% for u in uman %}<tr><td>{{ u.heb_year }}</td>
<td>{{ 'travelled' if u.went else 'did not travel' }}</td><td>{% if u.went %}{{ (u.percent * 100)|round(1) }}% · nights: {{ u.nights or 'none found' }}{% endif %}</td>
</tr>{% endfor %}</table></div>{% endif %}
<div class="box"><b>Add a shift manually</b><form method="post" action="{{ url_for('admin.add_attendance', eid=e.id) }}">
In <input type="datetime-local" name="clock_in" required> Out <input type="datetime-local" name="clock_out">
Note <input name="note"> <button>Add</button></form></div>
<div class="box"><b>Separate bonus</b> <span class="muted">(not part of the stipend: always paid, not counted for the minimum or the percents)</span>
<table>{% for g in gifts %}<tr><td>{{ g.note or '' }}</td><td class="right">{{ '%.2f'|format(g.amount) }} ₪</td>
<td class="muted">{{ fmt(g.created_at) }}{{ ' · by phone' if g.added_by and g.added_by > 0 else '' }}</td>
<td><form method="post" action="{{ url_for('admin.delete_gift', gid=g.id) }}"><button>Remove</button></form></td></tr>{% endfor %}</table>
<form method="post" action="{{ url_for('admin.add_gift', eid=e.id, hy=hy, hm=hm) }}">
Amount ₪ <input name="amount" type="number" step="0.01" required> For <input name="note"> <button>Add separate bonus</button></form></div>
<div class="box"><b>Stipend additions this month</b> <span class="muted">(part of the stipend)</span><table>{% for b in bons %}<tr><td>{{ b.note or '' }}</td>
<td class="right">{{ '%.2f'|format(b.amount) }} ₪</td><td><form method="post" action="{{ url_for('admin.delete_bonus', bid=b.id) }}">
<button>Remove</button></form></td></tr>{% endfor %}</table>
<form method="post" action="{{ url_for('admin.add_bonus', eid=e.id, hy=hy, hm=hm) }}">
Amount ₪ <input name="amount" type="number" step="0.01" required> For <input name="note" placeholder="e.g. shuttle driver"> <button>Add bonus</button></form></div>"""
    nights, by_night = month_nights(e, hy, hm, [x for x in recs if x["program"] == "night"])
    dsm = schedule.day_month_summary(e, hy, hm) if schedule.in_day(e) else None
    pay = {}
    for n, (d, rs) in zip(nights, sorted(by_night.items())):
        pay[rs[0]["id"]] = n
    sm = schedule.month_summary(e, hy, hm)
    drove = set(schedule.driver_nights(eid, nfirst, nlast))
    return page(body, e=e, recs=recs, bons=bons, gifts=gifts, uman=uman, pay=pay, sm=sm, dsm=dsm, hy=hy, hm=hm, drove=drove,
                session_of=lambda a: schedule.session_date(db.parse(a["clock_in"])),
                title=f"{hebcal.month_name(hm, hy)} {hy}", fmt=fmt, heb=lambda d: hebcal.format_date(d), nfirst=nfirst, nlast=nlast,
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


@bp.route("/employee/<int:eid>/attendance", methods=["POST"])
def add_attendance(eid):
    t_in, t_out = parse_local(request.form["clock_in"]), parse_local(request.form.get("clock_out"))
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
    body = """<h1>Edit shift</h1><div class="box"><form method="post">
In <input type="datetime-local" name="clock_in" value="{{ local(a.clock_in) }}" required>
Out <input type="datetime-local" name="clock_out" value="{{ local(a.clock_out) }}">
Note <input name="note" value="{{ a.note or '' }}"> <button>Save</button></form>
<form method="post" onsubmit="return confirm('Delete this shift?')"><input type="hidden" name="delete" value="1">
<button style="background:#b00020;margin-top:10px">Delete shift</button></form></div>"""
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
        c.execute("INSERT INTO gifts(employee_id, heb_year, heb_month, amount, note, created_at, added_by) VALUES (?,?,?,?,?,?,?)",
                  (eid, hy, hm, float(request.form["amount"]), request.form.get("note"), db.now().isoformat(), -1))
    return redirect(url_for("admin.employee_month", eid=eid, hy=hy, hm=hm))


@bp.route("/gift/<int:gid>/delete", methods=["POST"])
def delete_gift(gid):
    with db.connect() as c:
        g = c.execute("SELECT * FROM gifts WHERE id=?", (gid,)).fetchone()
        c.execute("DELETE FROM gifts WHERE id=?", (gid,))
    return redirect(url_for("admin.employee_month", eid=g["employee_id"], hy=g["heb_year"], hm=g["heb_month"]))


@bp.route("/bonus/<int:bid>/delete", methods=["POST"])
def delete_bonus(bid):
    with db.connect() as c:
        b = c.execute("SELECT * FROM bonuses WHERE id=?", (bid,)).fetchone()
        c.execute("DELETE FROM bonuses WHERE id=?", (bid,))
    return redirect(url_for("admin.employee_month", eid=b["employee_id"], hy=b["heb_year"], hm=b["heb_month"]))


# ---- employees -----------------------------------------------------------

EMP_FORM = """<form method="post">
<input name="emp_number" placeholder="Employee no." value="{{ e.emp_number if e else '' }}" required>
<input name="name" placeholder="Name" value="{{ e.name if e else '' }}" required>
<input name="id_number" placeholder="ID number (ת.ז)" value="{{ (e.id_number or '') if e else '' }}">
<input name="phone" placeholder="Phone (calls go here)" value="{{ (e.phone or '') if e else '' }}">
<input name="phone2" placeholder="Additional phone" value="{{ (e.phone2 or '') if e else '' }}">
<select name="phone2_mode" title="Calls to the additional phone">{% for k, v in {'none': 'No calls to it', 'shuttle': 'Call it: shuttle only', 'all': 'Call it: shuttle + messages'}.items() %}
<option value="{{k}}" {% if e and e.phone2_mode==k %}selected{% endif %}>{{v}}</option>{% endfor %}</select>
<input name="pin" placeholder="Password (empty = 1480)" value="{{ e.pin if e else '' }}" pattern="[0-9]{4,8}" title="4-8 digits. Empty = the default 1480">
<select name="role">{% for k, v in roles.items() %}<option value="{{k}}" {% if e and e.role==k %}selected{% endif %}>{{v}}</option>{% endfor %}</select>
<input name="shift" placeholder="Shift (e.g. morning)" value="{{ (e.shift or '') if e else '' }}">
<select name="shuttle_group"><option value="">No shuttle</option>{% for g in (1,2) %}<option value="{{g}}" {% if e and e.shuttle_group==g %}selected{% endif %}>{{ 'Maor Chaim (R. Uriel Darshan)' if g == 1 else 'Darom (R. Naftali Shapira)' }}</option>{% endfor %}</select>
<input name="hourly_rate" type="number" step="0.01" placeholder="Custom rate (optional)" value="{{ e.hourly_rate if e and e.hourly_rate is not none else '' }}">
<label><input type="checkbox" name="in_night" {% if (not e and not form_day) or (e and e.in_night) %}checked{% endif %}> Kollel Chatzos</label>
<label><input type="checkbox" name="in_day" {% if (not e and form_day) or (e and e.in_day) %}checked{% endif %}> Kollel Horaah</label>
<label><input type="checkbox" name="is_driver" {% if e and e.is_driver %}checked{% endif %}> Shuttle driver</label>
<select name="language">{% for k, v in langs.items() %}<option value="{{k}}" {% if e and e.language==k %}selected{% endif %}>{{v}}</option>{% endfor %}</select>
<input name="shuttle_calls" type="number" min="1" max="5" placeholder="Shuttle calls (1-5)" title="Shuttle alert calls - the person sets this on the phone" value="{{ e.shuttle_calls if e and e.shuttle_calls else '' }}">
{% if e %}<label><input type="checkbox" name="active" {% if e.active %}checked{% endif %}> Active</label>{% endif %}
<button>{{ 'Save' if e else 'Add employee' }}</button></form>"""


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
            form.get("phone2_mode") if form.get("phone2_mode") in ("none", "shuttle", "all") else "none")


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
    if request.method == "POST":
        try:
            if phone_taken(request.form.get("phone")) or phone_taken(request.form.get("phone2")):
                raise ValueError("This phone number already belongs to another employee")
            with db.connect() as c:
                c.execute("INSERT INTO employees(emp_number,name,phone,pin,role,shift,shuttle_group,hourly_rate,is_driver,"
                          "language,shuttle_calls,id_number,in_night,in_day,phone2,phone2_mode,joined_on) "
                          "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          emp_values(request.form) + (db.now().date().isoformat(),))
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
    body = """<h1>{{ {'night': 'Kollel Chatzos avrechim', 'day': 'Kollel Horaah avrechim', 'none': 'Avrechim in no kollel'}[kollel] }}</h1>
<p><a href="{{ url_for('admin.employees', k='night') }}">Kollel Chatzos</a> · <a href="{{ url_for('admin.employees', k='day') }}">Kollel Horaah</a>
{% if orphans or kollel == 'none' %} · <a class="warn" href="{{ url_for('admin.employees', k='none') }}">In no kollel ({{ orphans }})</a>{% endif %}
<span class="muted"> · {{ emps|length }} here. An avrech in both kollels appears on both pages.</span></p>
{% if no_id %}<p class="warn">{{ no_id }} active avrechim here have no ID number (ת.ז).</p>{% endif %}
{% if error %}<p class="warn">{{ error }}</p>{% endif %}
<div class="box"><b>Add avrech</b>""" + EMP_FORM + """</div>
<div class="wrap"><table><tr><th>No.</th><th>Name</th><th>ID (ת.ז)</th><th>Phone</th><th>Role</th><th>Shift</th><th>Shuttle</th><th>Rate/hour</th><th>Language</th><th>Status</th><th></th></tr>
{% for x in emps %}<tr class="{{ '' if x.active else 'muted' }}"><td>{{ x.emp_number }}</td><td>{{ x.name }}</td><td>{% if x.id_number %}{{ x.id_number }}{% else %}<span class="warn">missing</span>{% endif %}</td><td>{{ x.phone or '' }}{% if x.phone2 %}<br><span class="muted">{{ x.phone2 }}</span>{% endif %}</td>
<td>{{ roles[x.role] }}{% if x.is_driver %} · driver{% endif %}
<span class="muted">{{ 'Chatzos' if x.in_night else '' }}{{ ' + ' if x.in_night and x.in_day else '' }}{{ 'Horaah' if x.in_day else '' }}</span></td><td>{{ x.shift or '' }}</td><td>{{ {1: 'Maor Chaim', 2: 'Darom'}.get(x.shuttle_group, '') }}</td>
<td>{% set r = rate(x) %}{% if r is none %}<span class="warn">not set</span>{% else %}{{ r }}{% endif %}</td>
<td>{{ langs[x.language] }}{% if x.shuttle_group %} · {{ x.shuttle_calls or '?' }} calls{% endif %}</td>
<td>{% if removed(x) %}<span class="warn">removed (2 months)</span>
<form method="post" action="{{ url_for('admin.restore', eid=x.id) }}" style="display:inline"><button>Restore</button></form>{% endif %}</td>
<td><a href="{{ url_for('admin.edit_employee', eid=x.id) }}">edit</a></td></tr>{% endfor %}</table></div>"""
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
    if request.method == "POST":
        if phone_taken(request.form.get("phone"), eid) or phone_taken(request.form.get("phone2"), eid):
            error = "This phone number already belongs to another employee"
        else:
            with db.connect() as c:
                c.execute("UPDATE employees SET emp_number=?,name=?,phone=?,pin=?,role=?,shift=?,shuttle_group=?,"
                          "hourly_rate=?, is_driver=?, language=?, shuttle_calls=?, id_number=?, in_night=?, in_day=?, phone2=?, phone2_mode=?, active=? WHERE id=?",
                          emp_values(request.form) + (1 if request.form.get("active") else 0, eid))
            return redirect(url_for("admin.employees", k="night" if request.form.get("in_night") else
                                    "day" if request.form.get("in_day") else "none"))
    with db.connect() as c:
        e = c.execute("SELECT * FROM employees WHERE id=?", (eid,)).fetchone()
    return page("<h1>Edit employee</h1>{% if error %}<p class='warn'>{{ error }}</p>{% endif %}<div class='box'>"
                + EMP_FORM + "</div>", e=e, roles=ROLE_NAMES, langs=LANGS, error=error)


# ---- messages ------------------------------------------------------------

@bp.route("/messages")
def messages():
    since = (db.now() - timedelta(days=int(request.args.get("days", 30)))).isoformat()
    with db.connect() as c:
        msgs = c.execute("SELECT m.*, e.name sender FROM messages m JOIN employees e ON e.id=m.sender_id "
                         "WHERE m.created_at>=? ORDER BY m.created_at DESC", (since,)).fetchall()
        rcpts = c.execute("SELECT r.*, e.name FROM message_recipients r JOIN employees e ON e.id=r.employee_id "
                          "JOIN messages m ON m.id=r.message_id WHERE m.created_at>=?", (since,)).fetchall()
    by_msg = {}
    for r in rcpts:
        by_msg.setdefault(r["message_id"], []).append(r)
    body = """<h1>Messages (last 30 days)</h1>
{% for m in msgs %}<div class="box"><b>{{ m.sender }}</b> → {{ m.recipient_label }}
<span class="muted"> · {{ m.heb_date }} · {{ fmt(m.created_at) }}</span>
<div><audio controls preload="none" src="{{ url_for('admin.recording', mid=m.id) }}"></audio></div>
<table><tr><th>Recipient</th><th>Notification call</th><th>Listened</th></tr>{% for r in by_msg.get(m.id, []) %}
<tr><td>{{ r.name }}</td><td>{{ r.delivery_status }}</td><td>{{ fmt(r.listened_at) if r.listened_at else '—' }}</td></tr>{% endfor %}
</table></div>{% else %}<p class="muted">No messages yet.</p>{% endfor %}"""
    return page(body, msgs=msgs, by_msg=by_msg, fmt=fmt)


@bp.route("/recording/<int:mid>")
def recording(mid):
    with db.connect() as c:
        m = c.execute("SELECT recording_path FROM messages WHERE id=?", (mid,)).fetchone()
    data = nbs.download_recording(m["recording_path"]) if m else None
    if not data:
        return Response("Recording not available", 404)
    return Response(data, mimetype="audio/wav")


# ---- attendance corrections (extension 5) --------------------------------

from corrections import apply_correction, meron_rule  # noqa: E402  (shared with the phone)


@bp.route("/corrections", methods=["GET", "POST"])
def corrections():
    msg = None
    if request.method == "POST":
        cid, action = int(request.form["id"]), request.form["action"]
        with db.connect() as c:
            corr = c.execute("SELECT * FROM corrections WHERE id=? AND status='pending'", (cid,)).fetchone()
            if corr and action == "approve":
                ok, msg = apply_correction(c, corr)
                if ok:
                    c.execute("UPDATE corrections SET status='approved', result=? WHERE id=?", (msg, cid))
            elif corr:
                c.execute("UPDATE corrections SET status='rejected' WHERE id=?", (cid,))
    with db.connect() as c:
        rows = c.execute("SELECT k.*, e.name, e.emp_number FROM corrections k JOIN employees e ON e.id=k.employee_id "
                         "ORDER BY k.status='pending' DESC, k.created_at DESC LIMIT 200").fetchall()
    body = """<h1>Attendance corrections</h1>{% if msg %}<p class="warn">{{ msg }}</p>{% endif %}
<p class="muted">Requests employees made on the phone (key 5). Nothing changes until you approve.
For a forgotten clock-in and clock-out on the same day, approve the clock-in first.</p>
<div class="wrap"><table><tr><th>Requested</th><th>Employee</th><th>Fix</th><th>Time</th><th>Status</th><th></th></tr>
{% for k in rows %}<tr><td>{{ fmt(k.created_at) }}</td><td>{{ k.name }} ({{ k.emp_number }})</td>
<td>{{ 'Clock-in' if k.kind=='in' else 'Clock-out' }}</td><td>{{ fmt(k.requested_time) }}</td>
<td>{{ k.status }}{% if k.result %} · <span class="muted">{{ k.result }}</span>{% endif %}</td>
<td>{% if k.status=='pending' %}<form method="post" style="display:inline"><input type="hidden" name="id" value="{{k.id}}">
<button name="action" value="approve">Approve</button> <button name="action" value="reject" style="background:#777">Reject</button>
</form>{% endif %}</td></tr>{% else %}<tr><td colspan="6" class="muted">No requests.</td></tr>{% endfor %}</table></div>"""
    return page(body, rows=rows, fmt=fmt, msg=msg)


# ---- shuttle departures (key 3) ------------------------------------------

@bp.route("/shuttles", methods=["GET", "POST"])
def shuttles():
    msg = None
    if request.method == "POST":
        ok, info = nbs.set_redial(int(request.form.get("attempts", 3)), int(request.form.get("wait", 120)))
        msg = "Redial settings saved." if ok else f"Could not save: {info}"
    with db.connect() as c:
        rows = c.execute("SELECT s.*, e.name FROM shuttle_runs s JOIN employees e ON e.id=s.driver_id "
                         "ORDER BY s.created_at DESC LIMIT 100").fetchall()
    body = """<h1>Shuttle departures</h1>{% if msg %}<p class="warn">{{ msg }}</p>{% endif %}
<div class="wrap"><table><tr><th>Time</th><th>Driver</th><th>Group</th><th>Called</th><th>Result</th></tr>
{% for x in rows %}<tr><td>{{ fmt(x.created_at) }}</td><td>{{ x.name }}</td><td>{{ x.shuttle_group }}</td>
<td>{{ x.recipients }}</td><td>{{ x.status }}</td></tr>{% else %}<tr><td colspan="5" class="muted">None yet.</td></tr>{% endfor %}
</table></div>
<div class="box"><b>Retry unanswered calls</b> (applies to the campaign set in NBS_TEMPLATE_ID)
<form method="post">Attempts <input name="attempts" type="number" value="3" min="1" max="5">
Wait between attempts <input name="wait" type="number" value="120" step="30" min="60"> seconds
<button>Save</button></form></div>"""
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
            msg = "No file"
        elif parsed:
            rows = importer.match(parsed)
            action = request.form.get("action")
            if action in ("import", "create_and_import"):
                made = importer.create_missing(rows) if action == "create_and_import" else 0
                rows = importer.match(rows)
                added = importer.commit(rows)
                with db.connect() as c:
                    c.execute("DELETE FROM settings WHERE key=?", (f"import_{token}",))
                msg = f"Imported {added} records." + (f" Created {made} new avrechim (password 1480, add their phones)." if made else "")
                rows, token = None, None
            else:
                warns = importer.warnings(rows)
    counts = {}
    for r in rows or []:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    body = """<h1>Import past attendance</h1>{% if msg %}<p class="warn">{{ msg }}</p>{% endif %}
{% for e in errors %}<p class="warn">Could not read {{ e }}</p>{% endfor %}
<div class="box"><form method="post" enctype="multipart/form-data">
Files from the old system (CSV or Excel, you can choose many at once): <input type="file" name="file" multiple required accept=".csv,.txt,.xlsx"> <button>Check</button>
</form><p class="muted">Columns: זהות, שם משפחה, שם פרטי, כניסה, יציאה. Avrechim are matched by ID number (ת.ז), otherwise by name.
Records between 14:00 and 20:00 go to Kollel Horaah, the rest to Kollel Chatzos.
Nothing is saved until you press Import. Records already in the system are skipped.</p></div>
{% if rows %}
{% if warns %}<div class="box" style="border-color:#c62828"><b>Please check:</b><ul>{% for w in warns %}<li>{{ w }}</li>{% endfor %}</ul></div>{% endif %}
<div class="box">
<b>{{ counts.get('new', 0) }}</b> records to import · {{ counts.get('exists', 0) }} already there ·
{{ counts.get('duplicate', 0) }} repeated in the files ·
<span class="{{ 'warn' if counts.get('no match') else '' }}">{{ counts.get('no match', 0) }} with no matching avrech</span> ·
{{ counts.get('bad date', 0) }} unreadable
<form method="post" style="margin-top:10px"><input type="hidden" name="token" value="{{ token }}">
<button name="action" value="import">Import matched records</button>
{% if counts.get('no match') %}<button name="action" value="create_and_import" style="background:#2e7d32">Create the missing avrechim and import all</button>{% endif %}
</form></div>
<div class="wrap"><table><tr><th>File</th><th>ID</th><th>Name in file</th><th>Matched to</th><th>Kollel</th><th>In</th><th>Out</th><th>Status</th></tr>
{% for r in rows %}<tr><td class="muted">{{ r.sheet }}</td><td>{{ r.id }}</td><td>{{ r.name }}</td><td>{{ r.emp.name if r.emp else '' }}</td>
<td class="{{ 'warn' if r.emp and r.program and not r.emp['in_' + r.program] else '' }}">{{ r.program or '' }}</td>
<td>{{ r['in'].strftime('%d/%m/%Y %H:%M') if r['in'] else '' }}</td><td>{{ r.out.strftime('%d/%m/%Y %H:%M') if r.out else '' }}</td>
<td class="{{ 'warn' if r.status in ('no match', 'bad date') else 'muted' if r.status in ('exists', 'duplicate') else '' }}">{{ r.status }}</td></tr>{% endfor %}
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
        return redirect(url_for("admin.tefila_page", day=day, msg=f"Added {added}"))
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
    body = """<h1>Mivtza Tefila B'Koach</h1>
<div class="box">Key {{ camp.key }} · {{ camp.first.strftime('%d/%m/%Y') }} – {{ camp.last.strftime('%d/%m/%Y') }} (Zos Chanuka) · Kollel Chatzos only.
Open from {{ camp.opens_before }} to {{ camp.closes_before }} minutes before netz (Tzfas, Itim LeBina).<br>
Today: {% if w %}netz {{ netz.strftime('%H:%M:%S') }} · open {{ w[0].strftime('%H:%M') }}–{{ w[1].strftime('%H:%M') }}{% else %}<span class="warn">no netz time for today - closed</span>{% endif %}
{% if missing %}<br><span class="warn">Missing netz times for {{ missing|length }} days (first: {{ missing[0].strftime('%d/%m/%Y') }}) - those days the key stays closed.</span>{% endif %}</div>
{% if winners is not none %}<div class="box" style="border-color:#2e7d32"><b>Draw result (saved):</b><ol>{% for x in winners %}<li dir="rtl">{{ x.prize }}: <b>{{ x.name }}</b></li>{% endfor %}</ol></div>{% endif %}
<div class="box"><b>Entries per avrech</b> (every morning = one entry) · total <b>{{ totals|sum(attribute='entries') }}</b>
<div class="wrap"><table><tr><th>No.</th><th>Name</th><th>Phone</th><th class="right">Entries</th><th>Last</th></tr>
{% for x in totals %}<tr><td>{{ x.emp_number }}</td><td>{{ x.name }}</td><td>{{ x.phone or '' }}</td><td class="right"><b>{{ x.entries }}</b></td><td>{{ x.last_day }}</td></tr>
{% else %}<tr><td colspan="5" class="muted">No entries yet.</td></tr>{% endfor %}</table></div>
<form method="post" onsubmit="return confirm('Draw the winners now? Every draw is saved.')" style="margin-top:10px">
<button name="action" value="draw" style="background:#2e7d32">Draw the winners</button>
<span class="muted"> {{ camp.prizes|length }} prizes, weighted by entries, each avrech wins once.</span></form></div>
<div class="box"><b>Add avrechim by hand</b> <span class="muted">(for those who did not manage to register)</span>
<form method="post"><input type="hidden" name="action" value="add">
Day <input type="date" name="day" value="{{ day }}"> <select name="employee_id" multiple size="6" style="min-width:260px">
{% for e in avrechim %}<option value="{{ e.id }}">{{ e.name }} ({{ e.emp_number }})</option>{% endfor %}</select>
<button>Add</button> <span class="muted">Ctrl+click to choose several.</span></form>
{% if request.args.get('msg') %}<p class="warn">{{ request.args.get('msg') }}</p>{% endif %}</div>
<div class="box"><b>By day</b>: {% for x in days %}<a href="?day={{ x.day }}">{{ x.day }}</a> ({{ x.n }}){{ ' · ' if not loop.last }}{% endfor %}
<p><b>{{ day }}</b></p><table><tr><th>Name</th><th>Registered at</th><th></th></tr>
{% for x in list %}<tr><td>{{ x.name }}</td><td>{{ fmt(x.created_at) }}{% if x.added_by is not none %} <span class="muted">· added by hand</span>{% endif %}</td><td><form method="post" onsubmit="return confirm('Remove this entry?')">
<input type="hidden" name="action" value="remove"><input type="hidden" name="employee_id" value="{{ x.employee_id }}">
<input type="hidden" name="day" value="{{ x.day }}"><button>Remove</button></form></td></tr>
{% else %}<tr><td colspan="3" class="muted">Nobody.</td></tr>{% endfor %}</table></div>
{% if draws %}<div class="box"><b>Previous draws</b>{% for d in draws %}<p>{{ fmt(d.created_at) }} · {{ d.tickets }} entries:
{% for x in d.winners %}<span dir="rtl">{{ x.prize }}: <b>{{ x.name }}</b></span>{{ ' · ' if not loop.last }}{% endfor %}</p>{% endfor %}</div>{% endif %}"""
    with db.connect() as c:
        avrechim = c.execute("SELECT id, name, emp_number FROM employees WHERE active=1 AND in_night=1 ORDER BY name").fetchall()
    return page(body, camp=camp, w=w, netz=tefila.netz(today), missing=missing, winners=winners, avrechim=avrechim, request=request,
                totals=tefila.totals(), days=tefila.by_day(), day=day, list=tefila.entries_of_day(day),
                draws=tefila.draws(), fmt=fmt)


# ---- temporary registrations (trips.py) -----------------------------------

@bp.route("/registrations", methods=["GET", "POST"])
def registrations():
    if request.method == "POST":
        with db.connect() as c:
            c.execute("DELETE FROM trip_signups WHERE trip=? AND employee_id=?",
                      (request.form["trip"], int(request.form["employee_id"])))
        return redirect(url_for("admin.registrations"))
    now = db.now()
    data = []
    for t in reversed(trips.TRIPS):
        rows = trips.signups(t["id"])
        counts = {k: sum(1 for x in rows if x["choice"] == k) for k in t["options"]}
        data.append((t, rows, counts, now < t["until"]))
    body = """<h1>Registrations</h1>
{% for t, rows, counts, is_open in data %}<div class="box"><b dir="rtl">{{ t.title }}</b><br>
<span class="{{ '' if is_open else 'muted' }}">Key {{ t.key }} · {{ 'open until' if is_open else 'closed on' }} {{ t.until.strftime('%d/%m/%Y %H:%M') }}</span>
<p><b>{{ rows|length }}</b> registered{% for k, o in t.options.items() %} · {{ o.admin }}: <b>{{ counts[k] }}</b>{% endfor %}
 · <a href="{{ url_for('admin.registrations_csv', trip=t.id) }}">Download CSV</a></p>
<div class="wrap"><table><tr><th>No.</th><th>Name</th><th>Phone</th><th>Choice</th><th>Registered</th><th></th></tr>
{% for x in rows %}<tr><td>{{ x.emp_number }}</td><td>{{ x.name }}</td><td>{{ x.phone or '' }}</td>
<td>{{ t.options[x.choice].admin }}</td><td>{{ fmt(x.created_at) }}</td>
<td><form method="post" onsubmit="return confirm('Remove this registration?')"><input type="hidden" name="trip" value="{{ t.id }}">
<input type="hidden" name="employee_id" value="{{ x.employee_id }}"><button>Remove</button></form></td></tr>{% endfor %}
</table></div></div>{% else %}<p class="muted">No registrations.</p>{% endfor %}"""
    return page(body, data=data, fmt=fmt)


@bp.route("/registrations/<trip>.csv")
def registrations_csv(trip):
    t = trips.by_id(trip)
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["No.", "Name", "Phone", "Choice", "Registered"])
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
    body = """<h1>Kollel Horaah · {{ title }}</h1>
<p class="muted">{{ first.strftime('%d/%m/%Y') }} – {{ last.strftime('%d/%m/%Y') }} · {{ days|length }} kollel days (Sun–Thu, 16:00–19:00, clock-in from 15:00) ·
<a href="?hy={{py}}&hm={{pm}}">◀ previous month</a> · <a href="?hy={{ny}}&hm={{nm}}">next month ▶</a> ·
<br>Months with records: {% for y, m, name in data_months(program) %}<a href="?hy={{y}}&hm={{m}}" {% if y == hy and m == hm %}style="font-weight:700;text-decoration:none"{% endif %}>{{ name }}</a>{{ ' · ' if not loop.last }}{% endfor %} ·
<form method="post" action="{{ url_for('admin.send_day_report', hy=hy, hm=hm) }}" style="display:inline">
<button>E-mail this summary now</button></form>{% if msg %} <span class="warn">{{ msg }}</span>{% endif %}</p>
<div class="box"><b>Total: {{ '{:,.0f}'.format(total) }}</b> · all paid by Ohr Hatzadik</div>
<div class="wrap"><table><tr><th>Name</th><th class="right">Days</th><th class="right">On time</th><th class="right">Hours</th>
<th class="right">Attendance</th><th class="right">Hours pay</th><th class="right">On-time pay</th><th class="right">Total ₪</th></tr>
{% for r in rows %}<tr><td><a href="{{ url_for('admin.employee_month', eid=r.emp.id, hy=hy, hm=hm) }}">{{ r.emp.name }}</a></td>
{% if r.vacation %}<td colspan="6" class="muted">No Kollel Horaah this month – average of the last 3 months</td>
{% else %}<td class="right">{{ r.days }} / {{ r.held }}</td><td class="right">{{ r.kept }}</td><td class="right">{{ r.hours }}</td>
<td class="right">{{ (r.percent * 100)|round(1) }}%</td><td class="right">{{ '%.2f'|format(r.base) }}</td>
<td class="right">{{ '%.2f'|format(r.keep) }}{% if r.average %} <span class="muted">+ {{ '%.2f'|format(r.average) }} average</span>{% endif %}</td>{% endif %}<td class="right"><b>{{ '%.2f'|format(r.total) }}</b></td></tr>
{% else %}<tr><td colspan="8" class="muted">No avrechim are marked "Kollel Horaah" yet (Employees → edit).</td></tr>{% endfor %}
</table></div>
<p class="muted">800 ₪ a month divided by the month's kollel hours, for the hours present. 200 ₪ a month divided by the
month's kollel days, for each day he came by 16:00. Nisan and Tishrei: the average of the last 3 months. Av: 1–7 Av by attendance, the rest of the month by the average.
No kollel on Chanuka, Purim, Shushan Purim, fasts, erev Shavuos, isru chag Shavuos, Lag BaOmer, and 8 Av to 1 Elul.</p>"""
    return page(body, program="day", rows=rows, hy=hy, hm=hm, py=py, pm=pm, ny=ny, nm=nm, first=first, last=last, days=days,
                total=total, title=f"{hebcal.month_name(hm, hy)} {hy}", msg=request.args.get("msg"))


@bp.route("/day/send-report", methods=["POST"])
def send_day_report():
    hy, hm = month_args(night=False)
    ok, info = report.send_day(hy, hm)
    msg = "The summary was e-mailed." if ok else f"Could not send: {info}"
    return redirect(url_for("admin.day_payroll", hy=hy, hm=hm, msg=msg))
