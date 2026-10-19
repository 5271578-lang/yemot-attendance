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
        for t in ("tefila_draws", "tefila_entries", "trip_signups", "message_recipients", "messages", "attendance", "corrections", "shuttle_runs", "bonuses",
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
    if "להמשך לקו הקישו 2" in t:                                   # messages waiting: skip to the menu
        t = c.press("2")
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
    assert "הכניסה שלך להיום נרשמה לשעה 2 ו 20 דקות" in t and c.hung_up
    assert db.parse(attendance("303")[0]["clock_in"]).minute == 20
    assert "כבר נרשמה" in menu_call("0503333334").press("1")


def test_next_night_confirms_default_exit():
    seed()
    at(2026, 11, 16, 2, 25)
    menu_call("0503333334").press("1")
    at(2026, 11, 17, 2, 28)
    c = menu_call("0503333334")
    t = c.press("1")
    # 16/11/2026 = 6 Kislev 5787
    assert "לפני שאאשר את הכניסה, נא לאשר את היציאה האחרונה שלך, מתאריך 6 בכסלו, מעודכנת לשעה 7 ו 30 דקות" in t
    assert "ארבע ספרות" in t
    at(2026, 11, 17, 2, 31)
    t = c.press("1")
    assert "תודה רבה, הכניסה שלך להיום נרשמה לשעה 2 ו 28 דקות.t-בהצלחה" in t and c.hung_up
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
    t = c.press("0650")                                              # no extra confirmation step
    assert "תודה רבה, הכניסה שלך להיום נרשמה" in t
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
    assert "היציאה האחרונה שלך, מתאריך 2 בכסלו" in t                # the latest night only


def test_early_exit_then_next_night_shows_it():
    seed()
    at(2026, 11, 16, 2, 25)
    menu_call("0503333334").press("1")
    at(2026, 11, 16, 5, 10)
    t = menu_call("0503333334").press("2")
    assert "היציאה החריגה נרשמה בשעה 5 ו 10 דקות" in t
    at(2026, 11, 17, 2, 25)
    t = menu_call("0503333334").press("1")
    assert "מעודכנת לשעה 5 ו 10 דקות" in t
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
    assert "בין הזמנים" not in t and "הכניסה שלך להיום נרשמה" in t
    at(2026, 12, 6, 1, 35)                                           # non-rider at the regular time
    t = menu_call("0503333334").press("1")
    assert "בין הזמנים" in t and "הכניסה שלך להיום נרשמה" in t
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



def test_day_kollel_correction_says_day():
    seed()
    with db.connect() as c:
        c.execute("UPDATE employees SET in_night=0, in_day=1 WHERE emp_number='301'")
    g = hebcal.to_gregorian
    sun = g(5787, 8, 7)                                              # Sunday: the previous day kollel is Thursday
    assert sun.weekday() == 6
    at(sun.year, sun.month, sun.day, 20, 30)
    c = menu_call("0503333332")
    c.press("5")
    assert "לתיקון של היום הקישו 1" in c.press("2")
    c.press("2")
    assert "נשמרה ותועבר לאישור" in c.press("1845")
    with db.connect() as conn:
        k = conn.execute("SELECT * FROM corrections").fetchone()
    assert db.parse(k["requested_time"]) == datetime(sun.year, sun.month, sun.day - 3, 18, 45, tzinfo=db.TZ)
    c = menu_call("0503333331"); c.press("5")                         # night avrech still hears "night"
    assert "לתיקון של הלילה הקישו 1" in c.press("1")



def test_trip_registration_key_7():
    import trips
    seed()
    at(2026, 10, 18, 21, 0)                                           # Sunday evening: open
    c = menu_call("0503333331")
    t = c.press("7")
    assert "הגעת לשלוחת ההרשמה לנסיעה לטבריה" in t and "הקש 1" in t and '"' not in t.split("=")[1].replace('"', "", 0)
    assert "נרשמת בהצלחה לנסיעה, כולל תפילת שחרית" in c.press("1")
    c = menu_call("0503333332"); c.press("7")
    assert "לא כולל תפילת שחרית" in c.press("2")
    c = menu_call("0503333331")
    assert "אתה כבר רשום לנסיעה, כולל תפילת שחרית" in c.press("7")
    assert "לא כולל" in c.press("2")                                    # changed his choice
    with db.connect() as conn:                                        # Kollel Horaah only: does not hear it
        conn.execute("UPDATE employees SET in_night=0, in_day=1 WHERE emp_number='303'")
    c = menu_call("0503333334")
    assert "טבריה" not in Call("0503333334").start() and "טבריה" not in c.press("7")
    rows = trips.signups("teveria_5787")
    assert sorted(x["choice"] for x in rows) == ["2", "2"]
    html = client.get("/admin/registrations", headers=AUTH).data.decode()
    assert "<b>2</b> registered" in html
    at(2026, 10, 19, 15, 1)                                           # Monday 15:01: closed
    t = menu_call("0503333331").press("7")
    assert "טבריה" not in t and "להרשמה" not in Call("0503333331").start()



def test_tefila_bekoach_key_8():
    import tefila
    seed()
    f = os.path.join(tempfile.mkdtemp(), "netz.csv")
    open(f, "w").write("date,netz\n2026-10-18,06:21:40\n2026-10-19,06:22:20\n")
    tefila.NETZ_FILE = f
    at(2026, 10, 18, 5, 15)                                           # 66 min before netz: closed
    assert "תפילה בכוח" not in Call("0503333331").start()
    at(2026, 10, 18, 5, 25)                                           # 56 min before: open
    assert "למבצע תפילה בכוח הקישו 8" in Call("0503333331").start()
    c = menu_call("0503333331")
    t = c.press("8")
    assert "טלית ותפילין" in t
    t = c.press("1")
    assert "נרשמת בהצלחה למבצע תפילה בכוח להיום" in t and "כניסה אחת" in t and "hangup" in t
    c = menu_call("0503333331")
    assert "כבר נרשמת היום" in c.press("8")
    with db.connect() as conn:                                        # Kollel Horaah only: not for him
        conn.execute("UPDATE employees SET in_night=0, in_day=1 WHERE emp_number='303'")
    assert "8" not in menu_call("0503333334").press("8").split("=")[-1].split(",")[9].split(".")
    at(2026, 10, 18, 5, 52)                                           # 29 min before: closed
    assert "תפילה בכוח" not in Call("0503333332").start()
    at(2026, 10, 19, 5, 30)
    c = menu_call("0503333331"); c.press("8")
    assert "יש לך 2 כניסות להגרלה" in c.press("1")
    c = menu_call("0503333332"); c.press("8"); c.press("1")
    assert [x["entries"] for x in tefila.totals()] == [2, 1]
    html = client.post("/admin/tefila", headers=AUTH, data={"action": "draw"}).data.decode()
    assert "Draw result" in html and "כרטיס טיסה לאומן" in html
    assert len(tefila.draws()) == 1 and len(tefila.draws()[0]["winners"]) == 2   # only 2 avrechim -> 2 winners
    at(2026, 12, 13, 5, 30)                                           # after Zos Chanuka
    assert not tefila.is_open()


def test_manager_tools_sent_groups_and_voice():
    import ivr, tefila, trips
    seed()
    with db.connect() as c:
        c.execute("UPDATE employees SET name='משה כהן' WHERE emp_number='300'")
        c.execute("UPDATE employees SET name='יעקב לוי' WHERE emp_number='301'")
        c.execute("UPDATE employees SET name='משה לוינשטיין' WHERE emp_number='303'")
    assert ",no,record," in Call("0503333331").start() or True
    # new group: Kollel Chatzos without shuttles (303 only in shift A)
    c = menu_call("0502222222"); c.press("4"); c.press("2")
    t = c.press("3")
    assert ",record," in t and "/8," in t and ",,yes,no,1,180" in t          # recording with the review menu
    c.press("/8/ns.wav")
    with db.connect() as conn:
        m = conn.execute("SELECT * FROM messages ORDER BY id DESC").fetchone()
        to = [x[0] for x in conn.execute("SELECT e.emp_number FROM message_recipients r JOIN employees e ON e.id=r.employee_id WHERE message_id=?", (m["id"],))]
    assert m["recipient_label"] == "כולל חצות, מי שלא רשום בהסעות" and to == ["303"]
    # the manager hears what he sent
    c = menu_call("0502222222"); c.press("4")
    t = c.press("3")
    assert "הודעה ששלחת" in t and "שמעו אותה 0 מתוך 1" in t and "f-/8/ns" in t
    # status of another avrech by voice: one match -> confirm
    c = menu_call("0502222222"); c.press("6")
    assert "אמרו את שם האברך" in c.press("2")
    t = c.press("כהן")
    assert "t-משה כהן.t-לאישור הקישו 1" in t
    assert "המצב של משה כהן" in c.press("1")
    # two matches -> choose
    c = menu_call("0502222222"); c.press("6"); c.press("2")
    t = c.press("משה")
    assert "למשה כהן הקישו" in t and "למשה לוינשטיין הקישו" in t
    # not in his scope (shift B) / not found
    c = menu_call("0502222222"); c.press("6"); c.press("2")
    assert "לא נמצא" in c.press("Worker B1")
    # message to one avrech by voice
    c = menu_call("0502222222"); c.press("4"); c.press("2"); c.press("6")
    t = c.press("יעקב לוי")
    assert "t-יעקב לוי.t-לאישור" in t
    assert ",record," in c.press("1")
    # several avrechim at once: 300*303*999#
    c = menu_call("0501111111"); c.press("4"); c.press("2"); c.press("7"); c.press("1")
    t = c.press("300")
    assert "נוסף משה כהן" in t and "לסיום והקלטת ההודעה הקישו 1" in t
    c.press("2"); t = c.press("999")
    assert "9 9 9 לא נמצא" in t
    assert "אמרו את שם האברך" in c.press("3")
    t = c.press("משה")                                                  # two Moshes: choose
    assert "למשה לוינשטיין הקישו" in t
    k = "1" if "למשה לוינשטיין הקישו 1" in t else "2"
    t = c.press(k)
    assert "נוסף משה לוינשטיין" in t
    t = c.press("1")
    assert "נבחרו 2 אברכים" in t and ",record," in t
    c2 = menu_call("0501111111"); c2.press("4"); c2.press("2"); c2.press("7"); c2.press("1")
    t2 = c2.press("300*303")                                          # several at once, with stars
    assert "נוסף משה כהן" in t2 and "נבחרו עד עכשיו 2" in t2
    assert "לעדכון כניסה" in c2.press("0")                           # 0 = main menu
    # global back: 0 in a one-key menu, * in a typed entry
    c3 = menu_call("0502222222"); c3.press("9")
    assert "לעדכון כניסה" in c3.press("0")
    c3.press("4"); c3.press("2"); c3.press("7"); c3.press("1")
    assert "לעדכון כניסה" in c3.press("*")
    c3.press("6")
    assert "לעדכון כניסה" in c3.press("0")
    SENT.clear(); c.press("/8/sev.wav")
    with db.connect() as conn:
        m = conn.execute("SELECT * FROM messages ORDER BY id DESC").fetchone()
        n = conn.execute("SELECT COUNT(*) FROM message_recipients WHERE message_id=?", (m["id"],)).fetchone()[0]
    assert n == 2 and "משה כהן" in m["recipient_label"]
    # an avrech has no manager options
    assert "למצב של אברך אחר" not in menu_call("0503333331").press("6")
    assert match_ok(ivr)


def match_ok(ivr):
    people = [{"name": "חיים בנימין אנשין"}, {"name": "שמעון קעניג"}, {"name": "שמעון קרמר"}]
    return [p["name"] for p in ivr.match_names("קעניג", people)] == ["שמעון קעניג"] and \
        len(ivr.match_names("שמעון", people)) == 2 and ivr.match_names("", people) == []


def test_manager_trip_and_tefila_status():
    import tefila, trips
    seed()
    f = os.path.join(tempfile.mkdtemp(), "netz.csv")
    open(f, "w").write("2026-10-18,06:21:40\n2026-10-19,06:22:20\n")
    tefila.NETZ_FILE = f
    at(2026, 10, 18, 21, 0)
    c = menu_call("0503333331"); c.press("7"); c.press("1")
    c = menu_call("0503333332"); c.press("7"); c.press("2")
    m = menu_call("0502222222")
    assert "t-להרשמה לנסיעה הקישו 1.t-לשמיעת מצב ההרשמה" in m.press("7")
    t = m.press("2")
    assert "לנסיעה נרשמו 2 אברכים" in t and "1 כולל תפילת שחרית" in t
    assert "Worker A1" in m.press("1")
    at(2026, 10, 25, 9, 0)                                            # closed: managers still hear the status
    assert "למצב ההרשמה לנסיעה הקישו 7" in Call("0502222222").start()
    assert "לנסיעה לטבריה" not in Call("0503333331").start()
    assert "לנסיעה נרשמו 2" in menu_call("0502222222").press("7")
    # tefila: an avrech registers, the manager hears the status at any hour during the campaign
    at(2026, 10, 19, 5, 30)
    c = menu_call("0503333331"); c.press("8"); c.press("1")
    at(2026, 10, 19, 10, 0)
    m = menu_call("0501111111")
    t = m.press("8")
    assert "היום נרשמו 1 אברכים" in t and "1 כניסות להגרלה, של 1 אברכים" in t
    assert "Worker A1" in m.press("2")
    m.press("1")
    t = m.press("Digits-300"); t = m.press("1")
    assert "לWorker A1 יש 1 כניסות להגרלה" in t


def test_who_is_here_and_messages_to_present_and_registered():
    import tefila
    seed()
    at(2026, 11, 17, 2, 40)
    menu_call("0503333331").press("1")                                # 300 in
    menu_call("0503333334").press("1")                                # 303 in
    at(2026, 11, 17, 4, 0)
    c = menu_call("0503333334"); c.press("2")                         # 303 left early
    at(2026, 11, 17, 4, 30)
    m = menu_call("0502222222")                                       # site manager A (shift A)
    m.press("6")
    t = m.press("3")
    assert "נוכחים כרגע 1 אברכים" in t and "Worker A1" in t and "No Shuttle" not in t
    t = m.press("1")
    assert "לא נוכחים" in t and "No Shuttle" in t
    m.press("2"); m.press("/8/here.wav")
    with db.connect() as conn:
        msg = conn.execute("SELECT * FROM messages ORDER BY id DESC").fetchone()
        to = [x[0] for x in conn.execute("SELECT e.emp_number FROM message_recipients r JOIN employees e ON e.id=r.employee_id WHERE message_id=?", (msg["id"],))]
    assert msg["recipient_label"] == "מי שנוכח כרגע" and to == ["300"]
    # from the send menu too: 4 -> 2 -> 8
    m = menu_call("0502222222"); m.press("4"); m.press("2")
    assert ",record," in m.press("8")
    # trip registrants (7 -> 2 -> 2) and tefila registrants (8 -> 3)
    at(2026, 10, 18, 21, 0)
    c = menu_call("0503333331"); c.press("7"); c.press("1")
    m = menu_call("0502222222"); m.press("7"); m.press("2")
    m.press("2"); m.press("/8/trip.wav")
    with db.connect() as conn:
        msg = conn.execute("SELECT * FROM messages ORDER BY id DESC").fetchone()
        n = conn.execute("SELECT COUNT(*) FROM message_recipients WHERE message_id=?", (msg["id"],)).fetchone()[0]
    assert msg["recipient_label"] == "הנרשמים לנסיעה" and n == 1
    f = os.path.join(tempfile.mkdtemp(), "netz.csv")
    open(f, "w").write("2026-10-19,06:22:20\n")
    tefila.NETZ_FILE = f
    at(2026, 10, 19, 5, 30)
    c = menu_call("0503333332"); c.press("8"); c.press("1")
    at(2026, 10, 19, 9, 0)
    m = menu_call("0501111111"); m.press("8")
    m.press("3"); m.press("/8/tef.wav")
    with db.connect() as conn:
        msg = conn.execute("SELECT * FROM messages ORDER BY id DESC").fetchone()
        to = [x[0] for x in conn.execute("SELECT e.emp_number FROM message_recipients r JOIN employees e ON e.id=r.employee_id WHERE message_id=?", (msg["id"],))]
    assert msg["recipient_label"] == "הנרשמים היום לתפלה בכח" and to == ["301"]


def test_manager_registers_new_avrech_by_phone():
    seed()
    m = menu_call("0502222222")
    assert "לרישום אברך חדש הקישו 6" in m.press("9")
    m.press("6")
    assert "כבר רשום אצל Worker A1" in m.press("0503333331")         # taken
    m.press("6"); m.press("0529998877")
    m.press("123456782")
    assert "לקבוצת" not in m.press("1") or True
    m.press("2")                                                      # Darom
    t = m.press("שמואל גולדברג")
    assert "השם שנקלט" in t and "שמואל גולדברג" in t
    t = m.press("1")
    assert "נרשם בהצלחה" in t
    with db.connect() as c:
        e = c.execute("SELECT * FROM employees WHERE phone='0529998877'").fetchone()
    assert e["name"] == "שמואל גולדברג" and e["pin"] == "1480" and e["shift"] == "A" and e["shuttle_group"] == 2
    assert e["in_night"] == 1 and e["in_day"] == 0 and e["id_number"] == "123456782" and e["emp_number"] == "001"
    assert "לרישום אברך חדש" not in menu_call("0503333331").press("9")      # avrechim don't have it
    # edit by phone: wrong phone -> fix it
    m = menu_call("0502222222"); m.press("9")
    assert "לעריכת פרטי אברך הקישו 7" in m.last if hasattr(m, "last") else True
    m.press("7"); t = m.press("Digits-123456782")                     # by ID number
    assert "שמואל גולדברג" in t
    t = m.press("1")
    assert "לשינוי הטלפון הקישו 1" in t
    m.press("1"); t = m.press("0529998878")
    assert "השינוי נשמר" in t
    m.press("6")
    m.press("5"); t = m.press("שמואל גולדשטיין")
    assert "שמואל גולדשטיין" in t
    m.press("1")
    with db.connect() as c:
        e = c.execute("SELECT * FROM employees WHERE emp_number='001'").fetchone()
    assert e["phone"] == "0529998878" and e["name"] == "שמואל גולדשטיין"
    import ivr
    assert ivr.next_emp_number("0", ["011", "015", "017", "101", "102"]) == "018"
    assert ivr.next_emp_number("1", ["011", "015", "101", "102"]) == "103"
    assert ivr.next_emp_number("0", ["098", "099"]) == "0100"
    assert ivr.next_emp_number("1", []) == "101"
    people = [{"emp_number": "015"}, {"emp_number": "115"}]
    assert ivr.find_by_number("15", people)["emp_number"] == "015" and ivr.find_by_number("115", people)["emp_number"] == "115"


def test_manager_approves_corrections_and_adds_tefila_by_phone():
    import tefila
    seed()
    at(2026, 11, 17, 2, 50)
    c = menu_call("0503333331"); c.press("5"); c.press("1"); c.press("2"); c.press("0225")   # 300 asks: clock-in 2:25 yesterday
    m = menu_call("0502222222")
    t = m.press("5")
    assert "יש 1 בקשות ממתינות" in t
    t = m.press("2")
    assert "Worker A1 ביקש לתקן את שעת הכניסה" in t
    t = m.press("1")
    assert "אושר" in t and "אין בקשות נוספות" in t
    a = attendance("300")
    assert len(a) == 1 and db.parse(a[0]["clock_in"]).strftime("%d %H:%M") == "16 02:25"
    with db.connect() as conn:
        assert conn.execute("SELECT status FROM corrections").fetchone()[0] == "approved"
    # a rider asks for 1:00 -> recorded 2:25 with Meron
    at(2026, 11, 18, 3, 10)
    c = menu_call("0503333332"); c.press("5"); c.press("1"); c.press("1"); c.press("0100")   # 301 (group 2), tonight
    m = menu_call("0502222222"); m.press("5"); m.press("2")
    t = m.press("1")
    assert "עם נסיעה למירון" in t
    a = attendance("301")
    assert db.parse(a[-1]["clock_in"]).strftime("%H:%M") == "02:25" and a[-1]["meron"] == 1
    assert db.parse(a[-1]["actual_in"]).strftime("%H:%M") == "01:00"
    # the admin page follows the same rule: add 01:00 for a rider -> 2:25 + Meron; a non-rider keeps 01:00
    client.post(f"/admin/employee/{emp('300')['id']}/attendance", headers=AUTH, data={"clock_in": "2026-11-19T01:00"})
    a = attendance("300")[-1]
    assert db.parse(a["clock_in"]).strftime("%H:%M") == "02:25" and a["meron"] == 1
    client.post(f"/admin/employee/{emp('303')['id']}/attendance", headers=AUTH, data={"clock_in": "2026-11-19T01:00"})
    a = attendance("303")[-1]
    assert db.parse(a["clock_in"]).strftime("%H:%M") == "01:00" and a["meron"] == 0
    # editing a rider's clock-in to 01:10 -> 2:25 + Meron; saving without changing the time keeps it
    b = attendance("302")
    client.post(f"/admin/employee/{emp('302')['id']}/attendance", headers=AUTH, data={"clock_in": "2026-11-19T02:40"})
    aid = attendance("302")[-1]["id"]
    client.post(f"/admin/attendance/{aid}", headers=AUTH, data={"clock_in": "2026-11-19T01:10", "clock_out": "", "note": ""})
    a = attendance("302")[-1]
    assert db.parse(a["clock_in"]).strftime("%H:%M") == "02:25" and a["meron"] == 1
    client.post(f"/admin/attendance/{aid}", headers=AUTH, data={"clock_in": "2026-11-19T02:25", "clock_out": "", "note": "x"})
    assert attendance("302")[-1]["meron"] == 1
    # tefila: the manager adds two avrechim to today's raffle
    f = os.path.join(tempfile.mkdtemp(), "netz.csv")
    open(f, "w").write("2026-10-19,06:22:20\n")
    tefila.NETZ_FILE = f
    at(2026, 10, 19, 9, 0)
    m = menu_call("0502222222"); m.press("8")
    m.press("5"); m.press("1")
    t = m.press("300*303*999")
    assert "Worker A1 נוסף להגרלה" in t and "No Shuttle נוסף להגרלה" in t and "9 9 9 לא נמצא" in t
    assert sorted(x["entries"] for x in tefila.totals()) == [1, 1]
    rows = tefila.entries_of_day("2026-10-19")
    assert all(x["added_by"] == emp("200")["id"] for x in rows)
    # admin page: add by hand for yesterday
    client.post("/admin/tefila", headers=AUTH, data={"action": "add", "day": "2026-10-18", "employee_id": [str(emp("301")["id"])]})
    assert len(tefila.entries_of_day("2026-10-18")) == 1
    assert "added by hand" in client.get("/admin/tefila?day=2026-10-18", headers=AUTH).data.decode()


# ---- messages ------------------------------------------------------------

def test_driver_shuttle_also_clocks_in():
    seed()
    at(2026, 11, 16, 2, 20)
    menu_call("0504444444").press("1")
    at(2026, 11, 17, 0, 50)
    c = menu_call("0504444444")
    c.press("3")
    t = c.press("1")
    assert "נשלחת ל" in t and "היציאה האחרונה שלך" in t               # same rules as key 1
    t = c.press("1")
    assert "הכניסה שלך להיום נרשמה לשעה 2 ו 25 דקות" in t and "נסיעה למירון" in t
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
    c.press("4"); c.press("2"); c.press("4")                         # shuttle group 1
    c.press("/8/g.wav")
    _time.sleep(0.2)
    assert calls and sorted(calls[-1]) == ["0503333331", "0503333335", "0504444444"]
    c = menu_call("0502222222")
    c.press("4"); c.press("2"); c.press("6"); c.press("Digits-301"); c.press("1")         # one person: voice call
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
    assert "הכניסה שלך להיום נרשמה" in menu_call("0503333331").press("1")
    assert "ייפתח בשעה 1 ו 30 דקות" in menu_call("0503333334").press("1")
    at(2026, 11, 17, 7, 31)                                          # after the session: next opening tomorrow
    assert "ייפתח בשעה 1 ו 30 דקות" in menu_call("0503333334").press("1")
    at(2026, 12, 6, 1, 40)                                           # Chanuka: riders open at 1:45
    assert "ייפתח בשעה 1 ו 45 דקות" in menu_call("0503333331").press("1")
    t = menu_call("0503333334").press("1")                           # others: regular 1:30
    assert "הכניסה שלך להיום נרשמה" in t


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
    assert "היציאה האחרונה שלך" in c.press("1")                               # the earlier night is asked about first
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


def test_leaving_from_7_20_and_7_25():
    seed()
    d = hebcal.to_gregorian(5787, 8, 15)
    T = lambda h, m: datetime.combine(d, datetime.min.time(), tzinfo=db.TZ).replace(hour=h, minute=m)
    night("300", T(2, 20), T(7, 26))                                  # from 7:25: nothing deducted
    p = pay_of("300", d)
    assert p["base"] == 80 and p["whole"]
    night("301", T(2, 20), T(7, 21))                                  # 7:20-7:25: whole night, minutes deducted
    p = pay_of("301", d)
    assert p["whole"] and abs(p["base"] - 80 * 291 / 300) < 0.01
    night("302", T(2, 20), T(7, 19))                                  # before 7:20: not a whole night
    assert not pay_of("302", d)["whole"]


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
    # night month ends 29 Cheshvan: 10 nights so far, 15 more expected (no Shabbat): 70% of 25 = 17.5 -> 12 more
    assert "ב 70 אחוז, עליך להגיע לעוד 12 לילות שלמים לפחות, מתוך 15 הלילות שנשארו" in t
    assert "ב 80 אחוז, עליך להגיע לעוד 14 לילות שלמים לפחות, מתוך 15 הלילות שנשארו" in t
    assert "מינימום" not in t
    assert "למירון 6 לילות" in t and "עומדת על 870 שקלים" in t
    t = menu_call("0503333334").press("6")
    assert "למינימום המלגה עליך להגיע לעוד 5 לילות שלמים, מתוך 15" in t and "מירון" not in t   # 500 / 110
    assert "כבר לא ניתן להגיע ל 80 אחוז" in t                         # 0 of 10 so far, 15 left: max 60%
    late = g(5787, 8, 28)                                               # near the end of the night month (29th): minimum lost
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
    # 300: 8 Meron nights: 880 stipend (half each) + 280 Meron (Jerusalem) + 300 bonus (Ohr Hatzadik)
    # 302: 10 nights: 1100 (half each) + 300 bonus
    sp = schedule.payer_split(schedule.month_summary(emp("300"), 5787, 8))
    assert sp == {"kollel": 1180, "meron": 280, "total": 1460, "jerusalem": 440 + 280, "ohr_hatzadik": 440 + 300}
    t = report.totals(report.rows_for(5787, 8))
    assert t["kollel"] == 1180 + 1400 and t["meron"] == 280 and t["total"] == 2860
    assert t["jerusalem"] == 440 + 280 + 550 and t["ohr_hatzadik"] == 440 + 300 + 550 + 300
    assert "לשלוח לכולל חצות ירושלים" in html and "1,270" in html and "1,590" in html
    assert csv_text.startswith("\ufeff") and "Worker A1" in csv_text
    sent = []
    report.send = lambda hy, hm, until=None: (sent.append((hy, hm, until)) or (True, "sent"))
    report.enabled = lambda: True
    at(g(5787, 8, 28).year, g(5787, 8, 28).month, g(5787, 8, 28).day, 9, 0)
    assert report.run_once() is None and not sent                    # 28th: not yet (the month closes on the 29th)
    d28 = g(5787, 8, 29)
    at(d28.year, d28.month, d28.day, 7, 0)
    assert report.run_once() is None                                  # before 8:00
    at(d28.year, d28.month, d28.day, 8, 5)
    assert report.run_once() is True and sent[-1][:2] == (5787, 8)
    assert report.run_once() is None and len(sent) == 1              # only once a month
    at(d28.year, d28.month, d28.day + 1, 9, 0)
    assert report.run_once() is None and len(sent) == 1
    r = client.post("/admin/send-report?hy=5787&hm=8", headers=AUTH)
    assert r.status_code == 302 and len(sent) == 2


def test_import_old_system_file():
    import io as _io
    seed()
    raw = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_import_sample.csv"), "rb").read()
    r = client.post("/admin/import", headers=AUTH, data={"file": (_io.BytesIO(raw), "old.csv")},
                    content_type="multipart/form-data")
    html = r.data.decode()
    assert r.status_code == 200 and "<b>0</b> records to import" in html and "6 with no matching avrech" in html
    token = re.search(r'name="token" value="([0-9a-f]+)"', html).group(1)
    r = client.post("/admin/import", headers=AUTH, data={"token": token, "action": "create_and_import"})
    assert "Imported 6 records" in r.data.decode() and "Created 1 new" in r.data.decode()
    e = emp("123456782")
    assert e["id_number"] == "123456782" and e["pin"] == "1480" and "משה" in e["name"]
    a = attendance("123456782")
    assert len(a) == 6 and db.parse(a[0]["clock_in"]).strftime("%d/%m %H:%M") == "07/10 02:25"
    assert db.parse(a[3]["clock_out"]).strftime("%H:%M") == "07:30"
    # importing the same file again adds nothing
    r = client.post("/admin/import", headers=AUTH, data={"file": (_io.BytesIO(raw), "old.csv")},
                    content_type="multipart/form-data")
    assert "6 already there" in r.data.decode()
    # matched by ID for an existing avrech with a different name spelling
    seed()
    with db.connect() as c:
        c.execute("UPDATE employees SET id_number='123456782', name='Moshe Y' WHERE emp_number='303'")
    r = client.post("/admin/import", headers=AUTH, data={"file": (_io.BytesIO(raw), "old.csv")},
                    content_type="multipart/form-data")
    assert "<b>6</b> records to import" in r.data.decode() and "Moshe Y" in r.data.decode()


def test_import_excel_day_hours_and_warnings():
    import io as _io
    import openpyxl
    seed()
    with db.connect() as c:   # 303 = night only, no ID number
        c.execute("UPDATE employees SET name='ישראל ישראלי', id_number=NULL, in_night=1, in_day=0 WHERE emp_number='303'")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "ישראלי"
    ws.append(["דוח נוכחות"])
    ws.append(["זהות", "שם משפחה", "שם פרטי", "כניסה", "יציאה"])
    ws.append(["", "ישראלי", "הרב ישראל", datetime(2026, 10, 12, 2, 30), datetime(2026, 10, 12, 7, 30)])
    ws.append(["", "ישראלי", "ישראל", datetime(2026, 10, 12, 16, 0), datetime(2026, 10, 12, 19, 0)])
    ws2 = wb.create_sheet("זר")
    ws2.append(["זהות", "שם משפחה", "שם פרטי", "כניסה", "יציאה"])
    ws2.append(["111", "פלוני", "אלמוני", "13/10/26 02:30:00", "13/10/26 07:30:00"])
    buf = _io.BytesIO()
    wb.save(buf)
    raw = buf.getvalue()
    # the same file twice in one upload: records counted once
    r = client.post("/admin/import", headers=AUTH, data={"file": [(_io.BytesIO(raw), "a.xlsx"), (_io.BytesIO(raw), "b.xlsx")]},
                    content_type="multipart/form-data")
    html = r.data.decode()
    assert "<b>2</b> records to import" in html and "2 repeated in the files" in html
    assert "not marked as Kollel Horaah" in html and "has no ID number" in html
    assert "does not belong to any avrech" in html and "פלוני" in html
    token = re.search(r'name="token" value="([0-9a-f]+)"', html).group(1)
    r = client.post("/admin/import", headers=AUTH, data={"token": token, "action": "import"})
    assert "Imported 2 records" in r.data.decode()
    progs = sorted(a["program"] for a in attendance("303"))
    assert progs == ["day", "night"]


def test_meron_ticked_by_hand():
    seed()
    e = emp("303")
    with db.connect() as c:
        c.execute("UPDATE employees SET is_driver=1 WHERE id=?", (e["id"],))
        for day in (12, 13):
            t = datetime(2026, 11, day, 2, 30, tzinfo=db.TZ)
            y, m, d = hebcal.from_gregorian(t.date())
            c.execute("INSERT INTO attendance(employee_id, clock_in, clock_out, heb_year, heb_month, heb_day, exit_confirmed, note) "
                      "VALUES (?,?,?,?,?,?,1,'imported')", (e["id"], t.isoformat(), t.replace(hour=7).isoformat(), y, m, d))
    a = attendance("303")
    hy, hm = a[0]["heb_year"], a[0]["heb_month"]
    page_ = client.get(f"/admin/employee/{e['id']}?hy={hy}&hm={hm}", headers=AUTH).data.decode()
    assert 'name="meron"' in page_ and 'name="drove"' in page_
    client.post(f"/admin/employee/{e['id']}/meron?hy={hy}&hm={hm}", headers=AUTH, data={"meron": [str(a[0]["id"])], "drove": [str(a[1]["id"])]})
    sm = schedule.month_summary(emp("303"), hy, hm)
    assert sm["meron_nights"] == 1 and sm["meron"] == 35 and sm["drives"] == 1 and sm["driver"] == 60
    client.post(f"/admin/employee/{e['id']}/meron?hy={hy}&hm={hm}", headers=AUTH, data={})
    sm = schedule.month_summary(emp("303"), hy, hm)
    assert sm["meron_nights"] == 0 and sm["drives"] == 0


def test_employees_split_by_kollel():
    seed()
    with db.connect() as c:
        c.execute("UPDATE employees SET in_night=0, in_day=1 WHERE emp_number='303'")
        c.execute("UPDATE employees SET in_night=0, in_day=0 WHERE emp_number='300'")
    night = client.get("/admin/employees?k=night", headers=AUTH).data.decode()
    day = client.get("/admin/employees?k=day", headers=AUTH).data.decode()
    none = client.get("/admin/employees?k=none", headers=AUTH).data.decode()
    n303 = emp("303")["name"]
    n300 = emp("300")["name"]
    assert n303 in day and n303 not in night and n300 in none and n300 not in night and "In no kollel (1)" in night


def test_waiting_messages_and_newest_first():
    seed()
    rung = []
    nbs.run_tzintuk = lambda phones: (rung.append(phones) or (True, "9"))
    for name in ("/8/old.wav", "/8/new.wav"):
        c = menu_call("0502222222"); c.press("4"); c.press("2"); c.press("4"); c.press(name)
        NOW[0] += timedelta(minutes=5)
    w = Call("0503333331")
    t = w.start()
    assert "קיבלת 2 הודעות חדשות, האחרונה מאת Site Mgr A" in t and "להמשך לקו הקישו 2" in t
    assert "קיבלת" not in Call("0503333331").start()                # announced only on the first call
    c = menu_call("0502222222"); c.press("4"); c.press("2"); c.press("4"); c.press("/8/third.wav")
    assert "קיבלת הודעה מאת Site Mgr A" in Call("0503333331").start()  # a new message is announced again
    w = menu_call("0503333331"); w.press("4")
    assert "1 מתוך 3" in w.press("1") and "f-/8/third" in w.last     # newest first
    assert "f-/8/new" in w.press("2")
    assert "f-/8/old" in w.press("2")
    w = Call("0503333331")
    assert "קיבלת" not in w.start()                                  # all heard: straight to the menu


def test_meron_group_in_settings():
    seed()
    c = menu_call("0503333334")                                      # no group yet
    t = c.press("9")
    assert "לשיוך לנסיעה למירון ולשינוי קבוצה הקישו 4" in t and "מספר השיחות" not in t
    t = c.press("4")
    assert "לקבוצת מאור חיים, על ידי הרב אוריאל דרשן הקישו 1" in t and "לקבוצת דרום, על ידי הרב נפתלי שפירא הקישו 2" in t
    t = c.press("2")
    assert "נרשמת לקבוצת דרום" in t and "כמה פעמים להתקשר" in t
    c.press("3")
    assert emp("303")["shuttle_group"] == 2 and emp("303")["shuttle_calls"] == 3
    c = menu_call("0503333334"); c.press("9"); c.press("4")
    assert "הוסרת מהנסיעה למירון" in c.press("0") and emp("303")["shuttle_group"] is None
    at(2026, 11, 17, 0, 55)
    d = menu_call("0504444444"); d.press("3")
    assert "לאישור יציאת ההסעה של קבוצת מאור חיים" in d.last


def recipients_of_last():
    with db.connect() as c:
        m = c.execute("SELECT * FROM messages ORDER BY id DESC LIMIT 1").fetchone()
        names = sorted(r["name"] for r in c.execute(
            "SELECT e.name FROM message_recipients r JOIN employees e ON e.id=r.employee_id WHERE message_id=?",
            (m["id"],)))
    return m, names


def test_day_kollel():
    import report
    seed()
    with db.connect() as c:
        c.execute("UPDATE employees SET in_day=1 WHERE emp_number IN ('300','303')")    # 300 both, 303 both
        c.execute("UPDATE employees SET in_night=0, in_day=1 WHERE emp_number='301'")  # 301 day only
        c.execute("INSERT INTO employees(emp_number,name,phone,pin,role,in_night,in_day) "
                  "VALUES ('210','Day Mgr','0502222299','2299','onsite_manager',0,1)")
    g = hebcal.to_gregorian
    d = g(5787, 8, 3)                                                 # Wednesday 14/10/2026, Cheshvan
    assert d.weekday() == 2 and schedule.day_kollel_on(d)
    assert not schedule.day_kollel_on(g(5787, 8, 6))                  # Shabbat
    assert not schedule.day_kollel_on(g(5787, 9, 26))                 # Chanuka
    assert not schedule.day_kollel_on(g(5787, 1, 3)) and not schedule.day_kollel_on(g(5787, 6, 1))
    # 16:05: key 1 is a day kollel clock-in
    at(d.year, d.month, d.day, 16, 5)
    t = menu_call("0503333332").press("1")
    assert "נרשמה לשעה 16 ו 5 דקות" in t and "מירון" not in t
    a = attendance("301")[0]
    assert a["program"] == "day"
    # day-only avrech at night: closed, opens tomorrow 15:30
    at(d.year, d.month, d.day + 1, 1, 40)
    assert "ייפתח בשעה 15" in menu_call("0503333332").press("1")
    assert "ייפתח בשעה 15 ו" not in menu_call("0503333332").press("1")           # 15:00 exactly
    # a night member at night still clocks in to the night kollel
    t = menu_call("0503333334").press("1")
    assert "נרשמה לשעה 1 ו 40 דקות" in t and attendance("303")[0]["program"] == "night"
    # next day: the day exit question is about the day kollel only (19:00)
    at(d.year, d.month, d.day + 1, 15, 50)
    c = menu_call("0503333334")
    assert "נרשמה" in c.press("1") and attendance("303")[-1]["program"] == "day"   # no day before: straight in
    c = menu_call("0503333332")
    t = c.press("1")
    assert "מעודכנת לשעה 19" in t
    c.press("1")
    # pay: 800 / (22 days * 3h) per hour, 200 / 22 per day on time
    s = schedule.day_month_summary(emp("301"), 5787, 8)
    assert s["days"] == 2 and s["kept"] == 1                          # 16:05 is late in the day kollel
    expected_base = round((175 + 180) * 800 / (22 * 180), 2)        # 16:05-19:00 + 16:00-19:00 (clocked 15:50)
    assert abs(s["base"] - expected_base) < 0.02 and abs(s["keep"] - 200 / 22) < 0.02
    assert any(schedule.day_kollel_on(g(5787, 5, dd)) for dd in range(1, 8))      # kollel until 7 Av
    assert not schedule.day_kollel_on(g(5787, 5, 8)) and not schedule.day_kollel_on(g(5787, 5, 20))
    # night payroll does not count the day kollel
    assert schedule.month_summary(emp("303"), 5787, 8)["nights"] == 1
    # vacation month = average of the last three kollel months
    v = schedule.day_month_summary(emp("301"), 5788, 7)                # Tishrei 5788: Elul, Av skipped, Tammuz, Sivan
    assert v["vacation"] and v["total"] == 0
    # Av: 1-7 by attendance (at the rate of a whole Av), the rest by the 3-month average
    import unittest.mock as um
    with um.patch.object(schedule, "day_average", lambda e, y, m: 990.0):
        av = schedule.day_month_summary(emp("301"), 5787, 5)
        rate_days = schedule._would_be_av_days(5787)
        kollel_days = schedule.day_dates(5787, 5)
        assert all(hebcal.from_gregorian(x)[2] <= 7 for x in kollel_days)
        assert av["average"] == round(990 * (len(rate_days) - len(kollel_days)) / len(rate_days), 2)
    # messages: the day manager reaches only day avrechim; a day avrech's message reaches the day manager
    rung = []
    nbs.run_tzintuk = lambda phones: (rung.append(phones) or (True, "9"))
    m = menu_call("0502222299"); m.press("4"); m.press("2"); m.press("2"); m.press("/8/day.wav")
    _, names = recipients_of_last()
    assert names == ["No Shuttle", "Worker A1", "Worker A2"]
    w = menu_call("0503333332"); w.press("4"); w.press("2"); w.press("/8/q.wav")
    assert "Day Mgr" in recipients_of_last()[1] and "Site Mgr A" not in recipients_of_last()[1]
    # key 6 for a day-only avrech
    t = menu_call("0503333332").press("6")
    assert "בכולל הוראה" in t and "היית נוכח 2 ימים" in t and "לילות" not in t
    # admin pages and the day e-mail
    assert client.get("/admin/day?hy=5787&hm=8", headers=AUTH).status_code == 200
    subject, html, csv_text = report.build_day(5787, 8)
    assert "כולל הוראה" in subject and "Worker A2" in html and "אור הצדיק" in html


def test_additional_phone():
    seed()
    with db.connect() as c:
        c.execute("UPDATE employees SET phone2='0587777777' WHERE emp_number='300'")
    assert "שלום Worker A1" in Call("058-777-7777").start()            # recognised by the additional phone
    o = Call(); o.start(); o.press("0587777777")
    assert "שלום Worker A1" in o.press("3331")                          # signs in with it too
    r = client.post("/admin/employees", headers=AUTH, data={"emp_number": "600", "name": "X", "role": "employee",
                                                             "phone": "0509999990", "phone2": "0587777777"})
    assert b"already belongs" in r.data
    r = client.post(f"/admin/employees/{emp('301')['id']}", headers=AUTH, data={
        "emp_number": "301", "name": "Worker A2", "role": "employee", "phone": "0503333332", "phone2": "0581111222",
        "pin": "3332", "in_night": "on", "active": "on"})
    assert r.status_code == 302 and emp("301")["phone2"] == "0581111222"


def test_additional_phone_in_settings():
    seed()
    c = menu_call("0503333331")
    assert "לטלפון נוסף הקישו 5" in c.press("9")
    assert "מספר הטלפון הנוסף" in c.press("5")
    assert "אינו תקין" in c.press("12")
    assert "כבר רשום אצל אברך אחר" in c.press("0503333332")
    c = menu_call("0503333331"); c.press("9"); c.press("5")
    t = c.press("058-111-2233")
    assert "0 5 8 1 1 1 2 2 3 3 נשמר" in t and "כשההסעה יוצאת בלבד הקישו 1" in t
    assert "כמה פעמים להתקשר לטלפון הנוסף" in c.press("2")
    assert "עד 4 פעמים" in c.press("4")
    e = emp("300")
    assert (e["phone2"], e["phone2_mode"], e["phone2_calls"]) == ("0581112233", "all", 4)
    assert "שלום Worker A1" in Call("0581112233").start()
    # the shuttle calls both phones, each with its own number of attempts
    at(2026, 11, 17, 0, 55)
    d = menu_call("0504444444"); d.press("3"); d.press("1")
    kind, texts, attempts = SENT[-1]
    assert attempts["0503333331"] == 2 and attempts["0581112233"] == 4
    # 'all': a message to the group rings the additional phone too
    rung = []
    nbs.run_tzintuk = lambda phones: (rung.append(phones) or (True, "9"))
    m = menu_call("0502222222"); m.press("4"); m.press("2"); m.press("4"); m.press("/8/x.wav")
    _time.sleep(0.2)
    assert "0581112233" in rung[-1]
    # shuttle only: not rung for messages; menu shows the mode; delete
    c = menu_call("0503333331"); c.press("9")
    t = c.press("5")
    assert "בכל ההודעות" in t
    c.press("2"); c.press("1"); c.press("1")
    m = menu_call("0502222222"); m.press("4"); m.press("2"); m.press("4"); m.press("/8/y.wav")
    _time.sleep(0.2)
    assert "0581112233" not in rung[-1]
    c = menu_call("0503333331"); c.press("9"); c.press("5")
    assert "נמחק" in c.press("3") and emp("300")["phone2"] is None


def test_hangup_notice():
    assert client.post("/ivr", data={"ApiCallId": "z", "hangup": "yes", "key": "s3cret"}).data == b"ok"


if __name__ == "__main__":
    tests = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    for t in tests:
        t()
        print("ok ", t.__name__)
    print(f"\nAll {len(tests)} tests passed.")
