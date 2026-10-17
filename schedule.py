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
SHUTTLE_WINDOW = (time(0, 45), time(1, 25))        # key 3 (driver) works only in this window
SHUTTLE_WINDOW_BHZ = (time(1, 45), time(2, 25))    # bein hazmanim: the shuttle leaves at 2:00
BHZ_NOTICE_OTHERS_UNTIL = time(2, 30)   # in bein hazmanim, non-riders clocking in before this hear the notice
NEW_NIGHT_FROM_HOUR = 20             # a call from 20:00 belongs to the next morning's session
UMAN_NIGHTS_EACH_WAY = 3

# Meron
MERON_RECORDED_IN = time(2, 25)      # a Meron rider who clocks in before this is recorded as arriving at 2:25
MERON_RECORDED_IN_BHZ = time(3, 25)
MERON_BONUS = 35                     # per night travelled to Meron (clocked in within the shuttle window)
DRIVER_BONUS = 60                    # per night the driver confirmed the shuttle

# Monthly rules
PERCENT_BONUSES = [(70, 200), (80, 100)]   # attendance >= 70% -> +200, >= 80% -> another +100
MIN_WHOLE_NIGHTS = 2                 # below this OR below MIN_STIPEND (without Meron): no stipend this month
MIN_STIPEND = 500
REMOVE_AFTER_MONTHS = 2              # avrech with this many counted months in a row without stipend is removed
NOT_COUNTED_MONTHS = (1, 5, 7, 9)    # Nisan, Av, Tishrei, Kislev - neither count nor break the sequence


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


def regular_opens_at(emp, d):
    return datetime.combine(d, OPENS_RIDERS if is_rider(emp) else OPENS_OTHERS, tzinfo=db.TZ)


def bhz_notice_due(emp, dt):
    """In bein hazmanim, pressing 1 at the regular-night time plays the bein hazmanim notice.
    Riders: between 0:45 and 1:45 (and they cannot clock in yet). Others: 1:30 to 2:30 (and they can)."""
    d = session_date(dt)
    if not bhz_range_of(d):
        return False
    until = OPENS_RIDERS_BHZ if is_rider(emp) else BHZ_NOTICE_OTHERS_UNTIL
    return regular_opens_at(emp, d) <= dt < datetime.combine(d, until, tzinfo=db.TZ)


def shuttle_window(dt):
    d = session_date(dt)
    a, b = SHUTTLE_WINDOW_BHZ if bhz_range_of(d) else SHUTTLE_WINDOW
    return datetime.combine(d, a, tzinfo=db.TZ), datetime.combine(d, b, tzinfo=db.TZ)


def shuttle_open(dt):
    a, b = shuttle_window(dt)
    return a <= dt <= b


def night_open(emp, dt):
    d = session_date(dt)
    return in_night(emp) and opens_at(emp, d) <= dt <= window(d)[1]


def program_now(emp, dt):
    """Which kollel a key 1 / key 2 at `dt` belongs to: 'night', 'day' or None (closed)."""
    if in_day(emp) and day_open(dt):
        return "day"
    if night_open(emp, dt):
        return "night"
    return None


def clock_in_open(emp, dt):
    return program_now(emp, dt) is not None


def next_opening(emp, dt):
    """The next time key 1 opens for this person, in any of his kollels."""
    options = []
    if in_night(emp):
        d = session_date(dt)
        if dt > window(d)[1]:
            d += timedelta(days=1)
        options.append(opens_at(emp, d))
    if in_day(emp):
        d = dt.date()
        for _ in range(400):
            if day_kollel_on(d):
                o = datetime.combine(d, DAY_OPENS, tzinfo=db.TZ)
                if o >= dt or dt <= datetime.combine(d, DAY_END, tzinfo=db.TZ):
                    options.append(o)
                    break
            d += timedelta(days=1)
    return min(options) if options else dt


# ---- Kollel Horaah ------------------------------------------------------

DAY_START = time(16, 0)
DAY_END = time(19, 0)
DAY_OPENS = time(15, 0)                 # mincha at 15:40, the kollel from 16:00
DAY_MONTHLY_BASE = 800          # divided by the month's kollel hours
DAY_MONTHLY_KEEP_TIME = 200     # divided by the month's kollel days, for each day he came by 16:00 exactly
DAY_VACATION_MONTHS = (1, 7, 5)     # Nisan, Tishrei, Av: paid the average of the last 3 months
DAY_AV_LAST_DAY = 7                 # in Av there is kollel until 7 Av (attendance is recorded; the month is paid by average)
DAY_AVERAGE_OF = 3


def in_night(emp):
    v = _get(emp, "in_night")
    return True if v is None else bool(v)


def in_day(emp):
    return bool(_get(emp, "in_day"))


def day_kollel_on(d):
    """Is there a Kollel Horaah on date `d`? Sunday to Thursday, except vacations, Chanuka, Purim, fasts and so on."""
    if d.weekday() not in (6, 0, 1, 2, 3):
        return False
    y, m, dd = hebcal.from_gregorian(d)
    if m in (1, 7) or (m == 5 and dd > DAY_AV_LAST_DAY) or (m, dd) == (6, 1):   # Nisan, Tishrei, 8 Av - 1 Elul
        return False
    chanuka = hebcal.to_gregorian(y, 9, 25)
    if chanuka <= d <= chanuka + timedelta(days=7):
        return False
    adar = 13 if hebcal.is_leap(y) else 12
    esther = 13 if hebcal.to_gregorian(y, adar, 13).weekday() != 5 else 11
    tammuz17 = 17 if hebcal.to_gregorian(y, 4, 17).weekday() != 5 else 18
    off = {(adar, 14), (adar, 15), (adar, esther), (10, 10), (4, tammuz17),     # Purim, Shushan Purim, fasts
           (3, 5), (3, 6), (3, 7), (2, 18)}                                     # Shavuos eve, day, isru chag; Lag BaOmer
    return (m, dd) not in off


def day_window(d):
    return datetime.combine(d, DAY_START, tzinfo=db.TZ), datetime.combine(d, DAY_END, tzinfo=db.TZ)


def day_open(dt):
    d = dt.date()
    return day_kollel_on(d) and datetime.combine(d, DAY_OPENS, tzinfo=db.TZ) <= dt <= day_window(d)[1]


def day_dates(hy, hm):
    first, last = hebcal.month_range(hy, hm)
    return [first + timedelta(days=i) for i in range((last - first).days + 1) if day_kollel_on(first + timedelta(days=i))]


def day_average(emp, hy, hm):
    """Average Kollel Horaah stipend of the last 3 regular months before (hy, hm)."""
    prev, y, m = [], hy, hm
    while len(prev) < DAY_AVERAGE_OF:
        y, m = hebcal.prev_month(y, m)
        if m not in DAY_VACATION_MONTHS:
            prev.append(day_month_summary(emp, y, m)["total"])
    return round(sum(prev) / DAY_AVERAGE_OF, 2)


def _would_be_av_days(hy):
    """Av as if it were a regular month: Sunday to Thursday, without Tisha B'Av."""
    first, last = hebcal.month_range(hy, 5)
    av9 = 10 if hebcal.to_gregorian(hy, 5, 9).weekday() == 5 else 9
    out = []
    d = first
    while d <= last:
        if d.weekday() in (6, 0, 1, 2, 3) and hebcal.from_gregorian(d)[2] != av9:
            out.append(d)
        d += timedelta(days=1)
    return out


def day_month_summary(emp, hy, hm, until=None):
    """Kollel Horaah stipend for one Hebrew month (rosh chodesh to rosh chodesh).
    Nisan, Tishrei: the 3-month average. Av: 1-7 Av by attendance, the rest of the month by the average."""
    if hm in (1, 7):
        avg = day_average(emp, hy, hm)
        return {"vacation": True, "days": 0, "held": 0, "kept": 0, "hours": 0.0, "percent": 0.0, "base": 0,
                "keep": 0, "average": avg, "total": avg}
    days = day_dates(hy, hm)
    if hm == 5:
        rate_days = _would_be_av_days(hy)
        vac_share = (len(rate_days) - len(days)) / len(rate_days) if rate_days else 0
    else:
        rate_days, vac_share = days, 0
    empty = {"vacation": False, "days": 0, "held": 0, "kept": 0, "hours": 0.0, "percent": 0.0, "base": 0, "keep": 0,
             "average": 0, "total": 0}
    if not rate_days:
        return empty
    per_minute = DAY_MONTHLY_BASE / (len(rate_days) * 180)
    per_day_keep = DAY_MONTHLY_KEEP_TIME / len(rate_days)
    upto = [d for d in days if until is None or d <= until]
    mine = nights_for(emp["id"], days[0], days[-1], program="day") if days else {}
    minutes = kept = attended = 0
    for d in upto:
        recs = mine.get(d, [])
        if not recs:
            continue
        attended += 1
        start, end = day_window(d)
        for r in recs:
            cin = db.parse(r["clock_in"])
            cout = db.parse(r["clock_out"]) if r["clock_out"] else end
            lo, hi = max(cin, start), min(cout, end)
            if hi > lo:
                minutes += (hi - lo).total_seconds() / 60
        if min(db.parse(r["clock_in"]) for r in recs) <= start:          # Kollel Horaah: exactly 16:00
            kept += 1
    held_minutes = len(upto) * 180
    base = round(minutes * per_minute, 2)
    keep = round(kept * per_day_keep, 2)
    average = round(day_average(emp, hy, hm) * vac_share, 2) if vac_share else 0
    return {"vacation": False, "days": attended, "held": len(upto), "kept": kept, "hours": round(minutes / 60, 2),
            "percent": (minutes / held_minutes) if held_minutes else 0.0, "base": base, "keep": keep,
            "average": average, "total": round(base + keep + average, 2)}

def meron_recorded_in(d):
    t = MERON_RECORDED_IN_BHZ if bhz_range_of(d) else MERON_RECORDED_IN
    return datetime.combine(d, t, tzinfo=db.TZ)


def recorded_clock_in(emp, t):
    """(time to record, is_meron) for someone clocking in at `t`.
    Meron riders who call before 2:25 (3:25) are recorded at 2:25; within the shuttle window it is a Meron night."""
    if not is_rider(emp):
        return t, False
    d = session_date(t)
    at = meron_recorded_in(d)
    return (at if t < at else t), shuttle_open(t)


def default_exit(d, program="night"):
    return day_window(d)[1] if program == "day" else window(d)[1]


# ---- stipend -------------------------------------------------------------

def nightly_base(emp):
    rate = db.rate_for(emp)
    return None if rate is None else rate * REGULAR_HOURS


def nightly_max(emp):
    base = nightly_base(emp)
    return None if base is None else base + ON_TIME_BONUS + KEEP_TIME_BONUS


WHOLE_NIGHT_EXIT_GRACE = 10   # leaving from 7:20 still counts as a whole night
NO_DEDUCT_EXIT_GRACE = 5      # leaving from 7:25: the missing minutes are not deducted


def present_minutes(records, d):
    """Minutes present inside the kollel hours (2:30-7:30, bein hazmanim 3:30-7:30), and the session length."""
    start, end = window(d)
    session_min = (end - start).total_seconds() / 60
    present = 0.0
    for r in records:
        cin = db.parse(r["clock_in"])
        cout = db.parse(r["clock_out"]) if r["clock_out"] else end
        if end - timedelta(minutes=NO_DEDUCT_EXIT_GRACE) <= cout < end:
            cout = end
        lo, hi = max(cin, start), min(cout, end)
        if hi > lo:
            present += (hi - lo).total_seconds() / 60
    return min(present, session_min), session_min


def night_pay(emp, records, d):
    """Pay for one night from that night's attendance records. Returns dict or None if no rate."""
    base_full = nightly_base(emp)
    if base_full is None or not records:
        return None
    start, end = window(d)
    present, session_min = present_minutes(records, d)
    first_in = min(db.parse(r["clock_in"]) for r in records)
    bonus = 0
    if first_in < start:
        bonus = ON_TIME_BONUS + KEEP_TIME_BONUS
    elif first_in < start + timedelta(minutes=LATE_GRACE_MINUTES + 1):
        bonus = KEEP_TIME_BONUS
    base = round(base_full * present / session_min, 2)
    last_out = max((db.parse(r["clock_out"]) if r["clock_out"] else end) for r in records)
    meron = any(_get(r, "meron") for r in records)
    return {"date": d, "minutes": round(present), "session": round(session_min), "base": base, "bonus": bonus,
            "total": round(base + bonus, 2), "bhz": bool(bhz_range_of(d)),
            "whole": bonus >= KEEP_TIME_BONUS and last_out >= end - timedelta(minutes=WHOLE_NIGHT_EXIT_GRACE), "early": first_in < start,
            "meron": meron, "meron_pay": MERON_BONUS if meron else 0}


def _get(row, key):
    try:
        return row[key]
    except (IndexError, KeyError):
        return None


def nights_for(emp_id, first, last, program="night"):
    """{session_date: [records]} for one employee between two dates (inclusive), one program."""
    with db.connect() as c:
        rows = c.execute("SELECT * FROM attendance WHERE employee_id=? AND program=? ORDER BY clock_in",
                         (emp_id, program)).fetchall()
    out = {}
    for r in rows:
        d = session_date(db.parse(r["clock_in"]))
        if first <= d <= last:
            out.setdefault(d, []).append(r)
    return out


def kollel_nights(first, last):
    """Nights the kollel was held = nights anybody clocked in."""
    with db.connect() as c:
        rows = c.execute("SELECT clock_in FROM attendance WHERE program='night'").fetchall()
    return sorted({d for d in (session_date(db.parse(r["clock_in"])) for r in rows) if first <= d <= last})


# ---- Uman for Rosh Hashana -----------------------------------------------

def attendance_percent(emp, months, until=None):
    """Attendance % in time (kollel hours present / kollel hours held), over Hebrew months [(year, month)].
    The +20 / +10 and Meron are not part of it."""
    present = possible = 0.0
    for y, m in months:
        first, last = night_month_range(y, m)
        if until:
            last = min(last, until)
        mine = nights_for(emp["id"], first, last)
        for d in kollel_nights(first, last):
            p, length = present_minutes(mine.get(d, []), d)
            present += p
            possible += length
    return 0.0 if not possible else min(present / possible, 1.0)


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


# ---- the month -------------------------------------------------------------

def driver_nights(emp_id, first, last):
    with db.connect() as c:
        rows = c.execute("SELECT created_at FROM shuttle_runs WHERE driver_id=? AND status='sent'", (emp_id,)).fetchall()
    return sorted({d for d in (session_date(db.parse(r["created_at"])) for r in rows) if first <= d <= last})


def month_summary(emp, hy, hm, until=None):
    """Everything about one employee's stipend in one night month (27th to 26th; up to `until`)."""
    first, last = night_month_range(hy, hm)
    if until:
        last = min(last, until)
    mine = nights_for(emp["id"], first, last)
    nights = [n for n in (night_pay(emp, recs, d) for d, recs in sorted(mine.items())) if n]
    held = kollel_nights(first, last)
    present = sum(present_minutes(mine.get(d, []), d)[0] for d in held)
    possible = sum(present_minutes([], d)[1] for d in held)
    pct = 0.0 if not possible else min(present / possible, 1.0)
    with db.connect() as c:
        extras = c.execute("SELECT COALESCE(SUM(amount),0) FROM bonuses WHERE employee_id=? AND heb_year=? AND heb_month=?",
                           (emp["id"], hy, hm)).fetchone()[0]
    nightly = round(sum(n["total"] for n in nights), 2)
    base = round(sum(n["base"] for n in nights), 2)
    timing = sum(n["bonus"] for n in nights)
    percent_bonus = sum(amount for at, amount in PERCENT_BONUSES if pct * 100 >= at)
    whole = sum(1 for n in nights if n["whole"])
    stipend = round(nightly + percent_bonus + extras, 2)          # without Meron
    eligible = whole >= MIN_WHOLE_NIGHTS and stipend >= MIN_STIPEND
    meron_nights = sum(1 for n in nights if n["meron"])
    meron_nightly = round(sum(n["total"] for n in nights if n["meron"]), 2)
    meron = meron_nights * MERON_BONUS
    drives = len(driver_nights(emp["id"], first, last))
    driver = drives * DRIVER_BONUS
    rate_ok = nightly_base(emp) is not None
    return {
        "nights": len(nights), "whole": whole, "early": sum(1 for n in nights if n["early"]),
        "percent": pct, "present_min": present, "possible_min": possible, "held": len(held),
        "base": base, "timing": timing, "percent_bonus": percent_bonus, "extras": round(extras, 2),
        "stipend": stipend, "eligible": eligible, "meron_nights": meron_nights, "meron": meron,
        "drives": drives, "driver": driver, "rate_ok": rate_ok, "meron_nightly": meron_nightly,
        "total": round((stipend if eligible else 0) + meron + driver, 2) if rate_ok else None,
    }


# Mornings with no kollel, for forecasting the rest of the month: Shabbat, Yom Tov, Tisha B'Av
NO_KOLLEL_HEBREW = [(7, 1), (7, 2), (7, 10), (7, 15), (7, 22), (1, 15), (1, 21), (3, 6)]


def expected_kollel(d):
    if d.weekday() == 5:
        return False
    y, m, dd = hebcal.from_gregorian(d)
    if (m, dd) in NO_KOLLEL_HEBREW:
        return False
    av9 = 10 if hebcal.to_gregorian(y, 5, 9).weekday() == 5 else 9
    return (m, dd) != (5, av9)


def month_forecast(emp, hy, hm, now):
    """What is still needed by the end of the month, assuming a kollel every expected night still ahead.
    Returns dict: remaining nights, and for each target (70, 80, 'min') the whole nights still needed
    (None = no longer possible)."""
    first, last = night_month_range(hy, hm)
    today = session_date(now)
    tonight_done = now > window(today)[0] or bool(nights_for(emp["id"], today, today))
    next_night = today + timedelta(days=1) if tonight_done else today
    so_far = month_summary(emp, hy, hm, until=next_night - timedelta(days=1))
    remaining = [next_night + timedelta(days=i) for i in range((last - next_night).days + 1)]
    remaining = [d for d in remaining if expected_kollel(d)]
    lengths = [present_minutes([], d)[1] for d in remaining]
    total_possible = so_far["possible_min"] + sum(lengths)
    out = {"remaining": len(remaining)}
    for at, _amount in PERCENT_BONUSES:
        need = at / 100 * total_possible - so_far["present_min"]
        out[at] = _nights_for_minutes(need, lengths)
    mx = nightly_max(emp) or 1
    need_whole = max(MIN_WHOLE_NIGHTS - so_far["whole"], -(-max(0, MIN_STIPEND - so_far["stipend"]) // mx))
    out["min"] = int(need_whole) if need_whole <= len(remaining) else None
    return out


def _nights_for_minutes(need, lengths):
    if need <= 0:
        return 0
    got = 0
    for i, length in enumerate(sorted(lengths, reverse=True), 1):
        got += length
        if got >= need:
            return i
    return None


# Who pays what: Meron (35 a night + 60 for driving) -> Jerusalem; the 70/80% bonuses -> Ohr Hatzadik;
# everything else (all the nights' stipend, Meron nights too, Uman, manual extras) -> half each.
SPLIT_SHARE = 0.5


def payer_split(s):
    """For one month_summary: kollel part, Meron part, and what each donor pays."""
    if s["total"] is None:
        return None
    if s["eligible"]:
        shared = round(s["stipend"] - s["percent_bonus"], 2)
        bonus = s["percent_bonus"]
    else:
        shared, bonus = 0.0, 0
    meron = round(s["meron"] + s["driver"], 2)
    return {
        "kollel": round(shared + bonus, 2),
        "meron": meron,
        "total": round(shared + bonus + meron, 2),
        "jerusalem": round(shared * SPLIT_SHARE + meron, 2),
        "ohr_hatzadik": round(shared * (1 - SPLIT_SHARE) + bonus, 2),
    }


# The Kollel Chatzos stipend month: from Rosh Chodesh (the 30th of the month before, when there is one) to the 29th.
# Once only: Cheshvan 5787 started on 27 Tishrei (the stipend was paid up to 26 Tishrei).
NIGHT_MONTH_LAST_DAY = 29
NIGHT_MONTH_SPECIAL_START = {(5787, 8): (5787, 7, 27)}


def night_month_range(hy, hm):
    """Gregorian (first, last) session dates of night month (hy, hm)."""
    py, pm = hebcal.prev_month(hy, hm)
    if (hy, hm) in NIGHT_MONTH_SPECIAL_START:
        first = hebcal.to_gregorian(*NIGHT_MONTH_SPECIAL_START[(hy, hm)])
    elif hebcal.month_length(pm, py) == 30:
        first = hebcal.to_gregorian(py, pm, 30)
    else:
        first = hebcal.to_gregorian(hy, hm, 1)
    nxt = hebcal.next_month(hy, hm)
    if nxt in NIGHT_MONTH_SPECIAL_START:
        last = hebcal.to_gregorian(*NIGHT_MONTH_SPECIAL_START[nxt]) - timedelta(days=1)
    else:
        last = hebcal.to_gregorian(hy, hm, NIGHT_MONTH_LAST_DAY)
    return first, last


def night_month_of(d):
    """(hy, hm) of the night month that session date `d` belongs to."""
    y, m, dd = hebcal.from_gregorian(d)
    nxt = hebcal.next_month(y, m)
    if dd > NIGHT_MONTH_LAST_DAY:
        return nxt
    if nxt in NIGHT_MONTH_SPECIAL_START and d >= hebcal.to_gregorian(*NIGHT_MONTH_SPECIAL_START[nxt]):
        return nxt
    return y, m


def counted_months_before(hy, hm, n):
    """The n Hebrew months before (hy, hm) that count for removal (skipping Nisan, Av, Tishrei, Kislev)."""
    out = []
    y, m = hy, hm
    while len(out) < n:
        y, m = hebcal.prev_month(y, m)
        if m not in NOT_COUNTED_MONTHS:
            out.append((y, m))
    return out


def removed_by_rule(emp, today):
    """An avrech (not a manager) with REMOVE_AFTER_MONTHS counted months in a row without stipend,
    after the date he joined or was last restored."""
    if emp["role"] != "employee" or not in_night(emp):
        return False
    since = _get(emp, "restored_on") or _get(emp, "joined_on")
    since = date.fromisoformat(since[:10]) if since else None
    hy, hm = night_month_of(today)
    for y, m in counted_months_before(hy, hm, REMOVE_AFTER_MONTHS):
        first, _last = night_month_range(y, m)
        if since is None or first < since:
            return False
        if month_summary(emp, y, m)["eligible"]:
            return False
    return True


def uman_question_due(today):
    """From after Rosh Hashana until the end of Tishrei. Returns the Hebrew year or None."""
    y, m, dday = hebcal.from_gregorian(today)
    return y if m == 7 and dday >= 3 else None
