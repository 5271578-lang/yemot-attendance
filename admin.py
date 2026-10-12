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

bp = Blueprint("admin", __name__, url_prefix="/admin")
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
ROLE_NAMES = {"employee": "Employee", "onsite_manager": "On-Site Manager", "tech_manager": "Technical Manager"}
LANGS = {"he": "Hebrew", "en": "English", "yi": "Yiddish"}


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
</style></head><body><nav><a href="{{ url_for('admin.payroll') }}">Attendance &amp; Pay</a>
<a href="{{ url_for('admin.employees') }}">Employees</a><a href="{{ url_for('admin.corrections') }}">Corrections</a>
<a href="{{ url_for('admin.messages') }}">Messages</a><a href="{{ url_for('admin.shuttles') }}">Shuttles</a></nav>
<main>{% block body %}{% endblock %}</main></body></html>"""


def page(body, **ctx):
    return render_template_string(BASE.replace("{% block body %}{% endblock %}", body), **ctx)


def current_heb_month():
    y, m, _ = hebcal.from_gregorian(db.now().date())
    return y, m


def month_args():
    cy, cm = current_heb_month()
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
        att = c.execute("SELECT * FROM attendance WHERE heb_year=? AND heb_month=?", (hy, hm)).fetchall()
        bon = c.execute("SELECT employee_id, SUM(amount) s FROM bonuses WHERE heb_year=? AND heb_month=? "
                        "GROUP BY employee_id", (hy, hm)).fetchall()
    extra = {b["employee_id"]: b["s"] for b in bon}
    out = []
    for e in emps:
        nights, by_night = month_nights(e, hy, hm, att)
        if not nights and not e["active"] and e["id"] not in extra:
            continue
        rate_ok = schedule.nightly_base(e) is not None
        base = round(sum(n["base"] for n in nights if n), 2)
        timing = sum(n["bonus"] for n in nights if n)
        x = round(extra.get(e["id"], 0) or 0, 2)
        out.append(dict(emp=e, nights=len(by_night), hours=round(sum(n["minutes"] for n in nights if n) / 60, 2),
                        nightly=schedule.nightly_base(e), base=base, timing=timing, extra=x,
                        total=round(base + timing + x, 2) if rate_ok else None,
                        unconfirmed=sum(1 for recs in by_night.values() for a in recs if not a["exit_confirmed"])))
    return out


@bp.route("/")
def payroll():
    hy, hm = month_args()
    rows = payroll_rows(hy, hm)
    first, last = hebcal.month_range(hy, hm)
    py, pm = hebcal.prev_month(hy, hm)
    ny, nm = hebcal.next_month(hy, hm)
    body = """
<h1>{{ title }}</h1>
<p class="muted">{{ first.strftime('%d/%m/%Y') }} – {{ last.strftime('%d/%m/%Y') }} ·
<a href="?hy={{py}}&hm={{pm}}">◀ previous month</a> · <a href="?hy={{ny}}&hm={{nm}}">next month ▶</a> ·
<a href="{{ url_for('admin.export', hy=hy, hm=hm) }}">Download CSV</a></p>
<div class="wrap"><table><tr><th>No.</th><th>Name</th><th>Role</th><th class="right">Nights</th><th class="right">Hours</th>
<th class="right">Nightly base</th><th class="right">Base</th><th class="right">On-time</th><th class="right">Extras</th>
<th class="right">Total ₪</th><th></th></tr>
{% for r in rows %}<tr><td>{{ r.emp.emp_number }}</td>
<td><a href="{{ url_for('admin.employee_month', eid=r.emp.id, hy=hy, hm=hm) }}">{{ r.emp.name }}</a></td>
<td>{{ roles[r.emp.role] }}</td><td class="right">{{ r.nights }}</td><td class="right">{{ '%.2f'|format(r.hours) }}</td>
<td class="right">{% if r.nightly is none %}<span class="warn">not set</span>{% else %}{{ r.nightly }}{% endif %}</td>
<td class="right">{{ '%.2f'|format(r.base) }}</td><td class="right">{{ r.timing }}</td><td class="right">{{ '%.2f'|format(r.extra) }}</td>
<td class="right"><b>{{ '%.2f'|format(r.total) if r.total is not none else '—' }}</b></td>
<td>{% if r.unconfirmed %}<span class="muted">{{ r.unconfirmed }} exit not confirmed yet</span>{% endif %}</td></tr>{% endfor %}
</table></div>
<p class="muted">Per night: nightly base × share of the kollel hours present (2:30–7:30, bein hazmanim 3:30–7:30).
On-time: +20 up to 10 minutes late, +30 before the start. A night with no exit counts until 7:30.
Extras = Uman nights and bonuses added by hand.</p>"""
    title = f"{hebcal.month_name(hm, hy)} {hy}"
    return page(body, rows=rows, hy=hy, hm=hm, py=py, pm=pm, ny=ny, nm=nm, first=first, last=last,
                title=title, roles=ROLE_NAMES)


@bp.route("/export.csv")
def export():
    hy, hm = month_args()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Employee no.", "Name", "Role", "Nights", "Hours", "Nightly base", "Base", "On-time", "Extras", "Total"])
    for r in payroll_rows(hy, hm):
        w.writerow([r["emp"]["emp_number"], r["emp"]["name"], ROLE_NAMES[r["emp"]["role"]], r["nights"], r["hours"],
                    r["nightly"] if r["nightly"] is not None else "", r["base"], r["timing"], r["extra"],
                    r["total"] if r["total"] is not None else ""])
    name = f"payroll-{hebcal.month_name(hm, hy).replace(' ', '')}-{hy}.csv"
    return Response("﻿" + buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={name}"})


@bp.route("/employee/<int:eid>")
def employee_month(eid):
    hy, hm = month_args()
    with db.connect() as c:
        e = c.execute("SELECT * FROM employees WHERE id=?", (eid,)).fetchone()
        recs = c.execute("SELECT * FROM attendance WHERE employee_id=? AND heb_year=? AND heb_month=? "
                         "ORDER BY clock_in", (eid, hy, hm)).fetchall()
        uman = c.execute("SELECT * FROM uman WHERE employee_id=? ORDER BY heb_year DESC", (eid,)).fetchall()
        bons = c.execute("SELECT * FROM bonuses WHERE employee_id=? AND heb_year=? AND heb_month=?",
                         (eid, hy, hm)).fetchall()
    body = """
<h1>{{ e.name }} · {{ title }}</h1><p><a href="{{ url_for('admin.payroll', hy=hy, hm=hm) }}">← back</a></p>
<div class="wrap"><table><tr><th>Hebrew date</th><th>Clock in</th><th>Exit</th><th class="right">Minutes</th>
<th class="right">Base</th><th class="right">On-time</th><th class="right">Night ₪</th><th>Note</th><th></th></tr>
{% for a in recs %}{% set n = pay.get(a.id) %}<tr><td>{{ a.heb_day }} {{ title }}{% if n and n.bhz %} · <span class="muted">bein hazmanim</span>{% endif %}</td>
<td>{{ fmt(a.clock_in) }}</td>
<td>{% if a.clock_out %}{{ fmt(a.clock_out) }}{% if a.early_exit %} <span class="muted">early</span>{% endif %}{% else %}<span class="muted">7:30 (default)</span>{% endif %}
{% if not a.exit_confirmed %} <span class="muted">· not confirmed</span>{% endif %}</td>
<td class="right">{{ n.minutes if n else '' }}</td><td class="right">{{ n.base if n else '' }}</td>
<td class="right">{{ n.bonus if n else '' }}</td><td class="right"><b>{{ n.total if n else '' }}</b></td><td>{{ a.note or '' }}</td>
<td><a href="{{ url_for('admin.edit_attendance', aid=a.id) }}">edit</a></td></tr>{% endfor %}</table></div>
{% if uman %}<div class="box"><b>Uman</b><table>{% for u in uman %}<tr><td>{{ u.heb_year }}</td>
<td>{{ 'travelled' if u.went else 'did not travel' }}</td><td>{% if u.went %}{{ (u.percent * 100)|round(1) }}% · nights: {{ u.nights or 'none found' }}{% endif %}</td>
</tr>{% endfor %}</table></div>{% endif %}
<div class="box"><b>Add a shift manually</b><form method="post" action="{{ url_for('admin.add_attendance', eid=e.id) }}">
In <input type="datetime-local" name="clock_in" required> Out <input type="datetime-local" name="clock_out">
Note <input name="note"> <button>Add</button></form></div>
<div class="box"><b>Bonuses this month</b><table>{% for b in bons %}<tr><td>{{ b.note or '' }}</td>
<td class="right">{{ '%.2f'|format(b.amount) }} ₪</td><td><form method="post" action="{{ url_for('admin.delete_bonus', bid=b.id) }}">
<button>Remove</button></form></td></tr>{% endfor %}</table>
<form method="post" action="{{ url_for('admin.add_bonus', eid=e.id, hy=hy, hm=hm) }}">
Amount ₪ <input name="amount" type="number" step="0.01" required> For <input name="note" placeholder="e.g. shuttle driver"> <button>Add bonus</button></form></div>"""
    nights, by_night = month_nights(e, hy, hm, recs)
    pay = {}
    for n, (d, rs) in zip(nights, sorted(by_night.items())):
        pay[rs[0]["id"]] = n
    return page(body, e=e, recs=recs, bons=bons, uman=uman, pay=pay, hy=hy, hm=hm,
                title=f"{hebcal.month_name(hm, hy)} {hy}", fmt=fmt)


def parse_local(value):
    if not value:
        return None
    return datetime.fromisoformat(value).replace(tzinfo=db.TZ)


def heb_fields(dt):
    return hebcal.from_gregorian(schedule.session_date(dt))


@bp.route("/employee/<int:eid>/attendance", methods=["POST"])
def add_attendance(eid):
    t_in, t_out = parse_local(request.form["clock_in"]), parse_local(request.form.get("clock_out"))
    y, m, d = heb_fields(t_in)
    with db.connect() as c:
        c.execute("INSERT INTO attendance(employee_id, clock_in, clock_out, heb_year, heb_month, heb_day, note, "
                  "exit_confirmed) VALUES (?,?,?,?,?,?,?,1)", (eid, t_in.isoformat(), t_out.isoformat() if t_out else None,
                                                              y, m, d, request.form.get("note") or "added by admin"))
    return redirect(url_for("admin.employee_month", eid=eid, hy=y, hm=m))


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
                c.execute("UPDATE attendance SET clock_in=?, clock_out=?, heb_year=?, heb_month=?, heb_day=?, note=?, exit_confirmed=1 "
                          "WHERE id=?", (t_in.isoformat(), t_out.isoformat() if t_out else None, y, m, d,
                                         request.form.get("note"), aid))
            return redirect(url_for("admin.employee_month", eid=a["employee_id"], hy=a["heb_year"], hm=a["heb_month"]))
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
<input name="phone" placeholder="Phone (sign-in + caller ID)" value="{{ (e.phone or '') if e else '' }}">
<input name="pin" placeholder="Password (4-8 digits)" value="{{ e.pin if e else '' }}" required pattern="[0-9]{4,8}" title="4-8 digits">
<select name="role">{% for k, v in roles.items() %}<option value="{{k}}" {% if e and e.role==k %}selected{% endif %}>{{v}}</option>{% endfor %}</select>
<input name="shift" placeholder="Shift (e.g. morning)" value="{{ (e.shift or '') if e else '' }}">
<select name="shuttle_group"><option value="">No shuttle</option>{% for g in (1,2) %}<option value="{{g}}" {% if e and e.shuttle_group==g %}selected{% endif %}>Shuttle Group {{g}}</option>{% endfor %}</select>
<input name="hourly_rate" type="number" step="0.01" placeholder="Custom rate (optional)" value="{{ e.hourly_rate if e and e.hourly_rate is not none else '' }}">
<label><input type="checkbox" name="is_driver" {% if e and e.is_driver %}checked{% endif %}> Shuttle driver</label>
<select name="language">{% for k, v in langs.items() %}<option value="{{k}}" {% if e and e.language==k %}selected{% endif %}>{{v}}</option>{% endfor %}</select>
<input name="shuttle_calls" type="number" min="1" max="5" placeholder="Shuttle calls (1-5)" title="Shuttle alert calls - the person sets this on the phone" value="{{ e.shuttle_calls if e and e.shuttle_calls else '' }}">
{% if e %}<label><input type="checkbox" name="active" {% if e.active %}checked{% endif %}> Active</label>{% endif %}
<button>{{ 'Save' if e else 'Add employee' }}</button></form>"""


def emp_values(form):
    return (form["emp_number"].strip(), form["name"].strip(), form.get("phone", "").strip() or None,
            form["pin"].strip(), form["role"], form.get("shift", "").strip() or None,
            int(form["shuttle_group"]) if form.get("shuttle_group") else None,
            float(form["hourly_rate"]) if form.get("hourly_rate") else None,
            1 if form.get("is_driver") else 0,
            form.get("language") if form.get("language") in LANGS else "he",
            int(form["shuttle_calls"]) if form.get("shuttle_calls") else None)


def phone_taken(phone, eid=None):
    p = db.normalize_phone(phone or "")
    if not p:
        return False
    with db.connect() as c:
        return any(db.normalize_phone(x["phone"]) == p for x in
                   c.execute("SELECT phone FROM employees WHERE phone IS NOT NULL AND id<>?", (eid or -1,)))


@bp.route("/employees", methods=["GET", "POST"])
def employees():
    error = None
    if request.method == "POST":
        try:
            if phone_taken(request.form.get("phone")):
                raise ValueError("This phone number already belongs to another employee")
            with db.connect() as c:
                c.execute("INSERT INTO employees(emp_number,name,phone,pin,role,shift,shuttle_group,hourly_rate,is_driver,"
                          "language,shuttle_calls) VALUES (?,?,?,?,?,?,?,?,?,?,?)", emp_values(request.form))
            return redirect(url_for("admin.employees"))
        except Exception as ex:                       # duplicate number etc.
            error = str(ex)
    with db.connect() as c:
        emps = c.execute("SELECT * FROM employees ORDER BY active DESC, name").fetchall()
    body = """<h1>Employees</h1>{% if error %}<p class="warn">{{ error }}</p>{% endif %}
<div class="box"><b>Add employee</b>""" + EMP_FORM + """</div>
<div class="wrap"><table><tr><th>No.</th><th>Name</th><th>Phone</th><th>Role</th><th>Shift</th><th>Shuttle</th><th>Rate/hour</th><th>Language</th><th></th></tr>
{% for x in emps %}<tr class="{{ '' if x.active else 'muted' }}"><td>{{ x.emp_number }}</td><td>{{ x.name }}</td><td>{{ x.phone or '' }}</td>
<td>{{ roles[x.role] }}{% if x.is_driver %} · driver{% endif %}</td><td>{{ x.shift or '' }}</td><td>{{ x.shuttle_group or '' }}</td>
<td>{% set r = rate(x) %}{% if r is none %}<span class="warn">not set</span>{% else %}{{ r }}{% endif %}</td>
<td>{{ langs[x.language] }}{% if x.shuttle_group %} · {{ x.shuttle_calls or '?' }} calls{% endif %}</td>
<td><a href="{{ url_for('admin.edit_employee', eid=x.id) }}">edit</a></td></tr>{% endfor %}</table></div>"""
    return page(body, emps=emps, e=None, roles=ROLE_NAMES, rate=db.rate_for, error=error, langs=LANGS)


@bp.route("/employees/<int:eid>", methods=["GET", "POST"])
def edit_employee(eid):
    error = None
    if request.method == "POST":
        if phone_taken(request.form.get("phone"), eid):
            error = "This phone number already belongs to another employee"
        else:
            with db.connect() as c:
                c.execute("UPDATE employees SET emp_number=?,name=?,phone=?,pin=?,role=?,shift=?,shuttle_group=?,"
                          "hourly_rate=?, is_driver=?, language=?, shuttle_calls=?, active=? WHERE id=?",
                          emp_values(request.form) + (1 if request.form.get("active") else 0, eid))
            return redirect(url_for("admin.employees"))
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

def apply_correction(c, corr):
    """Returns (ok, explanation)."""
    t = db.parse(corr["requested_time"])
    day_start = t.replace(hour=0, minute=0, second=0)
    day_end = day_start + timedelta(days=1)
    rows = c.execute("SELECT * FROM attendance WHERE employee_id=? AND clock_in>=? AND clock_in<? ORDER BY clock_in",
                     (corr["employee_id"], day_start.isoformat(), day_end.isoformat())).fetchall()
    if corr["kind"] == "in":
        if rows:
            row = rows[0]
            if row["clock_out"] and db.parse(row["clock_out"]) <= t:
                return False, "clock-in would be after that day's clock-out"
            c.execute("UPDATE attendance SET clock_in=?, note='corrected by phone request' WHERE id=?",
                      (t.isoformat(), row["id"]))
            return True, "clock-in changed"
        y, m, d = heb_fields(t)
        c.execute("INSERT INTO attendance(employee_id, clock_in, heb_year, heb_month, heb_day, note) "
                  "VALUES (?,?,?,?,?,'added by phone request')", (corr["employee_id"], t.isoformat(), y, m, d))
        return True, "night added (exit counts as 7:30 unless corrected)"
    candidates = [x for x in rows if db.parse(x["clock_in"]) < t]
    if not candidates:
        return False, "no clock-in that day before this time - approve a clock-in correction first"
    row = candidates[-1]
    c.execute("UPDATE attendance SET clock_out=?, exit_confirmed=1, note='corrected by phone request' WHERE id=?",
              (t.isoformat(), row["id"]))
    return True, "clock-out set"


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
