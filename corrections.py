"""Approving the attendance corrections avrechim ask for on key 5 - from the admin page or by phone."""
from datetime import timedelta

import db
import hebcal
import schedule


def program_of(dt):
    return "day" if 14 <= dt.hour < 20 else "night"


def meron_rule(c, emp_id, t):
    """(time to record, meron 0/1) for a night clock-in at `t`: riders are recorded at 2:25 (3:25),
    and inside the shuttle window it is a Meron night. Others: the time as is."""
    if program_of(t) != "night":
        return t, 0
    emp = c.execute("SELECT * FROM employees WHERE id=?", (emp_id,)).fetchone()
    rec, is_meron = schedule.recorded_clock_in(emp, t)
    return rec, 1 if is_meron else 0


def apply_correction(c, corr):
    """Returns (ok, explanation)."""
    t = db.parse(corr["requested_time"])
    day_start = t.replace(hour=0, minute=0, second=0)
    day_end = day_start + timedelta(days=1)
    program = program_of(t)
    rows = c.execute("SELECT * FROM attendance WHERE employee_id=? AND program=? AND clock_in>=? AND clock_in<? "
                     "ORDER BY clock_in", (corr["employee_id"], program, day_start.isoformat(), day_end.isoformat())).fetchall()
    if corr["kind"] == "in":
        asked = t
        meron = 0
        t, meron = meron_rule(c, corr["employee_id"], t)    # same rule as calling the line
        if rows:
            row = rows[0]
            if row["clock_out"] and db.parse(row["clock_out"]) <= t:
                return False, "clock-in would be after that day's clock-out"
            c.execute("UPDATE attendance SET clock_in=?, actual_in=?, meron=MAX(meron, ?), note='corrected by phone request' "
                      "WHERE id=?", (t.isoformat(), asked.isoformat(), meron, row["id"]))
            return True, "clock-in changed" + (" (Meron)" if meron else "")
        y, m, d = hebcal.from_gregorian(schedule.session_date(t))
        c.execute("INSERT INTO attendance(employee_id, clock_in, actual_in, meron, heb_year, heb_month, heb_day, note, program) "
                  "VALUES (?,?,?,?,?,?,?,'added by phone request',?)",
                  (corr["employee_id"], t.isoformat(), asked.isoformat(), meron, y, m, d, program))
        return True, "night added (exit counts as 7:30 unless corrected)" + (" (Meron)" if meron else "")
    candidates = [x for x in rows if db.parse(x["clock_in"]) < t]
    if not candidates:
        return False, "no clock-in that day before this time - approve a clock-in correction first"
    row = candidates[-1]
    c.execute("UPDATE attendance SET clock_out=?, exit_confirmed=1, note='corrected by phone request' WHERE id=?",
              (t.isoformat(), row["id"]))
    return True, "clock-out set"


# ---- changes a manager makes himself (voice actions, 9 -> 9 -> 1) ---------------------

def manager_change(emp_id, kind, t):
    """Set a clock-in ('in') or an exit ('out') at `t`, by the same rules as an approved correction."""
    with db.connect() as c:
        return apply_correction(c, {"employee_id": emp_id, "kind": kind, "requested_time": t.isoformat()})


def whole_night(emp_id, d):
    """A whole night on morning `d`: in at the kollel start, out at its end."""
    a, b = schedule.window(d)
    ok, msg = manager_change(emp_id, "in", a)
    if not ok:
        return ok, msg
    return manager_change(emp_id, "out", b)


def _month_rows(c, emp_id, first, last):
    """His Kollel Chatzos records whose night is in [first, last]."""
    rows = c.execute("SELECT * FROM attendance WHERE employee_id=? AND program='night' AND clock_in>=? AND clock_in<?",
                     (emp_id, (first - timedelta(days=1)).isoformat(), (last + timedelta(days=2)).isoformat())).fetchall()
    return [r for r in rows if first <= schedule.session_date(db.parse(r["clock_in"])) <= last]


def full_month(emp_id, hy, hm, until=None, meron=False):
    """A whole night on every night the kollel was held in Kollel Chatzos month (hy, hm), up to `until`
    (default: today). Returns how many nights were set. What was there before is kept, so it can be undone."""
    import json
    first, last = schedule.night_month_range(hy, hm)
    with db.connect() as c:
        before = [dict(r) for r in _month_rows(c, emp_id, first, last)]
        c.execute("INSERT INTO fill_undo(employee_id, heb_year, heb_month, created_at, rows_json) VALUES (?,?,?,?,?)",
                  (emp_id, hy, hm, db.now().isoformat(), json.dumps(before, ensure_ascii=False)))
    last = min(last, until or db.now().date())
    n = 0
    for d in schedule.kollel_nights(first, last):
        if whole_night(emp_id, d)[0]:
            n += 1
            if meron:
                set_meron(emp_id, d)
    return n


def delete_nights(c, emp_id, rows):
    """Delete these attendance records, keeping a copy so the deletion can be undone."""
    import json
    rows = [dict(r) for r in rows]
    if not rows:
        return 0
    c.execute("INSERT INTO attendance_trash(employee_id, deleted_at, rows_json) VALUES (?,?,?)",
              (emp_id, db.now().isoformat(), json.dumps(rows, ensure_ascii=False)))
    c.executemany("DELETE FROM attendance WHERE id=?", [(r["id"],) for r in rows])
    return len(rows)


def last_deletion(emp_id):
    with db.connect() as c:
        return c.execute("SELECT * FROM attendance_trash WHERE employee_id=? AND restored_at IS NULL ORDER BY id DESC LIMIT 1",
                         (emp_id,)).fetchone()


def undo_deletion(emp_id):
    """Put back the nights of his last deletion. Returns how many."""
    import json
    t = last_deletion(emp_id)
    if t is None:
        return 0
    n = 0
    with db.connect() as c:
        for r in json.loads(t["rows_json"]):
            if c.execute("SELECT 1 FROM attendance WHERE id=?", (r["id"],)).fetchone():
                continue
            cols = list(r)
            c.execute(f"INSERT INTO attendance({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", [r[k] for k in cols])
            n += 1
        c.execute("UPDATE attendance_trash SET restored_at=? WHERE id=?", (db.now().isoformat(), t["id"]))
    return n


KEEP_DAYS_AFTER_REPORT = 7


def _keep_until(rows, made):
    """A week after the summary of the latest month these records belong to (Kollel Chatzos: the 29th;
    Kollel Horaah: Rosh Chodesh after the Hebrew month), and at least a week after the change itself."""
    until = made.date()
    for r in rows:
        d = schedule.session_date(db.parse(r["clock_in"]))
        if r.get("program") == "day":
            y, m, _ = hebcal.from_gregorian(d)
            end = hebcal.month_range(y, m)[1] + timedelta(days=1)
        else:
            end = schedule.night_month_range(*schedule.night_month_of(d))[1]
        until = max(until, end)
    return until + timedelta(days=KEEP_DAYS_AFTER_REPORT)


def purge_old(now=None):
    """Forget the saved copies (for undo) once they are past keeping; the records themselves stay as they are."""
    import json
    now = now or db.now()
    gone = 0
    with db.connect() as c:
        for t in c.execute("SELECT * FROM attendance_trash").fetchall():
            if now.date() > _keep_until(json.loads(t["rows_json"]), db.parse(t["deleted_at"])):
                c.execute("DELETE FROM attendance_trash WHERE id=?", (t["id"],))
                gone += 1
        for f in c.execute("SELECT * FROM fill_undo").fetchall():
            last = schedule.night_month_range(f["heb_year"], f["heb_month"])[1]
            if now.date() > max(last, db.parse(f["created_at"]).date()) + timedelta(days=KEEP_DAYS_AFTER_REPORT):
                c.execute("DELETE FROM fill_undo WHERE id=?", (f["id"],))
                gone += 1
    return gone


def last_fill(emp_id, hy, hm):
    with db.connect() as c:
        return c.execute("SELECT * FROM fill_undo WHERE employee_id=? AND heb_year=? AND heb_month=? AND undone_at IS NULL "
                         "ORDER BY id DESC LIMIT 1", (emp_id, hy, hm)).fetchone()


def undo_fill(emp_id, hy, hm):
    """Back to how the month was before the last full-month fill. True if done."""
    import json
    f = last_fill(emp_id, hy, hm)
    if f is None:
        return False
    first, last = schedule.night_month_range(hy, hm)
    rows = json.loads(f["rows_json"])
    with db.connect() as c:
        for r in _month_rows(c, emp_id, first, last):
            c.execute("DELETE FROM attendance WHERE id=?", (r["id"],))
        for r in rows:
            cols = list(r)
            c.execute(f"INSERT INTO attendance({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", [r[k] for k in cols])
        c.execute("UPDATE fill_undo SET undone_at=? WHERE id=?", (db.now().isoformat(), f["id"]))
    return True


def set_meron(emp_id, d, on=True):
    """Mark the night of morning `d` as a Meron night. False if he has no clock-in that night."""
    rows = schedule.nights_for(emp_id, d, d).get(d, [])
    if not rows:
        return False
    with db.connect() as c:
        c.executemany("UPDATE attendance SET meron=? WHERE id=?", [(1 if on else 0, r["id"]) for r in rows])
    return True


def decide(cid, approve):
    """Approve or reject one pending request. Returns (ok, explanation) or None if it is not pending."""
    with db.connect() as c:
        corr = c.execute("SELECT * FROM corrections WHERE id=? AND status='pending'", (cid,)).fetchone()
        if corr is None:
            return None
        if not approve:
            c.execute("UPDATE corrections SET status='rejected' WHERE id=?", (cid,))
            return True, "rejected"
        ok, msg = apply_correction(c, corr)
        if ok:
            c.execute("UPDATE corrections SET status='approved', result=? WHERE id=?", (msg, cid))
        return ok, msg
