"""Simulated phone calls through every flow, in the NBS/Yemot API protocol.
Run:  python test_flows.py
"""
import os
import re
import sqlite3
import tempfile
import time as _time
from datetime import datetime, timedelta

os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "test.db")
os.environ["ADMIN_PASSWORD"] = "pw"
os.environ["IVR_SECRET"] = "s3cret"
os.environ.pop("NBS_TOKEN", None)
os.environ["DISABLE_REPORT_THREAD"] = "1"

import app as appmod  # noqa: E402
import db  # noqa: E402
import hebcal  # noqa: E402
import nbs  # noqa: E402
import schedule  # noqa: E402

client = appmod.app.test_client()
AUTH = {"Authorization": "Basic YWRtaW46cHc="}      # admin:pw
SENT = []
NOW = [datetime(2026, 11, 17, 2, 20, tzinfo=db.TZ)]   # 7 Kislev 5787: a regular night


def fake_now():
    return NOW[0]


def at(y, mo, d, h, mi):
    NOW[0] = datetime(y, mo, d, h, mi, tzinfo=db.TZ)


db.now = fake_now
nbs.enabled = lambda: True
nbs.run_tts_calls = lambda texts, template_id=None: (SENT.append(("plain", texts)) or (True, "42"))
nbs.run_tts_calls_with_attempts = lambda texts, attempts: (SENT.append(("attempts", texts, attempts)) or (True, "43"))
_n = 0


def seed():
    SENT.clear()
    at(2026, 11, 17, 2, 20)
    with db.connect() as c:
        for t in ("message_recipients", "messages", "attendance", "corrections", "shuttle_runs", "bonuses",
                  "uman", "call_sessions", "employees"):
            c.execute(f"DELETE FROM {t}")
        rows = [("100", "Tech Mgr", "0501111111", "1111", "tech_manager", None, None, 40.0, 0, None),
                ("200", "Site Mgr A", "0502222222", "2222", "onsite_manager", "A", None, None, 0, None),
                ("201", "Site Mgr B", "0502222223", "2223", "onsite_manager", "B", None, None, 0, None),
                ("300", "Worker A1", "0503333331", "3331", "employee", "A", 1, None, 0, 2),
                ("301", "Worker A2", "0503333332", "3332", "employee", "A", 2, None, 0, 1),
                ("302", "Worker B1", "0503333333", "3333", "employee", "B", 1, None, 0, 3),
                ("303", "No Shuttle", "0503333334", "3334", "employee", "A", None, None, 0, None),
                ("304", "New Rider", "0503333335", "3335", "employee", "A", 1, None, 0, None),
                ("400", "Driver A1", "0504444444", "4444", "employee", "A", 1, None, 1, 1)]
        c.executemany("INSERT INTO employees(emp_number,name,phone,pin,role,shift,shuttle_group,hourly_rate,is_driver,"
                      "shuttle_calls) VALUES (?,?,?,?,?,?,?,?,?,?)", rows)


def emp(num):
    with db.connect() as c:
        return c.execute("SELECT * FROM employees WHERE emp_number=?", (num,)).fetchone()


def attendance(num):
    with db.connect() as c:
        return c.execute("SELECT * FROM attendance WHERE employee_id=? ORDER BY clock_in", (emp(num)["id"],)).fetchall()


class Call:
    """Behaves like the phone system: every request repeats all earlier answers."""

    def __init__(self, phone="0599999999"):
        global _n
        _n += 1
        self.values = {"ApiCallId": f"call{_n}", "ApiPhone": phone, "ApiDID": "0770000000",
                       "ApiExtension": "1", "key": "s3cret"}
        self.last = ""

    def _send(self):
        r = client.post("/ivr", data=self.values)
        assert r.status_code == 200, r.data
        self.last = r.data.decode()
        assert self.last.startswith(("read=", "id_list_message=", "go_to_folder=")), self.last
        for bad in ('"', "'", "|"):
            assert bad not in self.last.split("=", 1)[1].split("=")[0], self.last
        return self.last

    def start(self):
        return self._send()

    def press(self, digits):
        m = re.search(r"read=[^=]*=([^,]+),", self.last)
        assert m, f"not waiting for input: {self.last}"
        self.values[m.group(1)] = digits
        return self._send()

    @property
    def hung_up(self):
        return self.last.endswith("go_to_folder=hangup")


def menu_call(phone):
    c = Call(phone)
    t = c.start()
    assert "לעדכון כניסה הקישו 1" in t, t
    return c


# ---- identification ------------------------------------------------------

def test_wrong_secret_rejected():
    seed()
    r = client.post("/ivr", data={"ApiCallId": "x", "ApiPhone": "0503333331", "key": "wrong"})
    assert r.data.decode() == "go_to_folder=hangup"


def test_protocol_format():
    seed()
    t = menu_call("0503333331").last
    msgs, fields = t[len("read="):].split("=")
    assert msgs.startswith("t-") and ".t-" in msgs
    f = fields.split(",")
    assert len(f) == 14 and f[9] == "1.2.4.5.6.9.*" and f[11] == "Ok" and f[12] == "None"


def test_unknown_phone_signs_in_with_phone_and_password():
    seed()
    c = Call()
    assert "מספר הטלפון שלכם" in c.start()
    assert "סיסמה" in c.press("050-333-3331")
    assert "שגויים" in c.press("0000")
    c.press("0503333331")
    t = c.press("3331")
    assert "שלום Worker A1" in t and "לעדכון כניסה" in t


def test_three_bad_passwords_hang_up():
    seed()
    c = Call()
    c.start()
    for _ in range(3):
        c.press("0503333331")
        c.press("9")
    assert "יותר מדי" in c.last and c.hung_up


def test_silence_asks_again_then_hangs_up():
    seed()
    c = menu_call("0503333331")
    t = c.press("None")
    assert "לא התקבלה בחירה" in t and "לעדכון כניסה" in t
    c.press("None")
    c.press("None")
    assert c.hung_up


def test_switch_user_from_known_phone_records_nothing():
    seed()
    c = menu_call("0503333331")
    assert "לכניסה כמשתמש אחר הקישו כוכבית" in c.last
    assert "כניסה כמשתמש אחר" in c.press("*") and "מספר הטלפון" in c.last
    c.press("0503333332")
    t = c.press("3332")
    assert "שלום Worker A2" in t
    c.press("1")
    assert attendance("300") == [] and len(attendance("301")) == 1


# ---- first call: shuttle alert count -------------------------------------

def test_new_rider_chooses_alert_calls_once():
    seed()
    c = Call("0503333335")
    t = c.start()
    assert "כמה פעמים להתקשר" in t and "לעדכון כניסה" not in t
    t = c.press("3")
    assert "עד 3 פעמים" in t and "לעדכון כניסה" in t
    assert emp("304")["shuttle_calls"] == 3
    assert "כמה פעמים" not in Call("0503333335").start()           # not asked again
    assert "כמה פעמים" not in Call("0503333334").start()           # no shuttle group: never asked


# ---- 1: clock in, with yesterday's exit ----------------------------------

def test_clock_in_uses_call_start_time():
    seed()
    at(2026, 11, 17, 2, 20)
    c = menu_call("0503333334")
    at(2026, 11, 17, 2, 23)                                         # pressed 1 three minutes later
    t = c.press("1")
    assert "הכניסה שלך עודכנה בהצלחה לשעה 2 ו 20 דקות" in t and c.hung_up
    assert db.parse(attendance("303")[0]["clock_in"]).minute == 20
    assert "כבר נרשמה" in menu_call("0503333334").press("1")


def test_next_night_confirms_default_exit():
    seed()
    at(2026, 11, 16, 2, 25)
    menu_call("0503333334").press("1")
    at(2026, 11, 17, 2, 28)
    c = menu_call("0503333334")
    t = c.press("1")
    assert "היציאה שלך אתמול מוגדרת לשעה 7 ו 30 דקות" in t and "ארבע ספרות" in t
    at(2026, 11, 17, 2, 31)
    t = c.press("1")
    assert "הכניסה שלך עודכנה בהצלחה לשעה 2 ו 28 דקות" in t
    a = attendance("303")
    assert a[0]["clock_out"] and db.parse(a[0]["clock_out"]).hour == 7 and a[0]["exit_confirmed"] == 1
    assert len(a) == 2 and db.parse(a[1]["clock_in"]).minute == 28


def test_next_night_changes_exit_time():
    seed()
    at(2026, 11, 16, 2, 25)
    menu_call("0503333334").press("1")
    at(2026, 11, 17, 2, 25)
    c = menu_call("0503333334")
    c.press("1")
    assert "אינה תקינה" in c.press("0215")                          # before that night's clock in
    t = c.press("0645")
    assert "שעת היציאה 6 ו 45 דקות" in t
    assert "ארבע ספרות" in c.press("2")                             # re-enter
    c.press("0650")
    t = c.press("1")
    assert "שעת היציאה נשמרה" in t and "הכניסה שלך עודכנה" in t
    a = attendance("303")[0]
    assert db.parse(a["clock_out"]).strftime("%H:%M") == "06:50"


def test_older_nights_are_not_asked_and_day_name_is_used():
    seed()
    at(2026, 11, 12, 2, 25)
    menu_call("0503333334").press("1")
    at(2026, 11, 13, 2, 25)
    menu_call("0503333332").press("1")
    at(2026, 11, 17, 2, 25)
    t = menu_call("0503333334").press("1")
    assert "היציאה שלך ביום חמישי" in t


def test_early_exit_then_next_night_shows_it():
    seed()
    at(2026, 11, 16, 2, 25)
    menu_call("0503333334").press("1")
    at(2026, 11, 16, 5, 10)
    t = menu_call("0503333334").press("2")
    assert "היציאה החריגה נרשמה בשעה 5 ו 10 דקות" in t
    at(2026, 11, 17, 2, 25)
    t = menu_call("0503333334").press("1")
    assert "מוגדרת לשעה 5 ו 10 דקות" in t
    t = menu_call("0503333332").press("2")
    assert "לא נמצאה כניסה" in t and "לעדכון כניסה" in t


def test_bein_hazmanim_notice():
    seed()
    at(2026, 12, 6, 1, 0)                                            # Chanuka 5787, rider at the regular time
    t = menu_call("0503333331").press("1")
    assert "בין הזמנים" in t and "3 וחצי" in t and "2 טבת" in t and "ייפתח בשעה 1 ו 45 דקות" in t
    assert attendance("300") == []
    at(2026, 12, 6, 1, 50)                                           # rider at the bein hazmanim time
    t = menu_call("0503333331").press("1")
    assert "בין הזמנים" not in t and "הכניסה שלך עודכנה" in t
    at(2026, 12, 6, 1, 35)                                           # non-rider at the regular time
    t = menu_call("0503333334").press("1")
    assert "בין הזמנים" in t and "הכניסה שלך עודכנה" in t
    at(2026, 12, 7, 2, 40)                                           # non-rider later: no notice
    assert "בין הזמנים" not in menu_call("0503333334").press("1")
    at(2026, 11, 17, 1, 0)                                           # regular night: no notice
    assert "בין הזמנים" not in menu_call("0503333331").press("1")


# ---- stipend --------------------------------------------------------------

def night(num, cin, cout=None):
    with db.connect() as c:
        y, m, d = hebcal.from_gregorian(schedule.session_date(cin))
        c.execute("INSERT INTO attendance(employee_id, clock_in, clock_out, heb_year, heb_month, heb_day, exit_confirmed) "
                  "VALUES (?,?,?,?,?,?,1)", (emp(num)["id"], cin.isoformat(), cout.isoformat() if cout else None, y, m, d))


def pay_of(num, d):
    recs = schedule.nights_for(emp(num)["id"], d, d)[d]
    return schedule.night_pay(emp(num), recs, d)


def test_night_pay_rules():
    seed()
    T = lambda h, m, day=17: datetime(2026, 11, day, h, m, tzinfo=db.TZ)
    night("300", T(2, 20))                                           # on time, default exit 7:30
    p = pay_of("300", T(2, 20).date())
    assert (p["base"], p["bonus"], p["total"]) == (80, 30, 110)
    night("301", T(2, 40))                                           # 10 minutes late: keeps +20
    p = pay_of("301", T(2, 40).date())
    assert p["bonus"] == 20 and abs(p["base"] - 80 * 290 / 300) < 0.01
    night("302", T(2, 41))                                           # 11 minutes late: no bonus
    assert pay_of("302", T(2, 41).date())["bonus"] == 0
    night("303", T(2, 25), T(5, 0))                                  # left early
    assert pay_of("303", T(2, 25).date())["base"] == 40
    night("200", T(2, 29))                                           # on-site manager: 25 x 5
    assert pay_of("200", T(2, 29).date())["total"] == 155
    bhz = datetime(2026, 12, 6, 3, 25, tzinfo=db.TZ)                 # Chanuka: 3:30-7:30, 4 hours
    night("300", bhz)
    p = pay_of("300", bhz.date())
    assert p["bhz"] and (p["base"], p["bonus"]) == (80, 30)
    bhz2 = datetime(2026, 12, 7, 5, 30, tzinfo=db.TZ)                # half the bein hazmanim session
    night("301", bhz2)
    assert pay_of("301", bhz2.date())["base"] == 40


# ---- Uman ----------------------------------------------------------------

def test_uman_credit_question_and_amounts():
    seed()
    g = hebcal.to_gregorian
    tamuz = [g(5786, 4, d) for d in range(2, 12)]                   # 10 kollel nights
    elul = [g(5786, 6, d) for d in range(19, 29)]                    # 10 kollel nights, the last ones of Elul
    for d in tamuz + elul:
        night("302", datetime.combine(d, datetime.min.time(), tzinfo=db.TZ).replace(hour=2, minute=20))
    for d in tamuz[:6] + elul[:5]:                                   # 60% and 50%
        night("300", datetime.combine(d, datetime.min.time(), tzinfo=db.TZ).replace(hour=2, minute=20))
    for d in (g(5787, 7, 3), g(5787, 7, 4)):                         # two nights after Rosh Hashana
        night("302", datetime.combine(d, datetime.min.time(), tzinfo=db.TZ).replace(hour=2, minute=20))
    when = g(5787, 7, 5)
    at(when.year, when.month, when.day, 2, 15)
    c = Call("0503333331")
    t = c.start()
    assert "זכית לנסוע השנה לאומן" in t
    t = c.press("1")
    assert "אשריך שזכית" in t and "לעדכון כניסה" in t
    with db.connect() as conn:
        u = conn.execute("SELECT * FROM uman").fetchone()
        b = conn.execute("SELECT * FROM bonuses ORDER BY id").fetchall()
    assert abs(u["percent"] - 0.55) < 0.001
    assert len(b) == 5 and all(abs(x["amount"] - 60.5) < 0.01 for x in b)      # 3 before + 2 after so far
    assert sorted({x["heb_month"] for x in b}) == [6, 7]
    assert "אומן" not in Call("0503333331").start()                 # asked only once
    c = Call("0503333332")
    assert "אומן" in c.start()
    assert "העיקר הוא הרצון" in c.press("2")
    o = Call()                                                       # signing in from another phone: not asked
    o.start(); o.press("0503333333")
    assert "אומן" not in o.press("3333")


# ---- 3: shuttle ------------------------------------------------------------

def test_driver_sends_with_each_riders_attempts():
    seed()
    at(2026, 11, 17, 0, 55)
    c = menu_call("0504444444")
    assert "לאישור יציאת ההסעה הקישו 3" in c.last
    c.press("3")
    t = c.press("1")
    assert "נשלחת ל 2 נוסעים" in t                                 # group 1, shift A, not the driver
    kind, texts, attempts = SENT[-1]
    assert attempts == {"0503333331": 2, "0503333335": 1}             # not chosen yet -> 1
    assert "יצאה לדרך" in texts["0503333331"]


# ---- 9: settings -----------------------------------------------------------

def test_settings_password_language_and_calls():
    seed()
    c = menu_call("0503333331")
    t = c.press("9")
    assert "מספר השיחות" in t and "הסיסמה" in t and "כמשתמש אחר" not in t
    c.press("2")
    c.press("12")
    assert "בין 4 ל 8" in c.last
    c.press("5555")
    assert "הסיסמאות אינן זהות" in c.press("5556")
    c.press("5555")
    assert "שונתה" in c.press("5555")
    assert emp("300")["pin"] == "5555"
    c.press("9")
    t = c.press("3")
    assert "For English, press 2" in t and "אידיש" not in t
    t = c.press("2")
    assert "The language was saved" in t and "To clock in, press 1" in t
    assert "Hello Worker A1" in Call("0503333331").start()
    c = Call("0503333331"); c.start(); c.press("9"); c.press("1")
    assert "up to 4 times" in c.press("4")
    s = menu_call("0503333334")
    assert "מספר השיחות" not in s.press("9")                         # no shuttle group


def test_yiddish_falls_back_to_hebrew():
    seed()
    with db.connect() as c:
        c.execute("UPDATE employees SET language='yi' WHERE emp_number='300'")
    assert "לעדכון כניסה הקישו 1" in Call("0503333331").start()


def test_correction_request_and_approval():
    seed()
    at(2026, 11, 17, 2, 50)
    c = menu_call("0503333331")
    c.press("5"); c.press("1"); c.press("2")
    assert "אינה תקינה" in c.press("2599")
    assert "נשמרה ותועבר לאישור" in c.press("0225")
    with db.connect() as conn:
        k = conn.execute("SELECT * FROM corrections").fetchone()
    client.post("/admin/corrections", data={"id": k["id"], "action": "approve"}, headers=AUTH)
    a = attendance("300")
    assert len(a) == 1 and db.parse(a[0]["clock_in"]).day == 16


# ---- messages ------------------------------------------------------------

def test_driver_shuttle_also_clocks_in():
    seed()
    at(2026, 11, 16, 2, 20)
    menu_call("0504444444").press("1")
    at(2026, 11, 17, 0, 50)
    c = menu_call("0504444444")
    c.press("3")
    t = c.press("1")
    assert "נשלחת ל" in t and "היציאה שלך אתמול" in t               # same rules as key 1
    t = c.press("1")
    assert "הכניסה שלך עודכנה בהצלחה לשעה 2 ו 25 דקות" in t and "נסיעה למירון" in t
    assert len(attendance("400")) == 2
    SENT.clear()
    for h, m in ((0, 40), (1, 26), (18, 0)):                         # outside 0:45-1:25: nothing is sent
        at(2026, 11, 18, h, m)
        t = menu_call("0504444444").press("3")
        assert "רק בין השעה 0 ו 45 דקות לשעה 1 ו 25 דקות" in t and "לעדכון כניסה" in t
    at(2026, 12, 6, 1, 30)                                           # Chanuka: 1:45-2:25
    assert "רק בין השעה 1 ו 45 דקות לשעה 2 ו 25 דקות" in menu_call("0504444444").press("3")
    assert SENT == []


def test_group_message_uses_tzintuk():
    seed()
    calls = []
    nbs.run_tzintuk = lambda phones: (calls.append(phones) or (True, "9"))
    c = menu_call("0502222222")
    c.press("4"); c.press("2"); c.press("2")                         # shuttle group 1
    c.press("/8/g.wav")
    _time.sleep(0.2)
    assert calls and sorted(calls[-1]) == ["0503333331", "0503333335", "0504444444"]
    c = menu_call("0502222222")
    c.press("4"); c.press("2"); c.press("4"); c.press("301")         # one person: voice call
    SENT.clear()
    c.press("/8/p.wav")
    _time.sleep(0.2)
    assert SENT and SENT[-1][0] == "plain" and len(calls) == 1


def test_driver_message_to_his_group_and_drivers():
    seed()
    with db.connect() as c:
        c.execute("INSERT INTO employees(emp_number,name,phone,pin,role,shift,shuttle_group,is_driver,shuttle_calls) "
                  "VALUES ('401','Driver B2','0504444445','4445','employee','B',2,1,1)")
    rung = []
    nbs.run_tzintuk = lambda phones: (rung.append(phones) or (True, "9"))
    c = menu_call("0504444444")
    c.press("4")
    assert "לקבוצת ההסעה שלך ולנהגים הקישו 2" in c.press("2")
    assert "record" in c.press("2")
    assert "תישלח" in c.press("/8/d.wav")
    with db.connect() as conn:
        names = sorted(x["name"] for x in conn.execute(
            "SELECT e.name FROM message_recipients r JOIN employees e ON e.id=r.employee_id"))
    assert names == ["Driver B2", "New Rider", "Worker A1"]          # group 1 in shift A + the other driver
    _time.sleep(0.2)
    assert rung and sorted(rung[-1]) == ["0503333331", "0503333335", "0504444445"]
    w = menu_call("0503333331"); w.press("4")
    assert "record" in w.press("2")                                  # not a driver: managers only


def test_messages_still_work():
    seed()
    rung = []
    nbs.run_tzintuk = lambda phones: (rung.append(phones) or (True, "9"))
    c = menu_call("0502222222")
    c.press("4"); c.press("2")
    t = c.press("1")
    f = t.split("=")[2].split(",")
    assert f[2] == "record" and f[3] == "/8"
    assert "תישלח" in c.press("/8/m1.wav")
    _time.sleep(0.2)
    assert rung and "0503333331" in rung[-1]                         # whole shift: tzintuk
    w = menu_call("0503333331"); w.press("4")
    t = w.press("1")
    assert "הודעה חדשה, 1 מתוך 1, מאת Site Mgr A" in t and "f-/8/m1" in t
    e = menu_call("0503333333"); e.press("4")
    assert "record" in e.press("2")                                  # employee: message to managers only


# ---- admin and database ---------------------------------------------------

def test_admin_pages_and_payroll():
    seed()
    night("300", datetime(2026, 11, 17, 2, 20, tzinfo=db.TZ))
    night("300", datetime(2026, 11, 16, 2, 45, tzinfo=db.TZ))
    from admin import payroll_rows
    y, m, _ = hebcal.from_gregorian(datetime(2026, 11, 17).date())
    rows = {r["emp"]["emp_number"]: r for r in payroll_rows(y, m)}
    assert rows["300"]["nights"] == 2 and rows["300"]["stipend"] == 110 + 76 + 300    # 97.5% -> +200 +100
    assert not rows["300"]["eligible"] and rows["300"]["total"] == 0          # 1 whole night, below 500
    assert client.get("/admin/").status_code == 401
    for p in ("/admin/", "/admin/employees", "/admin/messages", "/admin/corrections", "/admin/shuttles",
              f"/admin/employee/{emp('300')['id']}?hy={y}&hm={m}", "/admin/export.csv",
              f"/admin/employees/{emp('400')['id']}"):
        assert client.get(p, headers=AUTH).status_code == 200, p
    r = client.post("/admin/employees", headers=AUTH, data={"emp_number": "500", "name": "New", "pin": "1234",
                                                             "role": "employee", "phone": "0503333331"})
    assert b"already belongs" in r.data
    r = client.post("/admin/employees", headers=AUTH, data={"emp_number": "500", "name": "New", "pin": "",
                                                             "role": "employee", "phone": "0509999999", "language": "yi"})
    assert r.status_code == 302 and emp("500")["language"] == "yi" and emp("500")["pin"] == "1480"


def test_old_database_is_upgraded():
    path = os.path.join(tempfile.mkdtemp(), "old.db")
    con = sqlite3.connect(path)
    con.executescript("""CREATE TABLE employees (id INTEGER PRIMARY KEY, emp_number TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
        phone TEXT, pin TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'employee', shift TEXT, shuttle_group INTEGER,
        hourly_rate REAL, is_driver INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE attendance (id INTEGER PRIMARY KEY, employee_id INTEGER NOT NULL, clock_in TEXT NOT NULL,
        clock_out TEXT, heb_year INTEGER NOT NULL, heb_month INTEGER NOT NULL, heb_day INTEGER NOT NULL, note TEXT);
        INSERT INTO employees(emp_number,name,pin) VALUES ('1','Kept','1234');""")
    con.close()
    old = db.DB_PATH
    db.DB_PATH = path
    try:
        db.init_db()
        with db.connect() as c:
            e = c.execute("SELECT * FROM employees").fetchone()
            cols = {r["name"] for r in c.execute("PRAGMA table_info(attendance)")}
        assert e["name"] == "Kept" and e["language"] == "he" and {"exit_confirmed", "early_exit"} <= cols
    finally:
        db.DB_PATH = old


def test_no_sms_anywhere():
    for f in ("ivr.py", "nbs.py", "admin.py"):
        src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), f), encoding="utf-8").read()
        assert "SendSms" not in src and "withSMS" not in src


def test_clock_in_opening_times():
    seed()
    at(2026, 11, 16, 18, 30)
    c = Call("0503333331")                                           # rider, regular night: opens 0:45
    assert "לעדכון כניסה" in c.start()
    assert "ייפתח בשעה 0 ו 45 דקות" in c.press("1") and "לעדכון כניסה" in c.last
    assert attendance("300") == []
    o = Call("0503333334")                                           # not a rider: opens 1:30
    o.start()
    assert "ייפתח בשעה 1 ו 30 דקות" in o.press("1")
    at(2026, 11, 17, 0, 50)
    assert "הכניסה שלך עודכנה" in menu_call("0503333331").press("1")
    assert "ייפתח בשעה 1 ו 30 דקות" in menu_call("0503333334").press("1")
    at(2026, 11, 17, 7, 31)                                          # after the session: next opening tomorrow
    assert "ייפתח בשעה 1 ו 30 דקות" in menu_call("0503333334").press("1")
    at(2026, 12, 6, 1, 40)                                           # Chanuka: riders open at 1:45
    assert "ייפתח בשעה 1 ו 45 דקות" in menu_call("0503333331").press("1")
    t = menu_call("0503333334").press("1")                           # others: regular 1:30
    assert "הכניסה שלך עודכנה" in t


def test_uman_not_asked_in_cheshvan():
    seed()
    at(2026, 10, 12, 2, 0)                                           # 1 Cheshvan 5787
    assert "אומן" not in Call("0503333331").start()


def test_meron_rider_recorded_at_2_25():
    seed()
    at(2026, 11, 17, 0, 50)                                          # in the shuttle window
    t = menu_call("0503333331").press("1")
    assert "לשעה 2 ו 25 דקות" in t and "נסיעה למירון" in t
    a = attendance("300")[0]
    assert a["meron"] == 1 and db.parse(a["actual_in"]).strftime("%H:%M") == "00:50"
    at(2026, 11, 17, 1, 40)                                          # after the window: 2:25, no Meron
    n = Call("0503333335"); n.start(); n.press("2")                  # (new rider first picks alert calls)
    t = n.press("1")
    assert "לשעה 2 ו 25 דקות" in t and "מירון" not in t
    at(2026, 11, 17, 2, 40)                                          # after 2:25: the real time
    t = menu_call("0503333332").press("1")
    assert "לשעה 2 ו 40 דקות" in t
    at(2026, 12, 6, 2, 0)                                            # bein hazmanim: window 1:45-2:25, recorded 3:25
    c = menu_call("0503333331")
    assert "היציאה שלך" in c.press("1")                               # the earlier night is asked about first
    t = c.press("1")
    assert "לשעה 3 ו 25 דקות" in t and "נסיעה למירון" in t
    at(2026, 11, 17, 1, 35)                                          # not a rider: real time
    assert "לשעה 1 ו 35 דקות" in menu_call("0503333334").press("1")


def month_of(d):
    y, m, _ = hebcal.from_gregorian(d)
    return y, m


def full_nights(num, days, meron=False):
    with db.connect() as c:
        for d in days:
            cin = datetime.combine(d, datetime.min.time(), tzinfo=db.TZ).replace(hour=2, minute=25)
            y, m, dd = hebcal.from_gregorian(d)
            c.execute("INSERT INTO attendance(employee_id, clock_in, heb_year, heb_month, heb_day, exit_confirmed, meron) "
                      "VALUES (?,?,?,?,?,1,?)", (emp(num)["id"], cin.isoformat(), y, m, dd, int(meron)))


def test_month_rules():
    seed()
    g = hebcal.to_gregorian
    days = [g(5787, 8, d) for d in range(2, 12)]                     # 10 nights in Cheshvan, none bein hazmanim
    full_nights("302", days)                                          # someone at every night: 10 held
    full_nights("300", days[:8], meron=True)                          # 80%, all Meron
    full_nights("301", days[:7])                                      # 70%
    full_nights("303", days[:4])                                      # 40%: 440 < 500 -> nothing
    s = schedule.month_summary(emp("300"), 5787, 8)
    assert s["whole"] == 8 and s["early"] == 8 and round(s["percent"], 2) == 0.8
    assert s["percent_bonus"] == 300 and s["stipend"] == 8 * 110 + 300 and s["eligible"]
    assert s["meron"] == 8 * 35 and s["total"] == 880 + 300 + 280
    s = schedule.month_summary(emp("301"), 5787, 8)
    assert s["percent_bonus"] == 200 and s["total"] == 770 + 200
    s = schedule.month_summary(emp("303"), 5787, 8)
    assert not s["eligible"] and s["total"] == 0 and s["stipend"] == 440
    with db.connect() as c:                                           # driver: 60 a night
        for d in days[:3]:
            c.execute("INSERT INTO shuttle_runs(driver_id, created_at, shuttle_group, recipients, status) VALUES (?,?,1,'x','sent')",
                      (emp("400")["id"], datetime.combine(d, datetime.min.time(), tzinfo=db.TZ).replace(hour=1).isoformat()))
    assert schedule.month_summary(emp("400"), 5787, 8)["driver"] == 180


def test_status_key_6():
    seed()
    g = hebcal.to_gregorian
    days = [g(5787, 8, d) for d in range(2, 12)]
    full_nights("302", days)
    full_nights("300", days[:6], meron=True)
    when = g(5787, 8, 11)
    at(when.year, when.month, when.day, 9, 0)
    t = menu_call("0503333331").press("6")
    assert "היית נוכח 6 לילות, מתוכם 6 לילות שלמים, ו 6 לילות זריזין מקדימים" in t
    assert "60 אחוז" in t and "660 שקלים" in t
    # 10 nights so far, 16 more expected nights in Cheshvan (no Shabbat): 70% of 26 nights = 18.2 -> 13 more
    assert "ב 70 אחוז, עליך להגיע לעוד 13 לילות שלמים לפחות, מתוך 16 הלילות שנשארו" in t
    assert "ב 80 אחוז, עליך להגיע לעוד 15 לילות שלמים לפחות, מתוך 16 הלילות שנשארו" in t
    assert "מינימום" not in t
    assert "למירון 6 לילות" in t and "עומדת על 870 שקלים" in t
    t = menu_call("0503333334").press("6")
    assert "למינימום המלגה עליך להגיע לעוד 5 לילות שלמים, מתוך 16" in t and "מירון" not in t   # 500 / 110
    assert "כבר לא ניתן להגיע ל 80 אחוז" in t                         # 0 of 10 so far, 16 left: max 61%
    late = g(5787, 8, 28)                                               # near the end of the month: minimum lost
    at(late.year, late.month, late.day, 9, 0)
    assert "כבר לא ניתן להגיע למינימום" in menu_call("0503333334").press("6")


def test_removed_after_two_counted_months():
    seed()
    g = hebcal.to_gregorian
    with db.connect() as c:
        c.execute("UPDATE employees SET joined_on='2026-01-01'")
    full_nights("302", [g(5786, 12, 2), g(5786, 2, 2)])               # kollel was held in Adar and Iyar
    for d in range(2, 8):                                              # 300: 6 full nights in Adar = eligible
        full_nights("300", [g(5786, 12, d)])
    when = g(5786, 3, 5)                                               # Sivan: counted months Iyar and Adar (Nisan skipped)
    at(when.year, when.month, when.day, 1, 0)
    t = Call("0503333334").start()
    assert "נמחקת מהמערכת" in t and t.endswith("go_to_folder=hangup")
    assert "נמחקת" not in Call("0503333331").start()                   # was eligible in Adar
    assert "נמחקת" not in Call("0502222222").start()                   # managers are never removed
    r = client.get("/admin/employees", headers=AUTH)
    assert b"removed (2 months)" in r.data
    client.post(f"/admin/employees/{emp('303')['id']}/restore", headers=AUTH)
    assert "נמחקת" not in Call("0503333334").start()


def test_monthly_email_report():
    import report
    seed()
    g = hebcal.to_gregorian
    days = [g(5787, 8, d) for d in range(2, 12)]
    full_nights("302", days)
    full_nights("300", days[:8], meron=True)
    subject, html, csv_text = report.build(5787, 8)
    assert "חשוון 5787" in subject and 'dir="rtl"' in html
    assert "Worker A1" in html and "Worker B1" in html and "מתחת למינימום" in html
    assert "1460" in html.replace(",", "")                          # 880 + 300 + 280 Meron
    assert csv_text.startswith("\ufeff") and "Worker A1" in csv_text
    sent = []
    report.send = lambda hy, hm, until=None: (sent.append((hy, hm, until)) or (True, "sent"))
    report.enabled = lambda: True
    at(g(5787, 8, 27).year, g(5787, 8, 27).month, g(5787, 8, 27).day, 9, 0)
    assert report.run_once() is None and not sent                    # 27th: not yet
    d28 = g(5787, 8, 28)
    at(d28.year, d28.month, d28.day, 7, 0)
    assert report.run_once() is None                                  # before 8:00
    at(d28.year, d28.month, d28.day, 8, 5)
    assert report.run_once() is True and sent[-1][:2] == (5787, 8)
    assert report.run_once() is None and len(sent) == 1              # only once a month
    at(d28.year, d28.month, d28.day + 1, 9, 0)
    assert report.run_once() is None and len(sent) == 1
    r = client.post("/admin/send-report?hy=5787&hm=8", headers=AUTH)
    assert r.status_code == 302 and len(sent) == 2


def test_hangup_notice():
    assert client.post("/ivr", data={"ApiCallId": "z", "hangup": "yes", "key": "s3cret"}).data == b"ok"


if __name__ == "__main__":
    tests = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    for t in tests:
        t()
        print("ok ", t.__name__)
    print(f"\nAll {len(tests)} tests passed.")
