"""One-time update (Cheshvan 5787) from the list the office sent: address, children and how many of them are
not married, for the Kollel Chatzos avrechim; and who is in Kollel Horaah. Runs once at startup (setting
family_update_1); what it did is shown on the avrechim page."""
import re

import db

CHATZOS = [  # (number in the system, name in the list, address, children, not married)
    ("013", "חיים בנימין אנשין", "בנימין הצדיק (לוחמי הגטאות) 48", 2, 2),
    ("010", "אשר אורלנצ'יק", "בעש\"ט (שפרינצק שר\"י) 330", 2, 2),
    ("007", "שמחה בורשטיין", "מאור חיים 5", 2, 2),
    ("011", "דוד משה גורית", "הרב שך (הרצל ימש\"ו) 7", 3, 3),
    ("009", "אברהם גלינסקי", "מאור חיים 5", None, None),
    ("019", "נחמן דוידוביץ", "בנימין הצדיק (לוחמי הגטאות) 47", 3, 3),
    ("003", "אוריאל דרשן", "מאור חיים 10", 3, 3),
    ("023", "יעקב הרצברג", None, None, None),
    ("002", "ישראל טנג'י", "מאור חיים 12", 3, 3),
    ("001", "ישראל מאיר מנדל", "מאור חיים 2", None, None),
    ("016", "רחמים נימני", "בעש\"ט (שפרינצק שר\"י) 164", 12, None),
    ("012", "חיים אליהו נפתליס", "בעש\"ט (שפרינצק שר\"י) 161", 7, 7),
    ("020", "אשר מרדכי פרידמן", "בנימין הצדיק (לוחמי הגטאות)", None, None),
    ("008", "שמעון קרמר", "הבית השלישי 33", 2, 2),
    ("017", "שמואל ראטה", None, None, None),
    ("015", "מרדכי רוזן", "הרב שך (הרצל ימש\"ו) 5", 4, 4),
    ("014", "יחיאל רוזנגרטן", "בעש\"ט (שפרינצק שר\"י) 142", 6, 5),
    ("005", "יודל שענק", "האחד עשר", 3, 3),
    ("018", "אהרן שפירא", "בנימין הצדיק (לוחמי הגטאות) 52", 1, 1),
    ("004", "נפתלי שפירא", "האחד עשר 306", 2, 2),
    (None, "אמנון דנברג", "יעקב לוי 9", 10, 3),
]
# Kollel Horaah: (first name(s), surname spellings)
HORAAH = [("יצחק", ["הערבסט", "הרבסט"]), ("שמואל", ["טרוטמן"]), ("יעקב יצחק", ["מינצברג"]),
          ("שמעון", ["קעניג", "קניג", "קאניג"]), ("יעקב", ["קרמר", "קרעמער"]), ("אלימלך", ["רוזנברג"]),
          ("אליעזר", ["טבילה"]), ("שרגא פייבל", ["מלמד"]), ("נתן", ["יארבלום", "ירבלום"])]


def _words(s):
    return re.sub(r"[\"'`׳״\-.,()]", " ", s or "").split()


def _find(people, first, surnames):
    firsts = _words(first)                         # "שרגא פייבל": either name is enough
    hits = [p for p in people if any(sn in _words(p["name"]) for sn in surnames)
            and any(f in _words(p["name"]) for f in firsts)]
    return hits[0] if len(hits) == 1 else None


def run_once():
    if db.get_setting("family_update_1"):
        return None
    import ivr
    report = []
    with db.connect() as c:
        people = c.execute("SELECT * FROM employees").fetchall()
        by_num = {p["emp_number"]: p for p in people}
        for num, name, addr, kids, unmarried in CHATZOS:
            e = by_num.get(num) if num else _find(people, " ".join(_words(name)[:-1]), [_words(name)[-1]])
            if e is None:
                new = ivr.next_emp_number("0", [p["emp_number"] for p in c.execute("SELECT emp_number FROM employees")])
                eid = c.execute("INSERT INTO employees(emp_number, name, pin, role, in_night, in_day, joined_on, active) "
                                "VALUES (?,?,?,?,1,0,?,0)", (new, name, "1480", "employee", db.now().date().isoformat())).lastrowid
                report.append(f"נוסף לכולל חצות, עדיין לא פעיל (אין לו כניסה לקו): {name} (מספר {new})")
            else:
                eid = e["id"]
            c.execute("UPDATE employees SET address=COALESCE(?, address), children=COALESCE(?, children), "
                      "children_unmarried=COALESCE(?, children_unmarried) WHERE id=?", (addr, kids, unmarried, eid))
        people = c.execute("SELECT * FROM employees").fetchall()
        for first, surnames in HORAAH:
            name = f"{first} {surnames[0]}"
            e = _find(people, first, surnames)
            if e is None:
                new = ivr.next_emp_number("1", [p["emp_number"] for p in c.execute("SELECT emp_number FROM employees")])
                c.execute("INSERT INTO employees(emp_number, name, pin, role, in_night, in_day, joined_on) VALUES (?,?,?,?,0,1,?)",
                          (new, name, "1480", "employee", db.now().date().isoformat()))
                report.append(f"נוסף לכולל הוראה: {name} (מספר {new}) - צריך להוסיף טלפון")
            elif not e["in_day"]:
                c.execute("UPDATE employees SET in_day=1 WHERE id=?", (e["id"],))
                report.append(f"סומן גם בכולל הוראה: {e['name']}")
    db.set_setting("family_update_report", "\n".join(report) or "כל הפרטים עודכנו")
    db.set_setting("family_update_1", "1")
    return report


def run_once_2():
    """Amnon Danberg: his phone, but not active yet (he has not officially joined): no access to the line."""
    if db.get_setting("family_update_2"):
        return None
    with db.connect() as c:
        hits = [p for p in c.execute("SELECT * FROM employees").fetchall()
                if "דנברג" in _words(p["name"]) and "אמנון" in _words(p["name"])]
        if len(hits) == 1:
            c.execute("UPDATE employees SET phone=?, active=0 WHERE id=?", ("0527672119", hits[0]["id"]))
    db.set_setting("family_update_2", "1")
    return len(hits) == 1


def run_once_3():
    """Natan Yarblum (Moreshet HaNachal): the right phone number."""
    if db.get_setting("family_update_3"):
        return None
    phone = "0548591759"
    with db.connect() as c:
        hits = [p for p in c.execute("SELECT * FROM employees").fetchall()
                if "נתן" in _words(p["name"]) and any(x in _words(p["name"]) for x in ("יארבלום", "ירבלום"))]
        other = c.execute("SELECT name FROM employees WHERE (phone=? OR phone2=?)", (phone, phone)).fetchall()
        ok = len(hits) == 1 and all(o["name"] == hits[0]["name"] for o in other)
        if ok:
            c.execute("UPDATE employees SET phone=?, active=1 WHERE id=?", (phone, hits[0]["id"]))
    if not ok:
        rep = db.get_setting("family_update_report") or ""
        db.set_setting("family_update_report", (rep + "\n" if rep else "") +
                       "לא הצלחתי לעדכן את הטלפון של נתן יארבלום (0548591759) - נא לעדכן בעריכה")
    db.set_setting("family_update_3", "1")
    return ok
