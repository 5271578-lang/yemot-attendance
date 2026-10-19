"""Approving the attendance corrections avrechim ask for on key 5 - from the admin page or by phone."""
from datetime import timedelta

import db
import hebcal
import schedule


def program_of(dt):
    return "day" if 14 <= dt.hour < 20 else "night"


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
        if program == "night":                 # same rule as calling the line: riders are recorded at 2:25,
            emp = c.execute("SELECT * FROM employees WHERE id=?", (corr["employee_id"],)).fetchone()
            t, is_meron = schedule.recorded_clock_in(emp, t)        # and the shuttle window means Meron
            meron = 1 if is_meron else 0
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
