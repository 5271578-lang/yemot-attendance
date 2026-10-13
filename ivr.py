"""Phone call flows for an NBS / Yemot-HaMashiach "API extension". Voice only - no SMS.

The phone system calls  /ivr  on every step of the call and we answer in its text
protocol (read=..., id_list_message=..., go_to_folder=hangup).

After identification:
  - once a year after Rosh Hashana (own phone only): did you travel to Uman
  - shuttle passengers, first call only: how many alert calls
Main menu:  1 clock in (confirming the previous night's exit first) | 2 early exit |
            3 shuttle departure (drivers) | 5 attendance correction | 9 messages + personal settings
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
import schedule
from prompts import LANGUAGES, WEEKDAYS, say_time, text

bp = Blueprint("ivr", __name__)

SECRET = os.environ.get("IVR_SECRET", "")                 # sent by the phone system as api_add_0=key=...
RECORD_FOLDER = os.environ.get("RECORD_FOLDER", "/8")      # folder on the phone system for recordings
NOTIFY_BY_CALL = os.environ.get("NOTIFY_BY_CALL", "1") == "1"
MESSAGE_HISTORY_DAYS = 14
MAX_RETRIES = 3
MAX_SHUTTLE_CALLS = 5


# ---- the phone system's text protocol -----------------------------------

_BAD = re.compile(r"[\-\"'&|]")


def tts(txt):
    """Text -> 't-a.t-b' (sentences split on '.', forbidden characters removed)."""
    parts = [_BAD.sub(" ", p).strip() for p in txt.split(".")]
    return ".".join("t-" + p for p in parts if p)


class Reply:
    def __init__(self, session):
        self.s = session
        self.say_parts = []
        self.action = None

    @property
    def lang(self):
        return self.s["data"].get("lang", "he")

    def t(self, key, **kw):
        return text(self.lang, key, **kw)

    def time(self, dt):
        return say_time(dt, self.lang)

    def say(self, txt):
        self.say_parts.append(tts(txt))
        return self

    def file(self, path):
        self.say_parts.append("f-" + path)
        return self

    def _messages(self):
        return ".".join(p for p in self.say_parts if p)

    def ask(self, step, prompt, digits=1, allowed=None, min_digits=1, timeout=7, **ctx):
        """Ask for keypad input; the answer comes back to `step`. digits=None: until #."""
        if prompt:
            self.say(prompt)
        name = self._next_name()
        fields = [name, "no", str(digits or ""), str(min_digits), str(timeout), "No", "no", "no", "",
                  ".".join(allowed) if allowed else "", "1", "Ok", "None", ""]
        self.action = f"read={self._messages()}={','.join(fields)}"
        self.s["data"]["await"] = {"name": name, "step": step, "ctx": ctx,
                                   "again": {"prompt": prompt, "digits": digits, "allowed": allowed,
                                             "min_digits": min_digits, "timeout": timeout}}
        self.s["data"]["last_action"] = self.action
        return self

    def record(self, step, prompt, file_name, **ctx):
        self.say(prompt)
        name = self._next_name()
        fields = [name, "no", "record", RECORD_FOLDER, file_name, "no", "yes", "no", "1", "180"]
        self.action = f"read={self._messages()}={','.join(fields)}"
        self.s["data"]["await"] = {"name": name, "step": step, "ctx": ctx}
        self.s["data"]["last_action"] = self.action
        return self

    def hangup(self):
        self.action = "go_to_folder=hangup"
        return self

    def _next_name(self):
        self.s["data"]["seq"] = self.s["data"].get("seq", 0) + 1
        return f"v{self.s['data']['seq']}"

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
        if answer is None:                                    # answer lost: ask the same again
            r.action = s["data"].get("last_action")
        elif answer == "None" and waiting["step"] != "recorded":   # caller pressed nothing
            s["data"]["retries"] = s["data"].get("retries", 0) + 1
            if s["data"]["retries"] >= MAX_RETRIES or "again" not in waiting:
                r.say(r.t("goodbye")).hangup()
            else:
                again = dict(waiting["again"])
                r.say(r.t("no_input"))
                r.ask(waiting["step"], again.pop("prompt"), **again, **waiting["ctx"])
        else:
            s["data"]["retries"] = 0
            s["data"]["await"] = None
            STEPS[waiting["step"]](r, answer, **waiting["ctx"])
    if not r.action or not r.action.startswith("read="):
        s["data"]["await"] = None
    db.save_session(call_id, employee_id=s["employee_id"], **s["data"])
    return respond(r.text())


def employee(r):
    if not r.s["employee_id"]:
        return None
    with db.connect() as c:
        return c.execute("SELECT * FROM employees WHERE id=? AND active=1", (r.s["employee_id"],)).fetchone()


def sign_in(r, emp, own_phone):
    r.s["employee_id"] = emp["id"]
    r.s["data"]["own_phone"] = own_phone
    r.s["data"]["lang"] = emp["language"] if emp["language"] in LANGUAGES else "he"
    r.say(r.t("hello", name=emp["name"]))
    after_sign_in(r)


def start(r):
    r.s["data"]["call_start"] = db.now().isoformat()
    caller = db.normalize_phone(value("ApiPhone") or "")
    emps = find_by_phone(caller) if caller else []
    if len(emps) == 1:
        r.s["data"]["lang"] = emps[0]["language"] if emps[0]["language"] in LANGUAGES else "he"
        r.say(r.t("welcome"))
        return sign_in(r, emps[0], own_phone=True)
    r.say(r.t("welcome"))
    ask_phone(r)


def call_start(r):
    return db.parse(r.s["data"]["call_start"])


def find_by_phone(phone):
    with db.connect() as c:
        return [e for e in c.execute("SELECT * FROM employees WHERE active=1 AND phone IS NOT NULL")
                if db.normalize_phone(e["phone"]) == phone]


# ---- sign in with phone number + password --------------------------------

def ask_phone(r):
    r.ask("login_phone", r.t("enter_phone"), digits=None, timeout=10)


@step
def login_phone(r, answer):
    r.s["data"]["typed_phone"] = db.normalize_phone(answer)
    r.ask("login_pin", r.t("enter_pin"), digits=None, timeout=10)


@step
def login_pin(r, answer):
    emps = find_by_phone(r.s["data"].get("typed_phone", ""))
    emp = emps[0] if len(emps) == 1 else None
    if emp and hmac.compare_digest(emp["pin"], answer):
        return sign_in(r, emp, own_phone=False)
    r.s["data"]["attempts"] = r.s["data"].get("attempts", 0) + 1
    if r.s["data"]["attempts"] >= 3:
        return r.say(r.t("too_many")).hangup()
    r.say(r.t("bad_login"))
    ask_phone(r)


# ---- once-only questions after sign in -----------------------------------

def after_sign_in(r):
    emp = employee(r)
    today = schedule.session_date(call_start(r))
    year = schedule.uman_question_due(today)
    if year and r.s["data"].get("own_phone"):
        with db.connect() as c:
            asked = c.execute("SELECT 1 FROM uman WHERE employee_id=? AND heb_year=?", (emp["id"], year)).fetchone()
        if not asked:
            return r.ask("uman_answer", r.t("uman_ask"), allowed=["1", "2"], year=year)
    if emp["shuttle_group"] and emp["shuttle_calls"] is None:
        r.say(r.t("shuttle_first"))
        return ask_shuttle_calls(r)
    main_menu(r)


@step
def uman_answer(r, answer, year):
    emp = employee(r)
    went = answer == "1"
    pct, nights = schedule.uman_credit(emp, year, schedule.session_date(call_start(r))) if went else (None, [])
    with db.connect() as c:
        c.execute("INSERT OR REPLACE INTO uman(employee_id, heb_year, went, answered_at, percent, nights) "
                  "VALUES (?,?,?,?,?,?)", (emp["id"], year, int(went), db.now().isoformat(), pct,
                                           ", ".join(d.isoformat() for d, _ in nights)))
        for d, amount in nights:
            hy, hm, hd = hebcal.from_gregorian(d)
            c.execute("INSERT INTO bonuses(employee_id, heb_year, heb_month, amount, note) VALUES (?,?,?,?,?)",
                      (emp["id"], hy, hm, amount, f"Uman {hd} {hebcal.month_name(hm, hy, 'he')} ({round(pct * 100)}%)"))
    r.say(r.t("uman_yes" if went else "uman_no"))
    if emp["shuttle_group"] and emp["shuttle_calls"] is None:
        r.say(r.t("shuttle_first"))
        return ask_shuttle_calls(r)
    main_menu(r)


def ask_shuttle_calls(r):
    r.ask("shuttle_calls_set", r.t("shuttle_calls_ask"), allowed=[str(i) for i in range(1, MAX_SHUTTLE_CALLS + 1)])


@step
def shuttle_calls_set(r, answer):
    n = int(answer)
    with db.connect() as c:
        c.execute("UPDATE employees SET shuttle_calls=? WHERE id=?", (n, r.s["employee_id"]))
    r.say(r.t("shuttle_calls_saved", n=n))
    main_menu(r)


# ---- main menu -----------------------------------------------------------

def main_menu(r):
    emp = employee(r)
    if emp is None:
        return ask_phone(r)
    txt, keys = r.t("menu_main"), ["1", "2", "4", "5", "9"]
    if emp["is_driver"]:
        txt += ". " + r.t("menu_driver")
        keys.append("3")
    r.ask("main_choice", txt + ". " + r.t("menu_rest"), allowed=keys)


@step
def main_choice(r, answer):
    emp = employee(r)
    if emp is None:
        return ask_phone(r)
    actions = {"1": clock_in, "2": early_exit, "3": shuttle, "4": ext4, "5": fix_start, "9": settings_menu}
    if answer == "1" and schedule.bhz_notice_due(emp, db.now()):
        bhz_notice(r)
    if answer in ("1", "2") and not schedule.clock_in_open(emp, db.now()):
        r.say(r.t("closed_until", time=r.time(schedule.next_opening(emp, db.now()))))
        return main_menu(r)
    if answer in actions and (answer != "3" or emp["is_driver"]):
        return actions[answer](r)
    r.say(r.t("invalid"))
    main_menu(r)


def records_of_night(conn, emp_id, d):
    rows = conn.execute("SELECT * FROM attendance WHERE employee_id=? AND clock_in>=? ORDER BY clock_in",
                        (emp_id, (datetime.combine(d, datetime.min.time(), tzinfo=db.TZ)
                                  - timedelta(days=1)).isoformat())).fetchall()
    return [x for x in rows if schedule.session_date(db.parse(x["clock_in"])) == d]


# ---- 1: clock in ---------------------------------------------------------

def clock_in(r):
    emp, t0 = employee(r), call_start(r)
    tonight = schedule.session_date(t0)
    with db.connect() as c:
        mine = records_of_night(c, emp["id"], tonight)
        if mine:
            return r.say(r.t("already_in", time=r.time(db.parse(mine[0]["clock_in"])))).hangup()
        older = c.execute("SELECT * FROM attendance WHERE employee_id=? AND exit_confirmed=0 ORDER BY clock_in DESC",
                          (emp["id"],)).fetchall()
        older = [x for x in older if schedule.session_date(db.parse(x["clock_in"])) < tonight]
        for x in older[1:]:                                    # only the latest night is asked about
            c.execute("UPDATE attendance SET exit_confirmed=1 WHERE id=?", (x["id"],))
    if older:
        return ask_prev_exit(r, older[0]["id"])
    finish_clock_in(r)


def bhz_notice(r):
    last = schedule.bhz_range_of(schedule.session_date(db.now()))[1]
    ly, lm, ld = hebcal.from_gregorian(last)
    r.say(r.t("bhz_notice", date=f"{ld} {hebcal.month_name(lm, ly, 'en' if r.lang == 'en' else 'he')}"))


def prev_exit_info(rid):
    with db.connect() as c:
        row = c.execute("SELECT * FROM attendance WHERE id=?", (rid,)).fetchone()
    d = schedule.session_date(db.parse(row["clock_in"]))
    current = db.parse(row["clock_out"]) if row["clock_out"] else schedule.default_exit(d)
    return row, d, current


def ask_prev_exit(r, rid):
    row, d, current = prev_exit_info(rid)
    tonight = schedule.session_date(call_start(r))
    when = r.t("when_yesterday") if d == tonight - timedelta(days=1) else r.t("when_day", weekday=WEEKDAYS[r.lang][d.weekday()])
    # up to 4 digits: "1" alone is accepted after a short pause (or 1 then #)
    r.ask("prev_exit_answer", r.t("prev_exit", when=when, time=r.time(current)), digits=4, timeout=5, rid=rid)


def parse_hhmm(answer, d):
    if len(answer) != 4 or not answer.isdigit():
        return None
    hh, mm = int(answer[:2]), int(answer[2:])
    if hh > 23 or mm > 59:
        return None
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=db.TZ)


@step
def prev_exit_answer(r, answer, rid):
    row, d, current = prev_exit_info(rid)
    if answer == "1":
        set_exit(rid, current)
        return finish_clock_in(r)
    t = parse_hhmm(answer, d)
    if t is None or t <= db.parse(row["clock_in"]):
        r.say(r.t("bad_time"))
        return ask_prev_exit(r, rid)
    r.ask("prev_exit_confirm", r.t("exit_repeat", time=r.time(t)), allowed=["1", "2"], rid=rid, t=t.isoformat())


@step
def prev_exit_confirm(r, answer, rid, t):
    if answer != "1":
        return ask_prev_exit(r, rid)
    set_exit(rid, db.parse(t))
    r.say(r.t("exit_saved"))
    finish_clock_in(r)


def set_exit(rid, t):
    with db.connect() as c:
        c.execute("UPDATE attendance SET clock_out=?, exit_confirmed=1 WHERE id=?", (t.isoformat(), rid))


def finish_clock_in(r):
    """Clock in at the time the call started, not when the key was pressed."""
    emp, t0 = employee(r), call_start(r)
    y, m, d = hebcal.from_gregorian(schedule.session_date(t0))
    with db.connect() as c:
        c.execute("INSERT INTO attendance(employee_id, clock_in, heb_year, heb_month, heb_day) VALUES (?,?,?,?,?)",
                  (emp["id"], t0.isoformat(), y, m, d))
    r.say(r.t("entry_saved", time=r.time(t0)))
    r.say(r.t("goodbye")).hangup()


# ---- 2: early exit ------------------------------------------------------

def early_exit(r):
    emp, t = employee(r), db.now()
    with db.connect() as c:
        mine = records_of_night(c, emp["id"], schedule.session_date(t))
        if not mine:
            r.say(r.t("not_in"))
            return main_menu(r)
        c.execute("UPDATE attendance SET clock_out=?, early_exit=1, exit_confirmed=0 WHERE id=?",
                  (t.isoformat(), mine[-1]["id"]))
    r.say(r.t("early_exit_saved", time=r.time(t))).say(r.t("goodbye")).hangup()


# ---- 3: shuttle departure (drivers) -------------------------------------

def shuttle(r):
    emp = employee(r)
    if not emp["shuttle_group"]:
        r.say(r.t("shuttle_no_group"))
        return main_menu(r)
    r.ask("shuttle_choice", r.t("shuttle_confirm", group=emp["shuttle_group"]), allowed=["1", "2"])


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
        r.say(r.t("shuttle_nobody"))
        return driver_clock_in(r)
    texts = {e["phone"]: text(e["language"], "shuttle_call_text", name=e["name"], group=driver["shuttle_group"])
             for e in people}
    attempts = {e["phone"]: min(max(e["shuttle_calls"] or 1, 1), MAX_SHUTTLE_CALLS) for e in people}
    ok, info = nbs.run_tts_calls_with_attempts(texts, attempts)
    with db.connect() as c:
        c.execute("INSERT INTO shuttle_runs(driver_id, created_at, shuttle_group, recipients, campaign_id, status) "
                  "VALUES (?,?,?,?,?,?)", (driver["id"], db.now().isoformat(), driver["shuttle_group"],
                                           ", ".join(f"{e['name']} ({attempts[e['phone']]})" for e in people),
                                           info if ok else None, "sent" if ok else info))
    if not ok:
        return r.say(r.t("shuttle_failed")).hangup()
    r.say(r.t("shuttle_sent", count=len(people), group=driver["shuttle_group"]))
    driver_clock_in(r)


def driver_clock_in(r):
    """The driver confirming the shuttle also clocks in, exactly as if he had pressed 1."""
    if schedule.clock_in_open(employee(r), db.now()):
        return clock_in(r)
    r.say(r.t("goodbye")).hangup()


# ---- 5: attendance correction ------------------------------------------

def fix_start(r):
    r.ask("fix_kind", r.t("fix_kind"), allowed=["1", "2"])


@step
def fix_kind(r, answer):
    r.ask("fix_day", r.t("fix_day"), allowed=["1", "2"], kind="in" if answer == "1" else "out")


@step
def fix_day(r, answer, kind):
    r.ask("fix_time", r.t("fix_time"), digits=4, min_digits=4, timeout=10, kind=kind, days_ago=0 if answer == "1" else 1)


@step
def fix_time(r, answer, kind, days_ago):
    d = schedule.session_date(call_start(r)) - timedelta(days=days_ago)
    when = parse_hhmm(answer, d)
    if when is None:
        r.say(r.t("bad_time"))
        return r.ask("fix_time", r.t("fix_time"), digits=4, min_digits=4, timeout=10, kind=kind, days_ago=days_ago)
    with db.connect() as c:
        c.execute("INSERT INTO corrections(employee_id, created_at, kind, requested_time) VALUES (?,?,?,?)",
                  (r.s["employee_id"], db.now().isoformat(), kind, when.isoformat()))
    r.say(r.t("fix_saved", kind=r.t("fix_kind_in" if kind == "in" else "fix_kind_out"), time=r.time(when)))
    main_menu(r)


# ---- 4: messages / 9: personal settings, switch user ---------------------

def is_manager(emp):
    return emp["role"] in db.MANAGER_ROLES


def ext4(r):
    emp = employee(r)
    r.ask("ext4_choice", r.t("ext4_manager" if is_manager(emp) else "ext4_employee"), allowed=["1", "2"])


@step
def ext4_choice(r, answer):
    emp = employee(r)
    if answer == "1":
        return start_listening(r, emp)
    if is_manager(emp):
        return send_menu(r)
    record_message(r, {"type": "managers"})


def switch_user(r):
    r.s["employee_id"] = None
    r.s["data"]["own_phone"] = False
    r.s["data"]["attempts"] = 0
    r.say(r.t("switch_user"))
    ask_phone(r)


def settings_menu(r):
    emp = employee(r)
    txt, keys = r.t("settings_rest"), ["2", "3", "9", "0"]
    if emp["shuttle_group"]:
        txt, keys = r.t("settings_shuttle") + ". " + txt, ["1"] + keys
    r.ask("settings_choice", txt, allowed=keys)


@step
def settings_choice(r, answer):
    if answer == "1" and employee(r)["shuttle_group"]:
        return ask_shuttle_calls(r)
    if answer == "2":
        return r.ask("new_pin", r.t("new_pin"), digits=None, timeout=10)
    if answer == "3":
        menu = ". ".join(text(lang, "language_menu") for lang in LANGUAGES)
        return r.ask("language_set", menu, allowed=["1", "2", "3"])
    if answer == "9":
        return switch_user(r)
    main_menu(r)


@step
def new_pin(r, answer):
    if not (answer.isdigit() and 4 <= len(answer) <= 8):
        r.say(r.t("bad_login"))
        return r.ask("new_pin", r.t("new_pin"), digits=None, timeout=10)
    r.ask("new_pin_again", r.t("pin_again"), digits=None, timeout=10, first=answer)


@step
def new_pin_again(r, answer, first):
    if answer != first:
        r.say(r.t("pin_mismatch"))
        return r.ask("new_pin", r.t("new_pin"), digits=None, timeout=10)
    with db.connect() as c:
        c.execute("UPDATE employees SET pin=? WHERE id=?", (answer, r.s["employee_id"]))
    r.say(r.t("pin_saved"))
    main_menu(r)


@step
def language_set(r, answer):
    lang = {"1": "he", "2": "en", "3": "yi"}[answer]
    with db.connect() as c:
        c.execute("UPDATE employees SET language=? WHERE id=?", (lang, r.s["employee_id"]))
    r.s["data"]["lang"] = lang
    r.say(r.t("language_saved"))
    main_menu(r)


# -- 9 -> 1: listen

def start_listening(r, emp):
    since = (db.now() - timedelta(days=MESSAGE_HISTORY_DAYS)).isoformat()
    with db.connect() as c:
        rows = c.execute(
            "SELECT r.id FROM message_recipients r JOIN messages m ON m.id=r.message_id "
            "WHERE r.employee_id=? AND (r.listened_at IS NULL OR m.created_at>=?) "
            "ORDER BY (r.listened_at IS NOT NULL), m.created_at", (emp["id"], since)).fetchall()
    ids = [x["id"] for x in rows]
    if not ids:
        r.say(r.t("no_messages"))
        return main_menu(r)
    r.s["data"]["playlist"] = ids
    play(r, 0)


def say_when(r, dt):
    y, m, d = hebcal.from_gregorian(dt.date())
    return r.t("when", weekday=WEEKDAYS[r.lang][dt.weekday()], day=d,
               month=hebcal.month_name(m, y, "en" if r.lang == "en" else "he"), time=r.time(dt))


def play(r, i, header=True):
    playlist = r.s["data"].get("playlist", [])
    if i >= len(playlist):
        r.say(r.t("end_messages"))
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
        kind = r.t("msg_new" if row["listened_at"] is None else "msg_saved")
        r.say(r.t("msg_header", kind=kind, n=i + 1, total=len(playlist), sender=row["sender_name"],
                  when=say_when(r, db.parse(row["created_at"]))))
    r.file(row["recording_path"])
    r.ask("after_message", r.t("after_msg"), allowed=["1", "2"], i=i)


@step
def after_message(r, answer, i):
    if answer == "1":
        return play(r, i, header=False)
    play(r, i + 1)


# -- 9 -> 2: send

def send_menu(r):
    emp = employee(r)
    if not is_manager(emp):                                   # regular employees never send to staff
        return ext4(r)
    r.ask("send_choice", r.t("send_menu_tech" if emp["role"] == "tech_manager" else "send_menu_onsite"),
          allowed=["1", "2", "3", "4"])


@step
def send_choice(r, answer):
    emp = employee(r)
    if not is_manager(emp):
        return ext4(r)
    targets = {"1": {"type": "shift"}, "2": {"type": "group", "group": 1}, "3": {"type": "group", "group": 2}}
    if answer in targets:
        if not resolve_recipients(emp, targets[answer]):
            r.say(r.t("no_recipients"))
            return send_menu(r)
        return record_message(r, targets[answer])
    r.ask("send_employee", r.t("enter_target"), digits=None, timeout=10)


@step
def send_employee(r, answer):
    emp = employee(r)
    if not is_manager(emp):
        return ext4(r)
    with db.connect() as c:
        target = c.execute("SELECT * FROM employees WHERE emp_number=? AND active=1", (answer,)).fetchone()
    if target is None:
        r.say(r.t("target_not_found"))
        return send_menu(r)
    if emp["role"] == "onsite_manager" and target["shift"] != emp["shift"]:
        r.say(r.t("target_not_in_shift"))
        return send_menu(r)
    r.say(r.t("target_is", name=target["name"]))
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
    r.record("recorded", r.t("record"), file_name, target=target)


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
        r.say(r.t("empty_recording"))
        return main_menu(r)
    recipients = resolve_recipients(emp, target)
    if not recipients:
        r.say(r.t("no_recipients"))
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
        by_tzintuk = target["type"] in ("shift", "group")
        threading.Thread(target=notify, args=(msg_id, by_tzintuk), daemon=True).start()
    r.say(r.t("saved_to_managers" if target["type"] == "managers" else "saved_to_employees"))
    main_menu(r)


def notify(msg_id, by_tzintuk=False):
    """Tell each recipient a new message is waiting: a tzintuk (missed-call ring) for a group or everyone,
    a short voice call for one person or for the managers. Never SMS."""
    with db.connect() as c:
        rows = c.execute("SELECT rc.id, e.name, e.phone, e.language, s.name AS sender FROM message_recipients rc "
                         "JOIN employees e ON e.id=rc.employee_id JOIN messages m ON m.id=rc.message_id "
                         "JOIN employees s ON s.id=m.sender_id WHERE rc.message_id=?", (msg_id,)).fetchall()
    callable_rows = [x for x in rows if nbs.israeli(x["phone"] or "")]
    if not callable_rows:
        return
    if by_tzintuk:
        ok, info = nbs.run_tzintuk([x["phone"] for x in callable_rows])
    else:
        ok, info = nbs.run_tts_calls({x["phone"]: text(x["language"], "notify_call_text", name=x["name"],
                                                       sender=x["sender"]) for x in callable_rows})
    with db.connect() as c:
        c.execute("UPDATE messages SET campaign_id=? WHERE id=?", (info if ok else None, msg_id))
        c.executemany("UPDATE message_recipients SET delivery_status=? WHERE id=?",
                      [(("tzintuk" if by_tzintuk else "called") if ok else "call failed", x["id"])
                       for x in callable_rows])
