"""Simulated phone calls through every flow, in the NBS/Yemot API protocol.
Run:  python test_flows.py
"""
import os
import re
import tempfile
from datetime import timedelta

os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "test.db")
os.environ["ADMIN_PASSWORD"] = "pw"
os.environ["IVR_SECRET"] = "s3cret"
os.environ.pop("NBS_TOKEN", None)

import app as appmod  # noqa: E402
import db  # noqa: E402
import hebcal  # noqa: E402
import ivr  # noqa: E402
import nbs  # noqa: E402

client = appmod.app.test_client()
AUTH = {"Authorization": "Basic YWRtaW46cHc="}      # admin:pw
SENT_CALLS = []


def fake_calls(texts):
    SENT_CALLS.append(texts)
    return True, "4242"


nbs.run_tts_calls = fake_calls
nbs.enabled = lambda: True
_n = 0


def seed():
    SENT_CALLS.clear()
    with db.connect() as c:
        for t in ("message_recipients", "messages", "attendance", "corrections", "shuttle_runs", "bonuses",
                  "call_sessions", "employees"):
            c.execute(f"DELETE FROM {t}")
        rows = [("100", "Tech Mgr", "0501111111", "1111", "tech_manager", None, None, 40.0, 0),
                ("200", "Site Mgr A", "0502222222", "2222", "onsite_manager", "A", None, None, 0),
                ("201", "Site Mgr B", "0502222223", "2223", "onsite_manager", "B", None, None, 0),
                ("300", "Worker A1", "0503333331", "3331", "employee", "A", 1, None, 0),
                ("301", "Worker A2", "0503333332", "3332", "employee", "A", 2, None, 0),
                ("302", "Worker B1", "0503333333", "3333", "employee", "B", 1, None, 0),
                ("303", "No Phone", None, "3334", "employee", "A", 1, None, 0),
                ("400", "Driver A1", "0504444444", "4444", "employee", "A", 1, None, 1)]
        c.executemany("INSERT INTO employees(emp_number,name,phone,pin,role,shift,shuttle_group,hourly_rate,is_driver) "
                      "VALUES (?,?,?,?,?,?,?,?,?)", rows)


def emp_id(num):
    with db.connect() as c:
        return c.execute("SELECT id FROM employees WHERE emp_number=?", (num,)).fetchone()["id"]


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


def logged_in(phone):
    c = Call(phone)
    t = c.start()
    assert "לכניסה הקישו 1" in t, t
    return c


# ---- tests ---------------------------------------------------------------

def test_wrong_secret_rejected():
    seed()
    r = client.post("/ivr", data={"ApiCallId": "x", "ApiPhone": "0503333331", "key": "wrong"})
    assert r.data.decode() == "go_to_folder=hangup"


def test_protocol_format():
    seed()
    t = logged_in("0503333331").last
    # one read command: messages, '=', then the 14 positional fields
    msgs, fields = t[len("read="):].split("=")
    assert msgs.startswith("t-") and ".t-" in msgs
    f = fields.split(",")
    assert len(f) == 14 and f[1] == "no" and f[9] == "1.2.5.9" and f[11] == "Ok" and f[12] == "None"


def test_unknown_caller_login():
    seed()
    c = Call()
    assert "מספר עובד" in c.start()
    assert "קוד אישי" in c.press("300")
    assert "שגויים" in c.press("0000")
    c.press("300")
    t = c.press("3331")
    assert "שלום Worker A1" in t and "לכניסה הקישו 1" in t


def test_three_bad_pins_hang_up():
    seed()
    c = Call()
    c.start()
    for _ in range(3):
        c.press("300")
        c.press("9")
    assert "יותר מדי" in c.last and c.hung_up


def test_silence_repeats_then_hangs_up():
    seed()
    c = logged_in("0503333331")
    t = c.press("None")
    assert "לא התקבלה בחירה" in t and "לכניסה הקישו 1" in t
    c.press("None")
    c.press("None")
    assert c.hung_up


def test_clock_in_and_out():
    seed()
    t = logged_in("050-333-3331").press("1")
    assert "הכניסה נרשמה" in t and t.endswith("go_to_folder=hangup") and t.startswith("id_list_message=")
    assert "כבר נרשמה" in logged_in("0503333331").press("1")
    with db.connect() as conn:
        a = conn.execute("SELECT * FROM attendance").fetchone()
        conn.execute("UPDATE attendance SET clock_in=? WHERE id=?",
                     ((db.parse(a["clock_in"]) - timedelta(hours=8, minutes=30)).isoformat(), a["id"]))
    t = logged_in("0503333331").press("2")
    assert "עבדת 8 שעות ו 30 דקות" in t
    t = logged_in("0503333331").press("2")
    assert "לא נמצאה כניסה פתוחה" in t and "לכניסה הקישו 1" in t      # back to menu, not hang up


def test_driver_menu_and_shuttle_calls():
    seed()
    c = logged_in("0504444444")
    assert "לאישור יציאת ההסעה הקישו 3" in c.last
    assert "קבוצה 1" in c.press("3")
    t = c.press("1")
    assert "נשלחת ל 1 עובדים" in t and c.hung_up
    assert list(SENT_CALLS[-1]) == ["0503333331"]          # group 1 + shift A, with a phone, not the driver
    assert "ההסעה של קבוצה 1 יצאה לדרך" in SENT_CALLS[-1]["0503333331"]
    with db.connect() as conn:
        assert conn.execute("SELECT status FROM shuttle_runs").fetchone()[0] == "sent"
    # regular employee: no key 3
    w = logged_in("0503333331")
    assert "הסעה" not in w.last
    assert "בחירה שגויה" in w.press("3")


def test_attendance_correction_request_and_approval():
    seed()
    c = logged_in("0503333331")
    assert "לתיקון שעת כניסה" in c.press("5")
    c.press("1")                              # clock-in
    c.press("2")                              # yesterday
    assert "אינה תקינה" in c.press("2599")
    t = c.press("0815")
    assert "נשמרה ותועבר לאישור" in t and "8 ו 15 דקות" in t
    c.press("5"); c.press("2"); c.press("2"); c.press("1630")
    with db.connect() as conn:
        ks = conn.execute("SELECT * FROM corrections ORDER BY id").fetchall()
    assert [k["kind"] for k in ks] == ["in", "out"] and all(k["status"] == "pending" for k in ks)
    # approving clock-out first fails, then clock-in, then clock-out works
    client.post("/admin/corrections", data={"id": ks[1]["id"], "action": "approve"}, headers=AUTH)
    client.post("/admin/corrections", data={"id": ks[0]["id"], "action": "approve"}, headers=AUTH)
    client.post("/admin/corrections", data={"id": ks[1]["id"], "action": "approve"}, headers=AUTH)
    with db.connect() as conn:
        a = conn.execute("SELECT * FROM attendance").fetchone()
        statuses = [k["status"] for k in conn.execute("SELECT status FROM corrections ORDER BY id")]
    assert statuses == ["approved", "approved"]
    yday = (db.now() - timedelta(days=1)).date()
    assert db.parse(a["clock_in"]).date() == yday and db.parse(a["clock_in"]).hour == 8
    assert (db.parse(a["clock_out"]) - db.parse(a["clock_in"])) == timedelta(hours=8, minutes=15)


def ext9_send(phone, choice, target=None, rec="ivr2:/8/msg123.wav"):
    c = logged_in(phone)
    c.press("9")
    t = c.press("2")
    if "לעובד מסוים" in t:
        t = c.press(choice)
        if choice == "4":
            t = c.press(target)
    if "record" not in t:
        return t
    return c.press(rec)


def recipients_of_last():
    with db.connect() as c:
        m = c.execute("SELECT * FROM messages ORDER BY id DESC LIMIT 1").fetchone()
        names = sorted(r["name"] for r in c.execute(
            "SELECT e.name FROM message_recipients r JOIN employees e ON e.id=r.employee_id WHERE message_id=?",
            (m["id"],)))
    return m, names


def test_ext9_menus_by_role():
    seed()
    assert "להשארת הודעה למנהלים" in logged_in("0503333331").press("9")
    c = logged_in("0502222222")
    assert "להשארת הודעה הקישו 2" in c.press("9")
    assert "לכל העובדים במשמרת שלך" in c.press("2")
    c = logged_in("0501111111")
    c.press("9")
    assert "לכל העובדים הקישו 1" in c.press("2")


def test_record_command_format():
    seed()
    c = logged_in("0502222222")
    c.press("9"); c.press("2")
    t = c.press("1")
    f = t.split("=")[2].split(",")
    assert f[2] == "record" and f[3] == "/8" and f[4].startswith("msg") and f[5] == "no" and f[9] == "180"


def test_onsite_manager_scope():
    seed()
    assert "תישלח לעובדים" in ext9_send("0502222222", "1")
    m, names = recipients_of_last()
    assert names == ["Driver A1", "No Phone", "Worker A1", "Worker A2"]
    assert m["recording_path"] == "/8/msg123" and m["heb_date"]
    ext9_send("0502222222", "2")
    assert recipients_of_last()[1] == ["Driver A1", "No Phone", "Worker A1"]
    ext9_send("0502222222", "3")
    assert recipients_of_last()[1] == ["Worker A2"]
    assert "אינו שייך למשמרת" in ext9_send("0502222222", "4", "302")
    ext9_send("0502222222", "4", "301")
    assert recipients_of_last()[1] == ["Worker A2"]
    assert "העובד לא נמצא" in ext9_send("0502222222", "4", "999")


def test_tech_manager_reaches_everyone():
    seed()
    ext9_send("0501111111", "1")
    assert len(recipients_of_last()[1]) == 7
    ext9_send("0501111111", "4", "302")
    assert recipients_of_last()[1] == ["Worker B1"]


def test_employee_message_goes_to_both_managers_and_they_get_a_call():
    seed()
    t = ext9_send("0503333333", None)
    assert "נשלחה למנהלים" in t
    assert recipients_of_last()[1] == ["Site Mgr B", "Tech Mgr"]
    import time
    for _ in range(50):
        if SENT_CALLS:
            break
        time.sleep(0.05)
    assert set(SENT_CALLS[-1]) == {"0502222223", "0501111111"}
    assert "הודעה חדשה מאת Worker B1" in SENT_CALLS[-1]["0501111111"]


def test_employee_cannot_send_to_staff():
    seed()
    c = logged_in("0503333331")
    c.press("9")
    t = c.press("2")
    assert "לעובד מסוים" not in t and "record" in t      # only "message to managers"


def test_empty_recording_not_saved():
    seed()
    t = ext9_send("0503333331", None, rec="None")
    assert "לא הוקלטה" in t
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0


def test_listening_plays_files_in_order_and_marks_heard():
    seed()
    ext9_send("0502222222", "1", rec="/8/first.wav")
    ext9_send("0501111111", "4", "300", rec="/8/second.wav")
    c = logged_in("0503333331")
    c.press("9")
    t = c.press("1")
    assert "הודעה חדשה, 1 מתוך 2, מאת Site Mgr A" in t and "f-/8/first" in t
    t = c.press("1")
    assert "f-/8/first" in t and "מתוך" not in t                 # replay without header
    t = c.press("2")
    assert "2 מתוך 2, מאת Tech Mgr" in t and "f-/8/second" in t
    assert "אין הודעות נוספות" in c.press("2")
    c = logged_in("0503333331")
    c.press("9")
    assert "הודעה שמורה, 1 מתוך 2" in c.press("1")
    other = logged_in("0503333333")
    other.press("9")
    assert "אין הודעות" in other.press("1")


def test_lost_answer_repeats_same_question():
    seed()
    c = logged_in("0503333331")
    before = c.last
    c._send()                         # phone system re-sent without the answer
    assert c.last == before


def test_no_sms_anywhere():
    for f in ("ivr.py", "nbs.py", "admin.py"):
        src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), f), encoding="utf-8").read()
        assert "SendSms" not in src and "withSMS" not in src


def test_payroll_and_admin_pages():
    seed()
    t0 = db.now().replace(hour=8, minute=0, second=0)
    y, m, d = hebcal.from_gregorian(t0.date())
    with db.connect() as c:
        for num, hrs in (("300", 8), ("200", 10), ("100", 5)):
            c.execute("INSERT INTO attendance(employee_id,clock_in,clock_out,heb_year,heb_month,heb_day) VALUES (?,?,?,?,?,?)",
                      (emp_id(num), t0.isoformat(), (t0 + timedelta(hours=hrs)).isoformat(), y, m, d))
        c.execute("INSERT INTO bonuses(employee_id,heb_year,heb_month,amount,note) VALUES (?,?,?,?,?)",
                  (emp_id("300"), y, m, 50, "x"))
    from admin import payroll_rows
    rows = {r["emp"]["emp_number"]: r for r in payroll_rows(y, m)}
    assert rows["300"]["total"] == 8 * 16 + 50 and rows["200"]["total"] == 250 and rows["100"]["total"] == 200
    assert client.get("/admin/").status_code == 401
    ext9_send("0502222222", "1")
    for p in ("/admin/", "/admin/employees", "/admin/messages", "/admin/corrections", "/admin/shuttles",
              f"/admin/employee/{emp_id('300')}", "/admin/export.csv", f"/admin/employees/{emp_id('400')}"):
        assert client.get(p, headers=AUTH).status_code == 200, p
    r = client.post("/admin/employees", headers=AUTH, data={"emp_number": "500", "name": "New", "pin": "1234",
                                                             "role": "employee", "is_driver": "on", "shuttle_group": "2"})
    assert r.status_code == 302
    with db.connect() as c:
        assert c.execute("SELECT is_driver FROM employees WHERE emp_number='500'").fetchone()[0] == 1


def test_hangup_notice():
    assert client.post("/ivr", data={"ApiCallId": "z", "hangup": "yes", "key": "s3cret"}).data == b"ok"


if __name__ == "__main__":
    tests = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    for t in tests:
        t()
        print("ok ", t.__name__)
    print(f"\nAll {len(tests)} tests passed.")
