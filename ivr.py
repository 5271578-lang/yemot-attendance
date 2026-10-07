"""Phone call flows for an NBS / Yemot-HaMashiach "API extension". Voice only - no SMS.

The phone system calls  /ivr  on every step of the call and we answer in its text
protocol (read=..., id_list_message=..., go_to_folder=hangup).

Main menu:  1 clock in | 2 clock out | 3 shuttle departure (drivers) | 5 attendance correction | 9 messages
"""
import hmac
import os
import re
import threading
from datetime import datetime, timedelta

from flask import Blueprint, Response, request

import db
import hebcal
import nbs
from prompts import P, WEEKDAYS, say_time

bp = Blueprint("ivr", __name__)

SECRET = os.environ.get("IVR_SECRET", "")                 # sent by the phone system as api_add_0=key=...
REQUIRE_PIN = os.environ.get("REQUIRE_PIN", "0") == "1"
RECORD_FOLDER = os.environ.get("RECORD_FOLDER", "/8")      # folder on the phone system for recordings
NOTIFY_BY_CALL = os.environ.get("NOTIFY_BY_CALL", "1") == "1"
OPEN_SHIFT_LIMIT_HOURS = 20
MESSAGE_HISTORY_DAYS = 14
MAX_RETRIES = 3


# ---- the phone system's text protocol -----------------------------------

_BAD = re.compile(r"[\-\"'&|]")


def tts(text):
    """Text -> 't-a.t-b' (sentences split on '.', forbidden characters removed)."""
    parts = [_BAD.sub(" ", p).strip() for p in text.split(".")]
    return ".".join("t-" + p for p in parts if p)


class Reply:
    def __init__(self, session):
        self.s = session
        self.say_parts = []
        self.action = None

    def say(self, text):
        self.say_parts.append(tts(text))
        return self

    def file(self, path):
        self.say_parts.append("f-" + path)
        return self

    def _messages(self):
        return ".".join(p for p in self.say_parts if p)

    def ask(self, step, prompt, digits=1, allowed=None, min_digits=1, timeout=7, **ctx):
        """Ask for keypad input; the answer comes back to `step`."""
        if prompt:
            self.say(prompt)
        name = self._next_name()
        fields = [name, "no", str(digits or ""), str(min_digits), str(timeout), "No", "no", "no", "",
                  ".".join(allowed) if allowed else "", "1", "Ok", "None", ""]
        self.action = f"read={self._messages()}={','.join(fields)}"
        self._await(name, step, ctx)
        return self

    def record(self, step, prompt, file_name, **ctx):
        self.say(prompt)
        name = self._next_name()
        fields = [name, "no", "record", RECORD_FOLDER, file_name, "no", "yes", "no", "1", "180"]
        self.action = f"read={self._messages()}={','.join(fields)}"
        self._await(name, step, ctx)
        return self

    def hangup(self):
        self.action = "go_to_folder=hangup"
        return self

    def _next_name(self):
        self.s["data"]["seq"] = self.s["data"].get("seq", 0) + 1
        return f"v{self.s['data']['seq']}"

    def _await(self, name, step, ctx):
        self.s["data"]["await"] = {"name": name, "step": step, "ctx": ctx}
        self.s["data"]["last_action"] = self.action

    def text(self):
        out = []
        if self.say_parts and not (self.action or "").startswith("read="):
            out.append("id_list_message=" + self._messages())
        out.append(self.action or "go_to_folder=hangup")
        return "&".join(out)


def respond(body):
    return Response(body, mimetype="text/plain; charset=utf-8")


def value(name):
    vals = request.values.getlist(name)
    return vals[-1] if vals else None


# ---- entry point ---------------------------------------------------------

STEPS = {}


def step(fn):
    STEPS[fn.__name__] = fn
    return fn


@bp.route("/ivr", methods=["GET", "POST"])
def ivr():
    if SECRET and not hmac.compare_digest(value("key") or "", SECRET):
        return respond("go_to_folder=hangup")
    if value("hangup") == "yes":
        return respond("ok")
    call_id = value("ApiCallId") or ""
    s = db.get_session(call_id)
    s["data"].setdefault("seq", 0)
    r = Reply(s)
    waiting = s["data"].get("await")
    if not waiting:
        start(r)
    else:
        answer = value(waiting["name"])
        if answer is None:                                    # answer lost: ask again
            r.action = s["data"].get("last_action")
        elif answer == "None" and waiting["step"] != "recorded":   # caller pressed nothing
            s["data"]["retries"] = s["data"].get("retries", 0) + 1
            if s["data"]["retries"] >= MAX_RETRIES:
                r.say(P["goodbye"]).hangup()
            else:
                s["data"]["await"] = None
                r.say(P["no_input"])
                r.action = None
                _repeat(r, waiting)
        else:
            s["data"]["retries"] = 0
            s["data"]["await"] = None
            STEPS[waiting["step"]](r, answer, **waiting["ctx"])
    if not r.action or not r.action.startswith("read="):
        s["data"]["await"] = None
    db.save_session(call_id, employee_id=s["employee_id"], **s["data"])
    return respond(r.text())


def _repeat(r, waiting):
    """Re-run the prompt that produced `waiting` (used after silence)."""
    prompt_of = {"login_number": lambda: ask_number(r), "login_pin": lambda: ask_pin(r),
                 "main_choice": lambda: main_menu(r), "ext9_choice": lambda: ext9(r),
                 "send_choice": lambda: send_menu(r)}
    fn = prompt_of.get(waiting["step"])
    if fn:
        fn()
    else:
        main_menu(r)


def employee(r):
    if not r.s["employee_id"]:
        return None
    with db.connect() as c:
        return c.execute("SELECT * FROM employees WHERE id=? AND active=1", (r.s["employee_id"],)).fetchone()


def start(r):
    caller = db.normalize_phone(value("ApiPhone") or "")
    r.say(P["welcome"])
    if caller and not REQUIRE_PIN:
        with db.connect() as c:
            emps = [e for e in c.execute("SELECT * FROM employees WHERE active=1 AND phone IS NOT NULL")
                    if db.normalize_phone(e["phone"]) == caller]
        if len(emps) == 1:
            r.s["employee_id"] = emps[0]["id"]
            r.say(P["hello"].format(name=emps[0]["name"]))
            return main_menu(r)
    ask_number(r)


# ---- login ---------------------------------------------------------------

def ask_number(r):
    r.ask("login_number", P["enter_number"], digits=None, timeout=10)


def ask_pin(r):
    r.ask("login_pin", P["enter_pin"], digits=None, timeout=10)


@step
def login_number(r, answer):
    r.s["data"]["typed_number"] = answer
    ask_pin(r)


@step
def login_pin(r, answer):
    with db.connect() as c:
        emp = c.execute("SELECT * FROM employees WHERE emp_number=? AND active=1",
                        (r.s["data"].get("typed_number", ""),)).fetchone()
    if emp and hmac.compare_digest(emp["pin"], answer):
        r.s["employee_id"] = emp["id"]
        r.say(P["hello"].format(name=emp["name"]))
        return main_menu(r)
    r.s["data"]["attempts"] = r.s["data"].get("attempts", 0) + 1
    if r.s["data"]["attempts"] >= 3:
        return r.say(P["too_many"]).hangup()
    r.say(P["bad_login"])
    ask_number(r)


# ---- main menu -----------------------------------------------------------

def main_menu(r):
    emp = employee(r)
    if emp is None:
        return ask_number(r)
    text, keys = P["menu_in_out"], ["1", "2", "5", "9"]
    if emp["is_driver"]:
        text += ". " + P["menu_driver"]
        keys.append("3")
    r.ask("main_choice", text + ". " + P["menu_rest"], allowed=keys)


@step
def main_choice(r, answer):
    emp = employee(r)
    if emp is None:
        return ask_number(r)
    actions = {"1": clock_in, "2": clock_out, "3": shuttle, "5": fix_start, "9": ext9}
    if answer in actions and (answer != "3" or emp["is_driver"]):
        return actions[answer](r)
    r.say(P["invalid"])
    main_menu(r)


def open_shift(conn, emp_id):
    return conn.execute("SELECT * FROM attendance WHERE employee_id=? AND clock_out IS NULL "
                        "ORDER BY clock_in DESC LIMIT 1", (emp_id,)).fetchone()


def clock_in(r):
    emp, t = employee(r), db.now()
    with db.connect() as c:
        row = open_shift(c, emp["id"])
        if row:
            started = db.parse(row["clock_in"])
            if t - started < timedelta(hours=OPEN_SHIFT_LIMIT_HOURS):
                return r.say(P["already_in"].format(time=say_time(started))).hangup()
            c.execute("UPDATE attendance SET note='missing clock-out' WHERE id=?", (row["id"],))
        y, m, d = hebcal.from_gregorian(t.date())
        c.execute("INSERT INTO attendance(employee_id, clock_in, heb_year, heb_month, heb_day) VALUES (?,?,?,?,?)",
                  (emp["id"], t.isoformat(), y, m, d))
    r.say(P["clocked_in"].format(time=say_time(t))).say(P["goodbye"]).hangup()


def clock_out(r):
    emp, t = employee(r), db.now()
    with db.connect() as c:
        row = open_shift(c, emp["id"])
        if not row or t - db.parse(row["clock_in"]) >= timedelta(hours=OPEN_SHIFT_LIMIT_HOURS):
            r.say(P["not_in"])
            return main_menu(r)
        c.execute("UPDATE attendance SET clock_out=? WHERE id=?", (t.isoformat(), row["id"]))
    mins = int((t - db.parse(row["clock_in"])).total_seconds() // 60)
    r.say(P["clocked_out"].format(time=say_time(t), hours=mins // 60, minutes=mins % 60))
    r.say(P["goodbye"]).hangup()


# ---- 3: shuttle departure (drivers) -------------------------------------

def shuttle(r):
    emp = employee(r)
    if not emp["shuttle_group"]:
        r.say(P["shuttle_no_group"])
        return main_menu(r)
    r.ask("shuttle_choice", P["shuttle_confirm"].format(group=emp["shuttle_group"]), allowed=["1", "2"])


def shuttle_recipients(driver):
    with db.connect() as c:
        rows = c.execute("SELECT * FROM employees WHERE active=1 AND id<>? AND shuttle_group=?",
                         (driver["id"], driver["shuttle_group"])).fetchall()
    if driver["shift"]:
        rows = [e for e in rows if e["shift"] == driver["shift"]]
    return [e for e in rows if nbs.israeli(e["phone"] or "")]


@step
def shuttle_choice(r, answer):
    if answer != "1":
        return main_menu(r)
    driver = employee(r)
    people = shuttle_recipients(driver)
    if not people:
        r.say(P["shuttle_nobody"])
        return main_menu(r)
    texts = {e["phone"]: P["shuttle_call_text"].format(name=e["name"], group=driver["shuttle_group"]) for e in people}
    ok, info = nbs.run_tts_calls(texts)
    with db.connect() as c:
        c.execute("INSERT INTO shuttle_runs(driver_id, created_at, shuttle_group, recipients, campaign_id, status) "
                  "VALUES (?,?,?,?,?,?)", (driver["id"], db.now().isoformat(), driver["shuttle_group"],
                                           ", ".join(e["name"] for e in people), info if ok else None,
                                           "sent" if ok else info))
    if not ok:
        return r.say(P["shuttle_failed"]).hangup()
    r.say(P["shuttle_sent"].format(count=len(people), group=driver["shuttle_group"])).say(P["goodbye"]).hangup()


# ---- 5: attendance correction ------------------------------------------

def fix_start(r):
    r.ask("fix_kind", P["fix_kind"], allowed=["1", "2"])


@step
def fix_kind(r, answer):
    r.ask("fix_day", P["fix_day"], allowed=["1", "2"], kind="in" if answer == "1" else "out")


@step
def fix_day(r, answer, kind):
    r.ask("fix_time", P["fix_time"], digits=4, min_digits=4, timeout=10, kind=kind, days_ago=0 if answer == "1" else 1)


@step
def fix_time(r, answer, kind, days_ago):
    hh, mm = int(answer[:2] or 99), int(answer[2:] or 99)
    if not (len(answer) == 4 and answer.isdigit() and hh < 24 and mm < 60):
        r.say(P["fix_bad_time"])
        return r.ask("fix_time", P["fix_time"], digits=4, min_digits=4, timeout=10, kind=kind, days_ago=days_ago)
    day = (db.now() - timedelta(days=days_ago)).date()
    when = datetime(day.year, day.month, day.day, hh, mm, tzinfo=db.TZ)
    with db.connect() as c:
        c.execute("INSERT INTO corrections(employee_id, created_at, kind, requested_time) VALUES (?,?,?,?)",
                  (r.s["employee_id"], db.now().isoformat(), kind, when.isoformat()))
    r.say(P["fix_saved"].format(kind=P["fix_kind_in" if kind == "in" else "fix_kind_out"], time=say_time(when)))
    main_menu(r)


# ---- 9: messages ---------------------------------------------------------

def is_manager(emp):
    return emp["role"] in db.MANAGER_ROLES


def ext9(r):
    emp = employee(r)
    r.ask("ext9_choice", P["ext9_manager"] if is_manager(emp) else P["ext9_employee"], allowed=["1", "2"])


@step
def ext9_choice(r, answer):
    emp = employee(r)
    if answer == "1":
        return start_listening(r, emp)
    if is_manager(emp):
        return send_menu(r)
    record_message(r, {"type": "managers"})


def start_listening(r, emp):
    since = (db.now() - timedelta(days=MESSAGE_HISTORY_DAYS)).isoformat()
    with db.connect() as c:
        rows = c.execute(
            "SELECT r.id FROM message_recipients r JOIN messages m ON m.id=r.message_id "
            "WHERE r.employee_id=? AND (r.listened_at IS NULL OR m.created_at>=?) "
            "ORDER BY (r.listened_at IS NOT NULL), m.created_at", (emp["id"], since)).fetchall()
    ids = [x["id"] for x in rows]
    if not ids:
        r.say(P["no_messages"])
        return main_menu(r)
    r.s["data"]["playlist"] = ids
    play(r, 0)


def say_when(dt):
    y, m, d = hebcal.from_gregorian(dt.date())
    return P["when"].format(weekday=WEEKDAYS[dt.weekday()], day=d, month=hebcal.month_name(m, y, "he"),
                            time=say_time(dt))


def play(r, i, header=True):
    playlist = r.s["data"].get("playlist", [])
    if i >= len(playlist):
        r.say(P["end_messages"])
        return main_menu(r)
    with db.connect() as c:
        row = c.execute("SELECT rc.*, m.recording_path, m.created_at, s.name AS sender_name "
                        "FROM message_recipients rc JOIN messages m ON m.id=rc.message_id "
                        "JOIN employees s ON s.id=m.sender_id WHERE rc.id=?", (playlist[i],)).fetchone()
        if row is None or row["employee_id"] != r.s["employee_id"]:
            return play(r, i + 1)
        c.execute("UPDATE message_recipients SET listened_at=COALESCE(listened_at, ?) WHERE id=?",
                  (db.now().isoformat(), row["id"]))
    if header:
        kind = P["msg_new"] if row["listened_at"] is None else P["msg_saved"]
        r.say(P["msg_header"].format(kind=kind, n=i + 1, total=len(playlist), sender=row["sender_name"],
                                     when=say_when(db.parse(row["created_at"]))))
    r.file(row["recording_path"])
    r.ask("after_message", P["after_msg"], allowed=["1", "2"], i=i)


@step
def after_message(r, answer, i):
    if answer == "1":
        return play(r, i, header=False)
    play(r, i + 1)


def send_menu(r):
    emp = employee(r)
    if not is_manager(emp):                                   # regular employees never send to staff
        return ext9(r)
    r.ask("send_choice", P["send_menu_tech"] if emp["role"] == "tech_manager" else P["send_menu_onsite"],
          allowed=["1", "2", "3", "4"])


@step
def send_choice(r, answer):
    emp = employee(r)
    if not is_manager(emp):
        return ext9(r)
    targets = {"1": {"type": "shift"}, "2": {"type": "group", "group": 1}, "3": {"type": "group", "group": 2}}
    if answer in targets:
        if not resolve_recipients(emp, targets[answer]):
            r.say(P["no_recipients"])
            return send_menu(r)
        return record_message(r, targets[answer])
    r.ask("send_employee", P["enter_target"], digits=None, timeout=10)


@step
def send_employee(r, answer):
    emp = employee(r)
    if not is_manager(emp):
        return ext9(r)
    with db.connect() as c:
        target = c.execute("SELECT * FROM employees WHERE emp_number=? AND active=1", (answer,)).fetchone()
    if target is None:
        r.say(P["target_not_found"])
        return send_menu(r)
    if emp["role"] == "onsite_manager" and target["shift"] != emp["shift"]:
        r.say(P["target_not_in_shift"])
        return send_menu(r)
    r.say(P["target_is"].format(name=target["name"]))
    record_message(r, {"type": "employee", "employee_id": target["id"]})


def resolve_recipients(sender, target):
    with db.connect() as c:
        everyone = c.execute("SELECT * FROM employees WHERE active=1 AND id<>?", (sender["id"],)).fetchall()
    t = target["type"]
    if t == "managers":
        tech = [e for e in everyone if e["role"] == "tech_manager"]
        onsite = [e for e in everyone if e["role"] == "onsite_manager"]
        same_shift = [e for e in onsite if e["shift"] and e["shift"] == sender["shift"]]
        return tech + (same_shift or onsite)
    if not is_manager(sender):
        return []
    if sender["role"] == "tech_manager":
        scope = everyone
    elif sender["shift"]:
        scope = [e for e in everyone if e["shift"] == sender["shift"]]
    else:
        scope = []
    if t == "shift":
        return scope
    if t == "group":
        return [e for e in scope if e["shuttle_group"] == target["group"]]
    if t == "employee":
        return [e for e in scope if e["id"] == target["employee_id"]]
    return []


def target_label(target, recipients):
    return {"shift": "כל העובדים", "managers": "המנהלים"}.get(target["type"]) or (
        f"קבוצת הסעה {target['group']}" if target["type"] == "group" else
        (recipients[0]["name"] if recipients else "עובד"))


def record_message(r, target):
    file_name = f"msg{db.now().strftime('%Y%m%d%H%M%S')}_{r.s['employee_id']}"
    r.record("recorded", P["record"], file_name, target=target)


def recording_path(answer):
    """Normalize what the phone system returns ('ivr2:/8/x.wav', '/8/x.wav', '8/x') -> '/8/x'."""
    p = answer.replace("ivr2:", "").strip()
    if p.lower().endswith(".wav"):
        p = p[:-4]
    return p if p.startswith("/") else "/" + p


@step
def recorded(r, answer, target):
    emp = employee(r)
    if not answer or answer in ("None", "0"):
        r.say(P["empty_recording"])
        return main_menu(r)
    recipients = resolve_recipients(emp, target)
    if not recipients:
        r.say(P["no_recipients"])
        return main_menu(r)
    t = db.now()
    with db.connect() as c:
        msg_id = c.execute(
            "INSERT INTO messages(sender_id, created_at, heb_date, recipient_type, recipient_label, recording_path) "
            "VALUES (?,?,?,?,?,?)", (emp["id"], t.isoformat(), hebcal.format_date(t.date(), "he"), target["type"],
                                     target_label(target, recipients), recording_path(answer))).lastrowid
        c.executemany("INSERT INTO message_recipients(message_id, employee_id) VALUES (?,?)",
                      [(msg_id, e["id"]) for e in recipients])
    if NOTIFY_BY_CALL and nbs.enabled():
        threading.Thread(target=notify, args=(msg_id,), daemon=True).start()
    r.say(P["saved_to_managers"] if target["type"] == "managers" else P["saved_to_employees"])
    main_menu(r)


def notify(msg_id):
    """Phone each recipient (voice, not SMS) and tell them a new message is waiting."""
    with db.connect() as c:
        rows = c.execute("SELECT rc.id, e.name, e.phone, s.name AS sender FROM message_recipients rc "
                         "JOIN employees e ON e.id=rc.employee_id JOIN messages m ON m.id=rc.message_id "
                         "JOIN employees s ON s.id=m.sender_id WHERE rc.message_id=?", (msg_id,)).fetchall()
    callable_rows = [x for x in rows if nbs.israeli(x["phone"] or "")]
    if not callable_rows:
        return
    ok, info = nbs.run_tts_calls({x["phone"]: P["notify_call_text"].format(name=x["name"], sender=x["sender"])
                                  for x in callable_rows})
    with db.connect() as c:
        c.execute("UPDATE messages SET campaign_id=? WHERE id=?", (info if ok else None, msg_id))
        c.executemany("UPDATE message_recipients SET delivery_status=? WHERE id=?",
                      [("called" if ok else "call failed", x["id"]) for x in callable_rows])
