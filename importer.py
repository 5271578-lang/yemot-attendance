"""Import past attendance from the old system's export: Excel (.xlsx, every sheet) or CSV / TSV.

Expected columns (header names, any order): זהות, שם משפחה, שם פרטי, כניסה, יציאה
The header row may be below a few title rows. A sheet with no name / ID column is matched by
the name or ID written above the header, or by the sheet's title.
Dates: dd/mm/yy HH:MM[:SS] (or real Excel dates). A separate תאריך column is combined with time-only cells.
"""
import csv
import io
import re
from datetime import datetime, date, time

import db
import hebcal
import schedule

COLUMNS = {"id": ("זהות", "ת.ז", "תז", "ת.ז.", "מספר זהות", "תעודת זהות"), "last": ("שם משפחה",),
           "first": ("שם פרטי",), "in": ("כניסה", "שעת כניסה", "זמן כניסה"),
           "out": ("יציאה", "שעת יציאה", "זמן יציאה"), "name": ("שם", "שם מלא", "שם האברך"),
           "date": ("תאריך",)}


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
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    if not isinstance(v, str):
        return v if isinstance(v, (datetime, date, time)) else str(v)
    v = v.strip()
    m = re.fullmatch(r'="?(.*?)"?', v)
    return (m.group(1) if m else v).strip().strip('"')


def norm_id(v):
    digits = re.sub(r"\D", "", str(v or ""))
    return digits.lstrip("0")


def norm_name(v):
    v = re.sub(r"(^|\s)(הרב|הר\"ר|ר'|רבי)(?=\s|$)", " ", v or "")
    return re.sub(r"[\s'\"`׳״\-]+", "", v)


def parse_dt(v, day=None):
    v = clean(v)
    if v in ("", None):
        return None
    if isinstance(v, datetime):
        dt = v
    elif isinstance(v, time):
        if day is None:
            return None
        dt = datetime.combine(day, v)
    elif isinstance(v, date):
        return None
    else:
        dt = None
        for fmt in ("%d/%m/%y %H:%M:%S", "%d/%m/%y %H:%M", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M",
                    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%d.%m.%y %H:%M", "%d.%m.%Y %H:%M"):
            try:
                dt = datetime.strptime(v, fmt)
                break
            except ValueError:
                pass
        if dt is None and day is not None:
            for fmt in ("%H:%M:%S", "%H:%M"):
                try:
                    dt = datetime.combine(day, datetime.strptime(v, fmt).time())
                    break
                except ValueError:
                    pass
        if dt is None:
            return None
    return dt.replace(tzinfo=db.TZ, microsecond=0)


def parse_day(v):
    v = clean(v)
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    for fmt in ("%d/%m/%y", "%d/%m/%Y", "%Y-%m-%d", "%d.%m.%y", "%d.%m.%Y"):
        try:
            return datetime.strptime(str(v), fmt).date()
        except ValueError:
            pass
    return None


def program_of(dt):
    return "day" if 14 <= dt.hour < 20 else "night"


# ---- reading -----------------------------------------------------------------

def is_xlsx(raw):
    return raw[:2] == b"PK"


def tables(raw):
    """[(sheet_title, rows as lists of cell values)]"""
    if is_xlsx(raw):
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
        out = [(ws.title, [list(r) for r in ws.iter_rows(values_only=True)]) for ws in wb.worksheets]
        wb.close()
        return out
    text = decode(raw)
    sample = text.split("\n", 1)[0]
    delim = "\t" if sample.count("\t") >= sample.count(",") else ","
    return [("", list(csv.reader(io.StringIO(text), delimiter=delim)))]


def find_header(rows):
    for n, r in enumerate(rows[:15]):
        cells = [clean(c) for c in r]
        if any(isinstance(c, str) and c in COLUMNS["in"] for c in cells):
            return n
    return None


def sheet_owner(title, above):
    """Name / ID written above the header (or the sheet title) for one-avrech-per-sheet files."""
    ident, name = "", ""
    for r in above:
        cells = [clean(c) for c in r if clean(c) not in ("", None)]
        for i, c in enumerate(cells):
            c = str(c)
            label = c.rstrip(": ")
            nxt = str(cells[i + 1]) if i + 1 < len(cells) else ""
            if label in COLUMNS["id"] and nxt:
                ident = norm_id(nxt)
            elif label in COLUMNS["name"] + ("שם האברך",) and nxt:
                name = nxt
            elif ":" in c and c.split(":", 1)[0].strip() in COLUMNS["id"]:
                ident = norm_id(c.split(":", 1)[1])
            elif ":" in c and c.split(":", 1)[0].strip() in COLUMNS["name"]:
                name = c.split(":", 1)[1].strip()
    if not ident:
        digits = norm_id(title)
        if len(digits) >= 7:
            ident = digits
    if not name and title and not re.fullmatch(r"(Sheet|גיליון|גליון)\s*\d*", title.strip(), re.I):
        name = re.sub(r"[\d\-_]+", " ", title).strip()
    return ident, name


def read_rows(raw):
    out, errors = [], []
    for title, rows in tables(raw):
        if not any(any(clean(c) not in ("", None) for c in r) for r in rows):
            continue
        h = find_header(rows)
        if h is None:
            errors.append(f"sheet '{title}': no 'כניסה' column" if title else "no 'כניסה' column found")
            continue
        header = [clean(c) for c in rows[h]]
        idx = {}
        for key, names in COLUMNS.items():
            for i, c in enumerate(header):
                if isinstance(c, str) and c in names:
                    idx[key] = i
                    break
        owner_id, owner_name = ("", "")
        if not ({"id", "name", "first", "last"} & idx.keys()):
            owner_id, owner_name = sheet_owner(title, rows[:h])
        for r in rows[h + 1:]:
            if not any(clean(c) not in ("", None) for c in r):
                continue
            get = lambda k: clean(r[idx[k]]) if k in idx and idx[k] < len(r) else ""
            first, last = str(get("first")), str(get("last"))
            day = parse_day(get("date")) if "date" in idx else None
            cin, cout = parse_dt(get("in"), day), parse_dt(get("out"), day)
            if cin and cout and cout < cin and cout.date() == cin.date():
                cout = cout.replace(day=cin.day) + (datetime.combine(date(2000, 1, 2), time()) -
                                                    datetime.combine(date(2000, 1, 1), time()))
            name = " ".join(x for x in (first, last) if x) or str(get("name")) or owner_name
            if not name and not get("id") and cin is None and not get("in"):
                continue                                  # totals line etc.
            out.append({"sheet": title, "id": norm_id(get("id")) or owner_id, "name": name,
                        "first": first, "last": last, "in": cin, "out": cout,
                        "program": program_of(cin) if cin else None})
    if not out:
        return [], "; ".join(errors) or "empty file"
    return out, None


# ---- matching ------------------------------------------------------------------

def match(rows):
    """Adds 'emp' (or None) and 'status' to every row. The same record twice (two files) counts once."""
    seen = set()
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
                if key and len(hits) == 1:
                    e = hits[0]
                    break
        r["emp"] = e
        if r["in"] is None:
            r["status"] = "bad date"
        elif e is None:
            r["status"] = "no match"
        elif (e["id"], r["in"]) in seen:
            r["status"] = "duplicate"
        elif already_there(e["id"], r["in"]):
            r["status"] = "exists"
        else:
            r["status"] = "new"
        if e is not None and r["in"] is not None:
            seen.add((e["id"], r["in"]))
    return rows


def already_there(emp_id, cin):
    d = schedule.session_date(cin)
    return bool(schedule.nights_for(emp_id, d, d, program_of(cin)))


def warnings(rows):
    """Things the manager asked to be told about before importing."""
    out = []
    per_emp = {}
    for r in rows:
        if r["emp"] is not None and r["in"] is not None:
            per_emp.setdefault(r["emp"]["id"], (r["emp"], []))[1].append(r)
    for e, rs in per_emp.values():
        day = sum(1 for r in rs if r["program"] == "day")
        night = len(rs) - day
        if day and not e["in_day"]:
            out.append(f"{e['name']} is not marked as Kollel Horaah, but has {day} day-hour records "
                       f"(they will be imported as Kollel Horaah).")
        if night and not e["in_night"]:
            out.append(f"{e['name']} is not marked as Kollel Chatzos, but has {night} night-hour records.")
        if not e["id_number"]:
            out.append(f"{e['name']} has no ID number (ת.ז) in the system.")
    missing_id = sorted({r["name"] for r in rows if r["emp"] is None and r["name"] and not r["id"]})
    for n in missing_id:
        out.append(f"{n} - no ID number in the file and no matching avrech.")
    unmatched = {}
    for r in rows:
        if r["emp"] is None:
            unmatched.setdefault((r["sheet"], r["name"] or "(no name)"), 0)
            unmatched[(r["sheet"], r["name"] or "(no name)")] += 1
    for (sheet, name), n in unmatched.items():
        where = f"Sheet '{sheet}': " if sheet else ""
        out.append(f"{where}{name} - {n} records belong to no avrech in the system.")
    sheets = {}
    for r in rows:
        sheets.setdefault(r["sheet"], []).append(r)
    if len(sheets) > 1:
        for s, rs in sheets.items():
            if all(r["emp"] is None for r in rs):
                out.append(f"Sheet '{s}' does not belong to any avrech.")
    return out


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
        progs = {x["program"] for x in rows if (x["id"] or norm_name(x["name"])) == key and x["program"]}
        in_night = 1 if "night" in progs or not progs else 0
        in_day = 1 if "day" in progs else 0
        with db.connect() as c:
            number = r["id"] or f"imp{c.execute('SELECT COUNT(*) FROM employees').fetchone()[0] + 1}"
            exists = c.execute("SELECT 1 FROM employees WHERE emp_number=?", (number,)).fetchone()
            if exists:
                continue
            c.execute("INSERT INTO employees(emp_number, name, pin, role, id_number, in_night, in_day, joined_on) "
                      "VALUES (?,?,?,?,?,?,?,?)",
                      (number, r["name"], "1480", "employee", r["id"] or None, in_night, in_day,
                       db.now().date().isoformat()))
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
                      "exit_confirmed, note, program) VALUES (?,?,?,?,?,?,1,'imported',?)",
                      (r["emp"]["id"], r["in"].isoformat(), r["out"].isoformat() if r["out"] else None, y, m, d,
                       r["program"]))
            added += 1
    return added
