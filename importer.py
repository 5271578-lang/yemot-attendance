"""Import past attendance from the old system's export (CSV / TSV, any common encoding).

Expected columns (header names, any order): זהות, שם משפחה, שם פרטי, כניסה, יציאה
Dates: dd/mm/yy HH:MM[:SS]
"""
import csv
import io
import re
from datetime import datetime

import db
import hebcal
import schedule

COLUMNS = {"id": ("זהות", "ת.ז", "תז", "מספר זהות"), "last": ("שם משפחה",), "first": ("שם פרטי",),
           "in": ("כניסה",), "out": ("יציאה",), "name": ("שם",)}


def decode(raw):
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16")
    for enc in ("utf-8-sig", "cp1255"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            pass
    return raw.decode("utf-8", errors="replace")


def clean(v):
    v = (v or "").strip()
    m = re.fullmatch(r'="?(.*?)"?', v)
    return (m.group(1) if m else v).strip().strip('"')


def norm_id(v):
    digits = re.sub(r"\D", "", v or "")
    return digits.lstrip("0")


def norm_name(v):
    return re.sub(r"[\s'\"`׳״\-]+", "", v or "")


def parse_dt(v):
    v = clean(v)
    if not v:
        return None
    for fmt in ("%d/%m/%y %H:%M:%S", "%d/%m/%y %H:%M", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(v, fmt).replace(tzinfo=db.TZ)
        except ValueError:
            pass
    return None


def read_rows(raw):
    text = decode(raw)
    sample = text.split("\n", 1)[0]
    delim = "\t" if sample.count("\t") >= sample.count(",") else ","
    rows = list(csv.reader(io.StringIO(text), delimiter=delim))
    if not rows:
        return [], "empty file"
    header = [clean(h) for h in rows[0]]
    idx = {}
    for key, names in COLUMNS.items():
        for i, h in enumerate(header):
            if h in names:
                idx[key] = i
                break
    if "in" not in idx:
        return [], "no 'כניסה' column found"
    out = []
    for r in rows[1:]:
        if not any(c.strip() for c in r):
            continue
        get = lambda k: clean(r[idx[k]]) if k in idx and idx[k] < len(r) else ""
        first, last = get("first"), get("last")
        out.append({"id": norm_id(get("id")), "name": " ".join(x for x in (first, last) if x) or get("name"),
                    "first": first, "last": last, "in": parse_dt(get("in")), "out": parse_dt(get("out"))})
    return out, None


def match(rows):
    """Adds 'emp' (or None) and 'status' to every row."""
    with db.connect() as c:
        emps = c.execute("SELECT * FROM employees").fetchall()
    by_id = {norm_id(e["id_number"]): e for e in emps if e["id_number"]}
    by_name = {}
    for e in emps:
        by_name.setdefault(norm_name(e["name"]), []).append(e)
    for r in rows:
        e = by_id.get(r["id"]) if r["id"] else None
        if e is None:
            for key in (norm_name(r["first"] + r["last"]), norm_name(r["last"] + r["first"]), norm_name(r["name"])):
                hits = by_name.get(key) or []
                if len(hits) == 1:
                    e = hits[0]
                    break
        r["emp"] = e
        if r["in"] is None:
            r["status"] = "bad date"
        elif e is None:
            r["status"] = "no match"
        elif already_there(e["id"], r["in"]):
            r["status"] = "exists"
        else:
            r["status"] = "new"
    return rows


def already_there(emp_id, cin):
    d = schedule.session_date(cin)
    return bool(schedule.nights_for(emp_id, d, d))


def create_missing(rows):
    """Creates an employee for every unmatched name (with the ID number). Returns how many."""
    made = 0
    seen = set()
    for r in rows:
        if r["status"] != "no match" or not r["name"]:
            continue
        key = r["id"] or norm_name(r["name"])
        if key in seen:
            continue
        seen.add(key)
        with db.connect() as c:
            number = r["id"] or f"imp{c.execute('SELECT COUNT(*) FROM employees').fetchone()[0] + 1}"
            exists = c.execute("SELECT 1 FROM employees WHERE emp_number=?", (number,)).fetchone()
            if exists:
                continue
            c.execute("INSERT INTO employees(emp_number, name, pin, role, id_number, joined_on) VALUES (?,?,?,?,?,?)",
                      (number, r["name"], "1480", "employee", r["id"] or None, db.now().date().isoformat()))
        made += 1
    return made


def commit(rows):
    added = 0
    with db.connect() as c:
        for r in rows:
            if r["status"] != "new":
                continue
            y, m, d = hebcal.from_gregorian(schedule.session_date(r["in"]))
            c.execute("INSERT INTO attendance(employee_id, clock_in, clock_out, heb_year, heb_month, heb_day, "
                      "exit_confirmed, note) VALUES (?,?,?,?,?,?,1,'imported')",
                      (r["emp"]["id"], r["in"].isoformat(), r["out"].isoformat() if r["out"] else None, y, m, d))
            added += 1
    return added
