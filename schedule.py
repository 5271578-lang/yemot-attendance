"""Kollel hours, bein hazmanim dates, and the nightly stipend.

Times and amounts are here so they are easy to change.
"""
from datetime import date, datetime, time, timedelta

import db
import hebcal

REGULAR_START = time(2, 30)
BHZ_START = time(3, 30)              # bein hazmanim
END = time(7, 30)
REGULAR_HOURS = 5                    # 2:30-7:30: the hourly rate x this = the nightly base (16 x 5 = 80)
ON_TIME_BONUS = 10                   # clocked in before the start (until x:29)
KEEP_TIME_BONUS = 20                 # clocked in up to 10 minutes after the start (also given when on time)
LATE_GRACE_MINUTES = 10
# When keys 1 and 2 open (until the end of the session)
OPENS_RIDERS = time(0, 45)           # in a shuttle group (Meron)
OPENS_RIDERS_BHZ = time(1, 45)
OPENS_OTHERS = time(1, 30)           # everyone else, also in bein hazmanim
NEW_NIGHT_FROM_HOUR = 20             # a call from 20:00 belongs to the next morning's session
UMAN_NIGHTS_EACH_WAY = 3


# ---- which night ---------------------------------------------------------

def session_date(dt):
    return dt.date() + timedelta(days=1) if dt.hour >= NEW_NIGHT_FROM_HOUR else dt.date()


def bhz_ranges(heb_year):
    """Bein hazmanim periods of a Hebrew year, as (first_date, last_date) of session mornings."""
    g = hebcal.to_gregorian
    adar = 13 if hebcal.is_leap(heb_year) else 12
    av9_shabbat = g(heb_year, 5, 9).weekday() == 5
    chanuka = g(heb_year, 9, 25)
    return [
        (g(heb_year, 7, 11), g(heb_year, 7, 23)),           # motzaei Yom Kippur - isru chag Sukkos
        (chanuka, chanuka + timedelta(days=7)),             # all nights of Chanuka
        (g(heb_year, adar, 15), g(heb_year, adar, 16)),     # Shushan Purim and the day after
        (g(heb_year, 1, 10), g(heb_year, 1, 22)),           # 10 Nisan - isru chag Pesach
        (g(heb_year, 3, 7), g(heb_year, 3, 7)),             # isru chag Shavuos
        (g(heb_year, 5, 11 if av9_shabbat else 10),) * 2,   # motzaei Tisha B'Av
    ]


def bhz_range_of(d):
    y = hebcal.from_gregorian(d)[0]
    for first, last in bhz_ranges(y):
        if first <= d <= last:
            return first, last
    return None


def window(d):
    """(start, end) datetimes of the session on morning `d`."""
    start = BHZ_START if bhz_range_of(d) else REGULAR_START
    return (datetime.combine(d, start, tzinfo=db.TZ), datetime.combine(d, END, tzinfo=db.TZ))


def is_rider(emp):
    return bool(emp["shuttle_group"])


def opens_at(emp, d):
    if is_rider(emp):
        t = OPENS_RIDERS_BHZ if bhz_range_of(d) else OPENS_RIDERS
    else:
        t = OPENS_OTHERS
    return datetime.combine(d, t, tzinfo=db.TZ)


def clock_in_open(emp, dt):
    d = session_date(dt)
    return opens_at(emp, d) <= dt <= window(d)[1]


def next_opening(emp, dt):
    d = session_date(dt)
    if dt > window(d)[1]:
        d += timedelta(days=1)
    return opens_at(emp, d)


def default_exit(d):
    return window(d)[1]


# ---- stipend -------------------------------------------------------------

def nightly_base(emp):
    rate = db.rate_for(emp)
    return None if rate is None else rate * REGULAR_HOURS


def nightly_max(emp):
    base = nightly_base(emp)
    return None if base is None else base + ON_TIME_BONUS + KEEP_TIME_BONUS


def night_pay(emp, records, d):
    """Pay for one night from that night's attendance records. Returns dict or None if no rate."""
    base_full = nightly_base(emp)
    if base_full is None or not records:
        return None
    start, end = window(d)
    session_min = (end - start).total_seconds() / 60
    present = 0.0
    for r in records:
        cin = db.parse(r["clock_in"])
        cout = db.parse(r["clock_out"]) if r["clock_out"] else end
        lo, hi = max(cin, start), min(cout, end)
        if hi > lo:
            present += (hi - lo).total_seconds() / 60
    present = min(present, session_min)
    first_in = min(db.parse(r["clock_in"]) for r in records)
    bonus = 0
    if first_in < start:
        bonus = ON_TIME_BONUS + KEEP_TIME_BONUS
    elif first_in < start + timedelta(minutes=LATE_GRACE_MINUTES + 1):
        bonus = KEEP_TIME_BONUS
    base = round(base_full * present / session_min, 2)
    return {"date": d, "minutes": round(present), "base": base, "bonus": bonus, "total": round(base + bonus, 2),
            "bhz": bool(bhz_range_of(d))}


def nights_for(emp_id, first, last):
    """{session_date: [records]} for one employee between two dates (inclusive)."""
    with db.connect() as c:
        rows = c.execute("SELECT * FROM attendance WHERE employee_id=? ORDER BY clock_in", (emp_id,)).fetchall()
    out = {}
    for r in rows:
        d = session_date(db.parse(r["clock_in"]))
        if first <= d <= last:
            out.setdefault(d, []).append(r)
    return out


def kollel_nights(first, last):
    """Nights the kollel was held = nights anybody clocked in."""
    with db.connect() as c:
        rows = c.execute("SELECT clock_in FROM attendance").fetchall()
    return sorted({d for d in (session_date(db.parse(r["clock_in"])) for r in rows) if first <= d <= last})


# ---- Uman for Rosh Hashana -----------------------------------------------

def attendance_percent(emp, months):
    """Weighted attendance % over Hebrew months [(year, month), ...] = earned / max possible."""
    mx = nightly_max(emp)
    if not mx:
        return 0.0
    earned = possible = 0.0
    for y, m in months:
        first, last = hebcal.month_range(y, m)
        held = kollel_nights(first, last)
        mine = nights_for(emp["id"], first, last)
        possible += mx * len(held)
        earned += sum((night_pay(emp, recs, d) or {"total": 0})["total"] for d, recs in mine.items())
    return 0.0 if not possible else min(earned / possible, 1.0)


def uman_credit(emp, heb_year, today):
    """Nights credited for travelling to Uman for Rosh Hashana of `heb_year`.
    Returns (percent, [(date, amount), ...])."""
    pct = attendance_percent(emp, [(heb_year - 1, 4), (heb_year - 1, 6)])     # Tamuz and Elul before
    mx = nightly_max(emp) or 0
    amount = round(pct * mx, 2)
    rh = hebcal.to_gregorian(heb_year, 7, 1)
    mine = set(nights_for(emp["id"], rh - timedelta(days=60), today))
    before = [d for d in reversed(kollel_nights(rh - timedelta(days=30), rh - timedelta(days=1))) if d not in mine]
    after = [d for d in kollel_nights(rh + timedelta(days=2), today) if d not in mine]
    nights = sorted(before[:UMAN_NIGHTS_EACH_WAY] + after[:UMAN_NIGHTS_EACH_WAY])
    return pct, [(d, amount) for d in nights]


def uman_question_due(today):
    """From after Rosh Hashana until the end of Tishrei. Returns the Hebrew year or None."""
    y, m, dday = hebcal.from_gregorian(today)
    return y if m == 7 and dday >= 3 else None
