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

import assistant
import db
import hebcal
import nbs
import schedule
import corrections
import money
import moreshet
import tefila
import trips
from prompts import GROUPS, GROUPS_SHORT, LANGUAGES, WEEKDAYS, say_time, text

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
        return say_time(dt.astimezone(db.TZ) if dt.tzinfo else dt, self.lang)     # as the clock shows it then

    def say(self, txt):
        self.say_parts.append(tts(txt))
        return self

    def file(self, path):
        self.say_parts.append("f-" + path)
        return self

    def _messages(self):
        return ".".join(p for p in self.say_parts if p)

    def ask(self, step, prompt, digits=1, allowed=None, min_digits=1, timeout=7, **ctx):
        """Ask for keypad input; the answer comes back to `step`. digits=None: until #.
        Everywhere after sign-in: 0 in a one-key menu (when 0 has no other meaning there) and * in a typed
        entry go back to the main menu."""
        if prompt:
            self.say(prompt)
        name = self._next_name()
        keys = list(allowed) if allowed else None
        if keys and step not in NO_GLOBAL_BACK and "0" not in keys:
            keys.append("0")
        fields = [name, "no", str(digits or ""), str(min_digits), str(timeout), "No", "no", "no", "",
                  ".".join(keys) if keys else "", "1", "Ok", "None", ""]
        self.action = f"read={self._messages()}={','.join(fields)}"
        self.s["data"]["await"] = {"name": name, "step": step, "ctx": ctx,
                                   "again": {"prompt": prompt, "digits": digits, "allowed": allowed,
                                             "min_digits": min_digits, "timeout": timeout}}
        self.s["data"]["last_action"] = self.action
        return self

    def record(self, step, prompt, file_name, **ctx):
        self.say(prompt)
        name = self._next_name()
        # position 6 empty = the phone system's own menu after recording: listen, approve, record again
        fields = [name, "no", "record", RECORD_FOLDER, file_name, "", "yes", "no", "1", "180"]
        self.action = f"read={self._messages()}={','.join(fields)}"
        self.s["data"]["await"] = {"name": name, "step": step, "ctx": ctx}
        self.s["data"]["last_action"] = self.action
        return self

    def ask_voice(self, step, prompt, **ctx):
        """Speech recognition (the caller can also key digits). The answer is the text, or 'Digits-123'."""
        self.say(prompt)
        name = self._next_name()
        self.action = f"read={self._messages()}={name},no,voice,he-IL,,9"
        self.s["data"]["await"] = {"name": name, "step": step, "ctx": ctx}
        self.s["data"]["last_action"] = self.action
        return self

    def hangup(self):
        self.action = "go_to_folder=hangup"
        return self

    def _next_name(self):
        self.s["data"]["seq"] = self.s["data"].get("seq", 0) + 1
        return f"v{self.s['data']['seq']}"

    def announce(self, txt):
        """Played on its own before what follows, so it cannot be skipped by pressing a key."""
        self.pre = getattr(self, "pre", []) + [tts(txt)]
        return self

    def text(self):
        out = []
        if getattr(self, "pre", None):
            out.append("id_list_message=" + ".".join(self.pre))
        if self.say_parts and not (self.action or "").startswith("read="):
            out.append("id_list_message=" + self._messages())
        out.append(self.action or "go_to_folder=hangup")
        return "&".join(out)


NO_GLOBAL_BACK = {"main_choice", "login_phone", "login_pin", "prev_exit_answer", "fix_time", "uman_answer",
                  "waiting_choice", "recorded"}


def global_back(step, answer, again):
    """True when the caller asked to go back to the main menu (see Reply.ask)."""
    if step in NO_GLOBAL_BACK or answer is None:
        return False
    a = answer.replace("Digits-", "").strip()
    if a and set(a) <= {"*", "#"} and "*" in a:
        return True
    return bool(again) and again.get("digits") == 1 and a == "0" and "0" not in (again.get("allowed") or [])


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
            if global_back(waiting["step"], answer, waiting.get("again")) and r.s.get("employee_id"):
                main_menu(r)
            else:
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
    if schedule.removed_by_rule(emp, schedule.session_date(db.now())):
        return r.say(r.t("removed")).hangup()
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
    """Recognised by the main phone or by the additional phone."""
    with db.connect() as c:
        return [e for e in c.execute("SELECT * FROM employees WHERE active=1 AND (phone IS NOT NULL OR phone2 IS NOT NULL)")
                if phone in (db.normalize_phone(e["phone"]), db.normalize_phone(e["phone2"]))]


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
    menu_or_waiting(r)


def unheard(emp_id):
    with db.connect() as c:
        return c.execute("SELECT rc.id, s.name AS sender FROM message_recipients rc JOIN messages m ON m.id=rc.message_id "
                         "JOIN employees s ON s.id=m.sender_id WHERE rc.employee_id=? AND rc.listened_at IS NULL "
                         "AND rc.announced_at IS NULL AND m.channel='kollel' ORDER BY m.created_at DESC", (emp_id,)).fetchall()


def play_files(r, recording_path):
    """A message can hold several recordings one after the other ('/8/a|/8/b')."""
    for p in (recording_path or "").split("|"):
        if p:
            r.file(p)


def urgent_waiting(emp_id):
    with db.connect() as c:
        return c.execute("SELECT rc.id, m.recording_path, s.name AS sender FROM message_recipients rc "
                         "JOIN messages m ON m.id=rc.message_id JOIN employees s ON s.id=m.sender_id "
                         "WHERE rc.employee_id=? AND rc.listened_at IS NULL AND m.urgent=1 AND m.channel='kollel' "
                         "ORDER BY m.created_at", (emp_id,)).fetchall()


def menu_or_waiting(r):
    """Right after sign-in: an urgent message is played at once; other waiting messages are offered first."""
    urgent = urgent_waiting(r.s["employee_id"])
    if urgent:
        u = urgent[0]
        with db.connect() as c:
            c.execute("UPDATE message_recipients SET listened_at=?, announced_at=COALESCE(announced_at, ?) WHERE id=?",
                      (db.now().isoformat(), db.now().isoformat(), u["id"]))
        r.say(r.t("urgent_header", sender=u["sender"]))
        play_files(r, u["recording_path"])
        return r.ask("urgent_after", r.t("urgent_after"), allowed=["1", "2"], path=u["recording_path"])
    if not r.s["data"].get("waiting_asked"):
        r.s["data"]["waiting_asked"] = True
        waiting = unheard(r.s["employee_id"])
        if waiting:
            with db.connect() as c:                       # announced once only, on the first call after it was sent
                c.executemany("UPDATE message_recipients SET announced_at=? WHERE id=?",
                              [(db.now().isoformat(), w["id"]) for w in waiting])
            key = "waiting_one" if len(waiting) == 1 else "waiting_many"
            return r.ask("waiting_choice", r.t(key, sender=waiting[0]["sender"], n=len(waiting)), allowed=["1", "2"])
    main_menu(r)


@step
def urgent_after(r, answer, path):
    if answer == "1":
        play_files(r, path)
        return r.ask("urgent_after", r.t("urgent_after"), allowed=["1", "2"], path=path)
    menu_or_waiting(r)


@step
def waiting_choice(r, answer):
    if answer == "1":
        return start_listening(r, employee(r))
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
            cy, cm, hd = hebcal.from_gregorian(d)
            hy, hm = schedule.night_month_of(d)
            c.execute("INSERT INTO bonuses(employee_id, heb_year, heb_month, amount, note) VALUES (?,?,?,?,?)",
                      (emp["id"], hy, hm, amount, f"Uman {hd} {hebcal.month_name(cm, cy, 'he')} ({round(pct * 100)}%)"))
    r.say(r.t("uman_yes" if went else "uman_no"))
    if emp["shuttle_group"] and emp["shuttle_calls"] is None:
        r.say(r.t("shuttle_first"))
        return ask_shuttle_calls(r)
    menu_or_waiting(r)


def ask_shuttle_calls(r):
    r.ask("shuttle_calls_set", r.t("shuttle_calls_ask"), allowed=[str(i) for i in range(1, MAX_SHUTTLE_CALLS + 1)])


@step
def shuttle_calls_set(r, answer):
    n = int(answer)
    with db.connect() as c:
        c.execute("UPDATE employees SET shuttle_calls=? WHERE id=?", (n, r.s["employee_id"]))
    r.say(r.t("shuttle_calls_saved", n=n))
    menu_or_waiting(r)


# ---- main menu -----------------------------------------------------------

def main_menu(r):
    emp = employee(r)
    if emp is None:
        return ask_phone(r)
    if moreshet.is_member(emp) and not db.get_setting(f"moreshet_announced_{emp['id']}"):
        db.set_setting(f"moreshet_announced_{emp['id']}", db.now().isoformat())
        r.announce(r.t("moreshet_announce"))
    if moreshet.only_member(emp):
        return moreshet_menu(r)
    if money.route_now(emp) and not r.s["data"].get("money_routed"):
        r.s["data"]["money_routed"] = True                 # once a call: 0 then gives the usual menu
        return money_menu(r)
    txt, keys = r.t("menu_main"), ["1", "2", "4", "5", "6", "9", "*"]
    if emp["is_driver"]:
        txt += ". " + r.t("menu_driver")
        keys.append("3")
    rest = r.t("menu_rest")
    if tefila.campaign_day(emp) or (is_manager(emp) and tefila.manager_period()):
        rest += ". " + r.t("tefila_menu")
        keys.append(tefila.CAMPAIGN["key"])
    shown = set()
    for trip in trips.open_trips(emp):
        rest += ". " + trip["menu"].get(r.lang, trip["menu"]["he"])
        keys.append(trip["key"])
        shown.add(trip["key"])
    if moreshet.is_member(emp):
        rest += ". " + r.t("moreshet_menu_line")
        keys.append("0")
    if is_manager(emp):
        for trip in trips.recent_trips():
            if trip["key"] not in shown:
                rest += ". " + r.t("trip_status_menu", digit=trip["key"])
                keys.append(trip["key"])
                shown.add(trip["key"])
    r.ask("main_choice", txt + ". " + rest + ". " + r.t("menu_switch"), allowed=keys)


@step
def main_choice(r, answer):
    emp = employee(r)
    if emp is None:
        return ask_phone(r)
    if answer == "0" and moreshet.is_member(emp):
        return r.ask("moreshet_gate", r.t("moreshet_gate"), allowed=["0"], timeout=4)
    actions = {"1": clock_in, "2": early_exit, "3": shuttle, "4": ext4, "5": fix_start, "6": status,
               "9": settings_menu,
               "*": switch_user}
    if answer == "1" and schedule.in_night(emp) and schedule.bhz_notice_due(emp, db.now()):
        bhz_notice(r)
    if answer in ("1", "2") and not schedule.clock_in_open(emp, db.now()):
        r.say(r.t("closed_until", time=r.time(schedule.next_opening(emp, db.now()))))
        return main_menu(r)
    if answer in actions and (answer != "3" or emp["is_driver"]):
        return actions[answer](r)
    if answer == tefila.CAMPAIGN["key"] and (tefila.campaign_day(emp) or (is_manager(emp) and tefila.manager_period())):
        return tefila_start(r)
    if is_manager(emp):
        trip = next((t for t in trips.recent_trips() if t["key"] == answer), None)
        if trip:
            if trips.by_key(answer, emp):
                return r.ask("trip_manager_choice", r.t("trip_manager_menu"), allowed=["1", "2"], trip=trip["id"])
            return trip_status(r, trip)
    trip = trips.by_key(answer, emp)
    if trip:
        return trip_start(r, trip)
    r.say(r.t("invalid"))
    main_menu(r)


# ---- 00: Moreshet HaNachal (moreshet.py) -----------------------------------

@step
def moreshet_gate(r, answer):
    if answer == "0" and moreshet.is_member(employee(r)):
        return moreshet_menu(r)
    main_menu(r)


def _m_unheard(emp_id):
    with db.connect() as c:
        return c.execute("SELECT COUNT(*) FROM message_recipients rc JOIN messages m ON m.id=rc.message_id "
                         "WHERE rc.employee_id=? AND rc.listened_at IS NULL AND m.channel='moreshet'", (emp_id,)).fetchone()[0]


def moreshet_menu(r):
    me = employee(r)
    if not moreshet.is_member(me):
        return main_menu(r)
    n = _m_unheard(me["id"])
    txt = [r.t("moreshet_title")]
    if n:
        txt.append(r.t("moreshet_new", n=n))
    txt.append(r.t("moreshet_menu"))
    keys = ["1", "2", "7", "9"]
    if money.is_treasurer(me):
        txt.append(r.t("money_menu_line"))
        keys.append("8")
    if moreshet.only_member(me):
        txt.append(r.t("menu_switch"))
        keys.append("*")
    else:
        txt.append(r.t("moreshet_back"))
    r.ask("moreshet_choice", ". ".join(txt), allowed=keys)


@step
def moreshet_choice(r, answer):
    me = employee(r)
    if not moreshet.is_member(me):
        return main_menu(r)
    if answer == "1":
        return m_start_listening(r, me)
    if answer == "2":
        return m_send_menu(r)
    if answer == "8" and money.is_treasurer(me):
        return money_menu(r)
    if answer in ("7", "9"):
        kind = "expense" if answer == "7" else "income"
        return r.ask("m_amount", r.t("m_amount_" + kind), digits=None, timeout=15, kind=kind)
    if answer == "*" and moreshet.only_member(me):
        return switch_user(r)
    main_menu(r)


# -- listen: everything sent to him (to him alone or to a group), in the order it was left

def m_start_listening(r, me):
    since = (db.now() - timedelta(days=60)).isoformat()
    with db.connect() as c:
        ids = [x["id"] for x in c.execute(
            "SELECT rc.id FROM message_recipients rc JOIN messages m ON m.id=rc.message_id WHERE rc.employee_id=? "
            "AND m.channel='moreshet' AND (rc.listened_at IS NULL OR m.created_at>=?) ORDER BY m.created_at",
            (me["id"], since))]
    if not ids:
        r.say(r.t("no_messages"))
        return moreshet_menu(r)
    r.s["data"]["m_playlist"] = ids
    m_play(r, 0)


def m_play(r, i, header=True):
    ids = r.s["data"].get("m_playlist", [])
    if i >= len(ids):
        r.say(r.t("end_messages"))
        return moreshet_menu(r)
    with db.connect() as c:
        row = c.execute("SELECT rc.*, m.recording_path, m.created_at, s.name AS sender FROM message_recipients rc "
                        "JOIN messages m ON m.id=rc.message_id JOIN employees s ON s.id=m.sender_id WHERE rc.id=?",
                        (ids[i],)).fetchone()
        if row is None or row["employee_id"] != r.s["employee_id"]:
            return m_play(r, i + 1)
        c.execute("UPDATE message_recipients SET listened_at=COALESCE(listened_at, ?) WHERE id=?", (db.now().isoformat(), row["id"]))
    if header:
        t = db.parse(row["created_at"])
        y, m, d = hebcal.from_gregorian(t.date())
        r.say(r.t("m_header", sender=row["sender"], day=d, month=hebcal.month_name(m, y, "he"), time=r.time(t)))
    play_files(r, row["recording_path"])
    r.ask("m_after", r.t("after_msg"), allowed=["1", "2"], i=i)


@step
def m_after(r, answer, i):
    if answer == "1":
        return m_play(r, i, header=False)
    m_play(r, i + 1)


# -- leave a message

def m_send_menu(r):
    me = employee(r)
    opts = [r.t("m_to_mashgichim"), r.t("m_to_managers")]
    keys = ["1", "2"]
    for e in moreshet.members():
        if e["id"] != me["id"] and e["moreshet_key"] and e["moreshet_key"] not in keys + ["7"]:
            opts.append(r.t("m_to_person", name=moreshet.spoken_name(e), digit=e["moreshet_key"]))
            keys.append(e["moreshet_key"])
    opts.append(r.t("m_to_all"))
    keys.append("7")
    r.ask("m_send_choice", ". ".join(opts), allowed=keys)


@step
def m_send_choice(r, answer):
    me = employee(r)
    if not moreshet.is_member(me):
        return main_menu(r)
    if answer == "1":
        target = {"type": "m_mashgichim"}
    elif answer == "2":
        target = {"type": "m_managers"}
    elif answer == "7":
        target = {"type": "m_all"}
    else:
        who = next((e for e in moreshet.members() if e["moreshet_key"] == answer and e["id"] != me["id"]), None)
        if who is None:
            return m_send_menu(r)
        target = {"type": "m_person", "employee_id": who["id"]}
    if not moreshet.recipients(target, me["id"]):
        r.say(r.t("no_recipients"))
        return moreshet_menu(r)
    file_name = f"mor{db.now().strftime('%Y%m%d%H%M%S')}_{me['id']}"
    r.record("m_recorded", r.t("record"), file_name, target=target)


@step
def m_recorded(r, answer, target):
    me = employee(r)
    if not answer or answer in ("None", "0") or not moreshet.is_member(me):
        r.say(r.t("empty_recording"))
        return moreshet_menu(r)
    rcpts = moreshet.recipients(target, me["id"])
    if not rcpts:
        r.say(r.t("no_recipients"))
        return moreshet_menu(r)
    t = db.now()
    with db.connect() as c:
        msg_id = c.execute(
            "INSERT INTO messages(sender_id, created_at, heb_date, recipient_type, recipient_label, recording_path, channel) "
            "VALUES (?,?,?,?,?,?,'moreshet')", (me["id"], t.isoformat(), hebcal.format_date(t.date(), "he"), target["type"],
                                               moreshet.label(target, rcpts), recording_path(answer))).lastrowid
        c.executemany("INSERT INTO message_recipients(message_id, employee_id) VALUES (?,?)", [(msg_id, e["id"]) for e in rcpts])
    if NOTIFY_BY_CALL and nbs.enabled():
        threading.Thread(target=notify, args=(msg_id, False), daemon=True).start()
    r.say(r.t("saved_to_employees"))
    moreshet_menu(r)


# -- 7 expenses / 9 unusual income: amount, Hebrew date, what for

@step
def m_amount(r, answer, kind):
    amount = moreshet.parse_amount(answer)
    if amount is None:
        r.say(r.t("m_bad_amount"))
        return r.ask("m_amount", r.t("m_amount_" + kind), digits=None, timeout=15, kind=kind)
    r.ask("m_date", r.t("m_date"), digits=4, min_digits=4, timeout=10, kind=kind, amount=amount)


@step
def m_date(r, answer, kind, amount):
    d = moreshet.parse_heb_date(answer, db.now().date())
    if d is None:
        r.say(r.t("m_bad_date"))
        return r.ask("m_date", r.t("m_date"), digits=4, min_digits=4, timeout=10, kind=kind, amount=amount)
    file_name = f"mor{kind}{db.now().strftime('%Y%m%d%H%M%S')}_{r.s['employee_id']}"
    r.record("m_report_recorded", r.t("m_what_for"), file_name, kind=kind, amount=amount, day=d.isoformat())


@step
def m_report_recorded(r, answer, kind, amount, day):
    if not answer or answer in ("None", "0"):
        r.say(r.t("empty_recording"))
        return moreshet_menu(r)
    d = datetime.fromisoformat(day).date()
    y, m, dd = hebcal.from_gregorian(d)
    shek, ag = int(amount), round((amount - int(amount)) * 100)
    amt = r.t("m_shekels", n=shek) + (r.t("m_agorot", n=ag) if ag else "")
    r.ask("m_report_ok", r.t("m_confirm_" + kind, amount=amt, day=dd, month=hebcal.month_name(m, y, "he")),
          allowed=["1", "2"], kind=kind, amount=amount, day=day, rec=recording_path(answer))


@step
def m_report_ok(r, answer, kind, amount, day, rec):
    me = employee(r)
    if answer != "1" or not moreshet.is_member(me):
        r.say(r.t("m_cancelled"))
        return moreshet_menu(r)
    rid = moreshet.save_report(me["id"], kind, amount, datetime.fromisoformat(day).date(), rec)
    money.from_report(me, kind, amount, datetime.fromisoformat(day).date(), rid)
    r.say(r.t("m_saved"))
    moreshet_menu(r)


# -- 8 (the treasurer): money of the activity, confirming the monthly transfers (money.py)

def say_money(r, x):
    x = round(abs(x), 2)
    shek, ag = int(x), round((x - int(x)) * 100)
    return r.t("m_shekels", n=shek) + (r.t("m_agorot", n=ag) if ag else "")


def money_menu(r):
    me = employee(r)
    if not money.is_treasurer(me):
        return moreshet_menu(r)
    bal = money.balance()
    r.say(r.t("money_balance" if bal >= 0 else "money_balance_minus", amount=say_money(r, bal)))
    ps = money.pending()
    if not ps:
        r.say(r.t("money_nothing_due"))
        return moreshet_menu(r)
    cy = ps[0]
    who = ". ".join(r.t("money_due_person", name=x["name"], amount=say_money(r, x["amount"])) for x in cy["due"])
    r.say(r.t("money_due", who=who))
    if cy["paid"]:
        r.say(r.t("money_paid_left", paid=say_money(r, cy["paid"]), left=say_money(r, cy["left"])))
    r.ask("money_choice", r.t("money_menu", left=say_money(r, cy["left"])), allowed=["1", "2", "3"], cycle=cy["cycle"])


@step
def money_choice(r, answer, cycle):
    me = employee(r)
    if not money.is_treasurer(me):
        return main_menu(r)
    if answer == "1":
        money.record_transfer(cycle)
        r.say(r.t("money_all_saved"))
        return money_menu(r)
    if answer == "2":
        return r.ask("money_partial", r.t("money_partial_ask"), digits=None, timeout=15, cycle=cycle)
    if answer == "3":
        money.snooze()
        r.say(r.t("money_later"))
    moreshet_menu(r)


@step
def money_partial(r, answer, cycle):
    amount = moreshet.parse_amount(answer)
    cy = money.cycle(cycle)
    if amount is None or cy is None or amount > cy["left"] + 0.004:
        r.say(r.t("m_bad_amount"))
        return r.ask("money_partial", r.t("money_partial_ask"), digits=None, timeout=15, cycle=cycle)
    money.record_transfer(cycle, amount)
    money.snooze()
    r.say(r.t("money_partial_saved", amount=say_money(r, amount)))
    money_menu(r)


# ---- 8: Mivtza Tefila B'Koach (tefila.py) --------------------------------

def tefila_entries_text(r, n):
    if n == 0:
        return r.t("tefila_entries_none")
    return r.t("tefila_entries_one") if n == 1 else r.t("tefila_entries", n=n)


def tefila_start(r):
    """Key 8: 1 = register (only inside the window), 2 = how many entries I have, 3 = (managers) the status."""
    me = employee(r)
    is_open = tefila.is_open(me, call_start(r))
    keys, txt = ["2"], []
    if not is_open and tefila.campaign_day(me):
        txt.append(r.t("tefila_closed"))
    if is_open:
        keys.append("1")
        txt.append(r.t("tefila_menu_register"))
    if tefila.campaign_day(me):
        txt.append(r.t("tefila_menu_entries"))
    if is_manager(me) and tefila.manager_period():
        keys.append("3")
        txt.append(r.t("tefila_menu_status"))
    if tefila.campaign_day(me):
        keys.append("4")
        txt.append(r.t("tefila_menu_reminder"))
    if keys == ["2"] and not tefila.campaign_day(me):
        return main_menu(r)
    r.ask("tefila_choice", ". ".join(txt), allowed=keys, timeout=10)


@step
def tefila_choice(r, answer):
    me = employee(r)
    if answer == "3" and is_manager(me):
        return tefila_status(r)
    if answer == "2":
        r.say(tefila_entries_text(r, tefila.entries_of(me["id"])))
        return main_menu(r)
    if answer == "4" and tefila.campaign_day(me):
        now = me["tefila_reminder"]
        r.say(r.t("tefila_reminder_now", minutes=now) if now else r.t("tefila_reminder_none"))
        return r.ask("tefila_reminder_set", r.t("tefila_reminder_ask"), digits=None, timeout=10)
    if not tefila.is_open(me, call_start(r)):
        r.say(r.t("tefila_closed"))
        return main_menu(r)
    new, n = tefila.register(r.s["employee_id"], call_start(r))
    r.say(r.t("tefila_saved" if new else "tefila_already", entries=tefila_entries_text(r, n)))
    r.hangup()


@step
def tefila_reminder_set(r, answer):
    me = employee(r)
    a = (answer or "").strip("#")
    if not a.isdigit() or int(a) > tefila.REMINDER_MAX:
        r.say(r.t("invalid"))
        return r.ask("tefila_reminder_set", r.t("tefila_reminder_ask"), digits=None, timeout=10)
    if 0 < int(a) < tefila.REMINDER_MIN:
        r.say(r.t("tefila_reminder_min", minutes=tefila.REMINDER_MIN))
        return r.ask("tefila_reminder_set", r.t("tefila_reminder_ask"), digits=None, timeout=10)
    m = int(a)
    tefila.set_reminder(me["id"], m)
    if not m:
        r.say(r.t("tefila_reminder_off"))
    else:
        dav = tefila.davening(db.now().date() + timedelta(days=1))
        when = r.time(dav - timedelta(minutes=m)) if dav else ""
        r.say(r.t("tefila_reminder_saved", minutes=m) + (". " + r.t("tefila_reminder_tomorrow", time=when) if when else ""))
    main_menu(r)


@step
def tefila_manager_choice(r, answer):
    if answer == "1":
        return tefila_start(r)
    tefila_status(r)


def tefila_status(r):
    today = db.now().date().isoformat()
    with db.connect() as c:
        n_today = c.execute("SELECT COUNT(*) FROM tefila_entries WHERE campaign=? AND day=?",
                            (tefila.CAMPAIGN["id"], today)).fetchone()[0]
    tot = tefila.totals()
    r.say(r.t("tefila_status", today=n_today, entries=sum(x["entries"] for x in tot), people=len(tot)))
    r.ask("tefila_status_choice", r.t("tefila_status_menu"), allowed=["1", "2", "3", "4", "5", "0"])


@step
def tefila_status_choice(r, answer):
    if answer == "1":
        return pick_avrech(r, "tefila")
    if answer == "2":
        names = [x["name"] for x in tefila.entries_of_day(db.now().date().isoformat())]
        r.say(r.t("tefila_today_names", names=", ".join(names)) if names else r.t("tefila_today_none"))
        return tefila_status(r)
    if answer == "5":
        return r.ask("tefila_add_when", r.t("tefila_add_when"), allowed=["1", "2"])
    if answer == "3":
        return send_to(r, {"type": "tefila_today"})
    if answer == "4":
        return send_to(r, {"type": "tefila_all"})
    main_menu(r)


@step
def tefila_add_when(r, answer):
    day = db.now().date() - timedelta(days=0 if answer == "1" else 1)
    r.ask("tefila_add", r.t("enter_several"), digits=None, timeout=15, day=day.isoformat())


@step
def tefila_add(r, answer, day):
    me = employee(r)
    if not is_manager(me):
        return main_menu(r)
    people = [e for e in manager_scope(me) if schedule.in_night(e)]
    d = datetime.fromisoformat(day).date()
    for n in dict.fromkeys(x for x in re.split(r"[^0-9]+", answer or "") if x and x.strip("0")):
        e = find_by_any_number(n, people)
        if e is None:
            r.say(r.t("several_missing", numbers=say_digits(n)))
        elif tefila.add_manual(e["id"], d, me["id"]):
            r.say(r.t("tefila_added", name=e["name"]))
        else:
            r.say(r.t("tefila_already_there", name=e["name"]))
    r.ask("tefila_add_more", r.t("tefila_add_more"), allowed=["1", "2"], day=day)


@step
def tefila_add_more(r, answer, day):
    if answer == "1":
        return r.ask("tefila_add", r.t("enter_several"), digits=None, timeout=15, day=day)
    tefila_status(r)


def tefila_status_of(r, emp):
    with db.connect() as c:
        row = c.execute("SELECT COUNT(*) AS n, MAX(day) AS last FROM tefila_entries WHERE campaign=? AND employee_id=?",
                        (tefila.CAMPAIGN["id"], emp["id"])).fetchone()
    if not row["n"]:
        r.say(r.t("tefila_person_none", name=emp["name"]))
    else:
        last = datetime.fromisoformat(row["last"])
        y, m, d = hebcal.from_gregorian(last.date())
        r.say(r.t("tefila_person", name=emp["name"], n=row["n"],
                  last=f"{d} ב{hebcal.month_name(m, y, 'he')}"))
    tefila_status(r)


@step
def trip_manager_choice(r, answer, trip):
    t = trips.by_id(trip)
    if answer == "1" and t and trips.by_key(t["key"], employee(r)):
        return trip_start(r, t)
    trip_status(r, t)


def trip_status(r, trip):
    rows = trips.signups(trip["id"])
    parts = [r.t("trip_status_option", n=sum(1 for x in rows if x["choice"] == k), option=_lang(r, o))
             for k, o in trip["options"].items()]
    r.say(r.t("trip_status", n=len(rows), parts=", ".join(parts)))
    r.ask("trip_status_choice", r.t("trip_status_names_menu"), allowed=["1", "2", "0"], trip=trip["id"])


@step
def trip_status_choice(r, answer, trip):
    t = trips.by_id(trip)
    if answer == "1" and t:
        rows = trips.signups(t["id"])
        for k, o in t["options"].items():
            names = [x["name"] for x in rows if x["choice"] == k]
            if names:
                r.say(_lang(r, o) + ": " + ", ".join(names))
        if not rows:
            r.say(r.t("trip_none"))
        return trip_status(r, t)
    if answer == "2" and t:
        return send_to(r, {"type": "trip", "trip": t["id"]})
    main_menu(r)


# ---- temporary registration extensions (trips.py) ------------------------

def _lang(r, d):
    return d.get(r.lang, d["he"])


def trip_start(r, trip):
    mine = trips.signup_of(trip["id"], r.s["employee_id"])
    if mine:
        r.say(_lang(r, trip["already"]).format(option=_lang(r, trip["options"][mine["choice"]])))
    r.ask("trip_choice", _lang(r, trip["intro"]), allowed=list(trip["options"]), timeout=10, trip=trip["id"])


@step
def trip_choice(r, answer, trip):
    t = trips.by_id(trip)
    if t is None or db.now() >= t["until"] or not trips.for_employee(t, employee(r)):
        r.say(r.t("invalid"))
        return main_menu(r)
    if answer not in t["options"]:
        return trip_start(r, t)
    trips.register(t["id"], r.s["employee_id"], answer)
    r.say(_lang(r, t["saved"]).format(option=_lang(r, t["options"][answer])))
    main_menu(r)


def records_of_night(conn, emp_id, d, program="night"):
    rows = conn.execute("SELECT * FROM attendance WHERE employee_id=? AND program=? AND clock_in>=? ORDER BY clock_in",
                        (emp_id, program, (datetime.combine(d, datetime.min.time(), tzinfo=db.TZ)
                                           - timedelta(days=1)).isoformat())).fetchall()
    return [x for x in rows if schedule.session_date(db.parse(x["clock_in"])) == d]


# ---- 1: clock in ---------------------------------------------------------

def clock_in(r):
    emp, t0 = employee(r), call_start(r)
    program = schedule.program_now(emp, t0) or schedule.program_now(emp, db.now()) or "night"
    r.s["data"]["program"] = program
    tonight = schedule.session_date(t0)
    with db.connect() as c:
        mine = records_of_night(c, emp["id"], tonight, program)
        if mine:
            last = mine[-1]
            if not last["clock_out"] or db.parse(last["clock_out"]) > t0:       # still inside
                return r.say(r.t("already_in", time=r.time(db.parse(mine[0]["clock_in"])))).hangup()
            # he went out in the middle and came back: a second entry the same night (his exit is known)
            c.execute("UPDATE attendance SET exit_confirmed=1 WHERE id=?", (last["id"],))
            reentry = True
        else:
            reentry = False
        older = [] if reentry else c.execute(
            "SELECT * FROM attendance WHERE employee_id=? AND program=? AND exit_confirmed=0 "
            "ORDER BY clock_in DESC", (emp["id"], program)).fetchall()
        older = [x for x in older if schedule.session_date(db.parse(x["clock_in"])) < tonight]
        for x in older[1:]:                                    # only the latest night is asked about
            c.execute("UPDATE attendance SET exit_confirmed=1 WHERE id=?", (x["id"],))
    if older:
        return ask_prev_exit(r, older[0]["id"])
    finish_clock_in(r, reentry=reentry)


def bhz_notice(r):
    last = schedule.bhz_range_of(schedule.session_date(db.now()))[1]
    ly, lm, ld = hebcal.from_gregorian(last)
    r.say(r.t("bhz_notice", date=f"{ld} {hebcal.month_name(lm, ly, 'en' if r.lang == 'en' else 'he')}"))


def prev_exit_info(rid):
    with db.connect() as c:
        row = c.execute("SELECT * FROM attendance WHERE id=?", (rid,)).fetchone()
    d = schedule.session_date(db.parse(row["clock_in"]))
    current = db.parse(row["clock_out"]) if row["clock_out"] else schedule.default_exit(d, row["program"])
    return row, d, current


def ask_prev_exit(r, rid):
    row, d, current = prev_exit_info(rid)
    hy, hm, hd = hebcal.from_gregorian(d)
    date = f"{hd} ב{hebcal.month_name(hm, hy, 'he')}" if r.lang != "en" else f"{hd} {hebcal.month_name(hm, hy, 'en')}"
    # up to 4 digits: "1" alone is accepted after a short pause (or 1 then #)
    r.ask("prev_exit_answer", r.t("prev_exit", date=date, time=r.time(current)), digits=4, timeout=5, rid=rid)


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
    set_exit(rid, t)
    finish_clock_in(r)


def set_exit(rid, t):
    with db.connect() as c:
        c.execute("UPDATE attendance SET clock_out=?, exit_confirmed=1 WHERE id=?", (t.isoformat(), rid))


def finish_clock_in(r, reentry=False):
    """Clock in at the time the call started, not when the key was pressed.
    reentry: back after going out in the middle - recorded at the real time (no Meron rule)."""
    emp, t0 = employee(r), call_start(r)
    program = r.s["data"].get("program", "night")
    y, m, d = hebcal.from_gregorian(schedule.session_date(t0))
    rec, meron = schedule.recorded_clock_in(emp, t0) if program == "night" and not reentry else (t0, False)
    with db.connect() as c:
        c.execute("INSERT INTO attendance(employee_id, clock_in, heb_year, heb_month, heb_day, meron, actual_in, program) "
                  "VALUES (?,?,?,?,?,?,?,?)", (emp["id"], rec.isoformat(), y, m, d, int(meron), t0.isoformat(), program))
    if meron:
        r.say(r.t("entry_saved", time=r.time(rec)).replace(". בהצלחה", "").replace(". Good luck", ""))
        r.say(r.t("meron_saved")).say(r.t("good_luck"))
    else:
        r.say(r.t("entry_saved", time=r.time(rec)))
    r.hangup()


# ---- 2: early exit ------------------------------------------------------

def early_exit(r):
    emp, t = employee(r), db.now()
    program = schedule.program_now(emp, t) or "night"
    with db.connect() as c:
        mine = records_of_night(c, emp["id"], schedule.session_date(t), program)
        if not mine or (mine[-1]["clock_out"] and db.parse(mine[-1]["clock_out"]) <= t):   # not inside now
            r.say(r.t("not_in"))
            return main_menu(r)
        c.execute("UPDATE attendance SET clock_out=?, early_exit=1, exit_confirmed=0 WHERE id=?",
                  (t.isoformat(), mine[-1]["id"]))
    r.say(r.t("early_exit_saved", time=r.time(t))).say(r.t("goodbye")).hangup()


# ---- 3: shuttle departure (drivers) -------------------------------------

def shuttle(r):
    emp = employee(r)
    if not schedule.shuttle_open(db.now()):
        a, b = schedule.shuttle_window(db.now())
        r.say(r.t("shuttle_closed", start=r.time(a), end=r.time(b)))
        return main_menu(r)
    if not emp["shuttle_group"]:
        r.say(r.t("shuttle_no_group"))
        return main_menu(r)
    r.ask("shuttle_choice", r.t("shuttle_confirm", group=GROUPS_SHORT[r.lang].get(emp["shuttle_group"], emp["shuttle_group"])), allowed=["1", "2"])


def shuttle_waiting(driver, now=None):
    """His shuttle group (his shift): (waiting at the station = clocked in tonight, not coming tonight = did not)."""
    now = now or db.now()
    with db.connect() as c:
        rows = c.execute("SELECT * FROM employees WHERE active=1 AND id<>? AND shuttle_group=?",
                         (driver["id"], driver["shuttle_group"])).fetchall()
        # (a rider's clock-in at the station is written as the kollel start, which can still be ahead)
        came = {a["employee_id"] for a in c.execute("SELECT employee_id, clock_in FROM attendance WHERE program='night' "
                                                    "AND clock_in>=?", ((now - timedelta(hours=20)).isoformat(),)).fetchall()
                if schedule.session_date(db.parse(a["clock_in"])) == schedule.session_date(now)}
    if driver["shift"]:
        rows = [e for e in rows if e["shift"] == driver["shift"]]
    rows = [e for e in rows if schedule.in_night(e)]
    return [e for e in rows if e["id"] in came], [e for e in rows if e["id"] not in came]


def shuttle_recipients(driver):
    with db.connect() as c:
        rows = c.execute("SELECT * FROM employees WHERE active=1 AND id<>? AND shuttle_group=?",
                         (driver["id"], driver["shuttle_group"])).fetchall()
    if driver["shift"]:
        rows = [e for e in rows if e["shift"] == driver["shift"]]
    return [e for e in rows if nbs.israeli(e["phone"] or "")]


@step
def shuttle_choice(r, answer):
    if answer == "2":
        driver = employee(r)
        waiting, not_coming = shuttle_waiting(driver)
        r.say(r.t("shuttle_waiting", n=len(waiting), names=", ".join(e["name"] for e in waiting)) if waiting
              else r.t("shuttle_waiting_none"))
        if not_coming:
            r.say(r.t("shuttle_not_coming", n=len(not_coming)))
        return r.ask("shuttle_choice", r.t("shuttle_confirm", group=GROUPS_SHORT[r.lang].get(driver["shuttle_group"], driver["shuttle_group"])),
                     allowed=["1", "2"])
    if answer != "1":
        return main_menu(r)
    driver = employee(r)
    people = shuttle_recipients(driver)
    if not people:
        r.say(r.t("shuttle_nobody"))
        return driver_clock_in(r)
    texts = {e["phone"]: text(e["language"], "shuttle_call_text", name=e["name"],
                                    group=GROUPS_SHORT.get(e["language"], GROUPS_SHORT["he"]).get(driver["shuttle_group"], ""))
             for e in people}
    attempts = {e["phone"]: min(max(e["shuttle_calls"] or 1, 1), MAX_SHUTTLE_CALLS) for e in people}
    for e in people:                                       # the additional phone, if the avrech asked for it
        if e["phone2"] and e["phone2_mode"] in ("shuttle", "all") and nbs.israeli(e["phone2"]):
            texts[e["phone2"]] = texts[e["phone"]]
            attempts[e["phone2"]] = min(max(e["phone2_calls"] or 1, 1), MAX_SHUTTLE_CALLS)
    ok, info = nbs.run_tts_calls_with_attempts(texts, attempts)
    with db.connect() as c:
        c.execute("INSERT INTO shuttle_runs(driver_id, created_at, shuttle_group, recipients, campaign_id, status) "
                  "VALUES (?,?,?,?,?,?)", (driver["id"], db.now().isoformat(), driver["shuttle_group"],
                                           ", ".join(f"{e['name']} ({attempts[e['phone']]})" for e in people),
                                           info if ok else None, "sent" if ok else info))
    if not ok:
        return r.say(r.t("shuttle_failed")).hangup()
    r.say(r.t("shuttle_sent", count=len(people), group=GROUPS_SHORT[r.lang].get(driver["shuttle_group"], "")))
    driver_clock_in(r)


def driver_clock_in(r):
    """The driver confirming the shuttle also clocks in, exactly as if he had pressed 1."""
    if schedule.clock_in_open(employee(r), db.now()):
        return clock_in(r)
    r.say(r.t("goodbye")).hangup()


# ---- 5: attendance correction ------------------------------------------

def fix_start(r):
    me = employee(r)
    if is_manager(me):
        pending = pending_corrections(me)
        if pending:
            return r.ask("fix_manager_choice", r.t("fix_manager_menu", n=len(pending)), allowed=["1", "2"])
    r.ask("fix_kind", r.t("fix_kind"), allowed=["1", "2"])


def pending_corrections(me):
    scope = {e["id"] for e in manager_scope(me)}
    with db.connect() as c:
        rows = c.execute("SELECT * FROM corrections WHERE status='pending' ORDER BY created_at").fetchall()
    return [x["id"] for x in rows if x["employee_id"] in scope]


@step
def fix_manager_choice(r, answer):
    if answer == "2":
        return review_correction(r, pending_corrections(employee(r)), 0)
    r.ask("fix_kind", r.t("fix_kind"), allowed=["1", "2"])


CORRECTION_RESULTS = {"clock-in changed": "corr_in_changed", "clock-out set": "corr_out_set",
                      "night added (exit counts as 7:30 unless corrected)": "corr_added",
                      "clock-in would be after that day's clock-out": "corr_err_after_out",
                      "no clock-in that day before this time - approve a clock-in correction first": "corr_err_no_in"}


def review_correction(r, ids, i):
    if i >= len(ids):
        r.say(r.t("corr_none_left"))
        return main_menu(r)
    with db.connect() as c:
        k = c.execute("SELECT k.*, e.name FROM corrections k JOIN employees e ON e.id=k.employee_id WHERE k.id=?",
                      (ids[i],)).fetchone()
    if k is None or k["status"] != "pending":
        return review_correction(r, ids, i + 1)
    r.say(r.t("corr_item", n=i + 1, total=len(ids), name=k["name"],
              kind=r.t("fix_kind_in" if k["kind"] == "in" else "fix_kind_out"),
              when=say_when(r, db.parse(k["requested_time"]))))
    r.ask("review_choice", r.t("corr_menu"), allowed=["1", "2", "3", "0"], ids=ids, i=i)


@step
def review_choice(r, answer, ids, i):
    me = employee(r)
    if not is_manager(me) or answer == "0":
        return main_menu(r)
    if answer in ("1", "2") and ids[i] in pending_corrections(me):
        res = corrections.decide(ids[i], approve=answer == "1")
        if res:
            ok, msg = res
            r.say(r.t("corr_rejected") if answer == "2" else r.t(CORRECTION_RESULTS.get(msg.replace(" (Meron)", ""), "corr_in_changed")))
            if answer == "1" and ok and msg.endswith("(Meron)"):
                r.say(r.t("corr_meron"))
    review_correction(r, ids, i + 1)


def fix_program(emp, t):
    """Which kollel a correction is about: the only one he is in, else by the time of the call."""
    if not schedule.in_night(emp):
        return "day"
    if not schedule.in_day(emp):
        return "night"
    return schedule.program_now(emp, t) or ("day" if 12 <= t.hour < 20 else "night")


def fix_date(program, t, previous):
    if program == "night":
        return schedule.session_date(t) - timedelta(days=1 if previous else 0)
    d = t.date()
    if previous:                                   # the last Kollel Horaah before today
        d -= timedelta(days=1)
        for _ in range(60):
            if schedule.day_kollel_on(d):
                break
            d -= timedelta(days=1)
    return d


@step
def fix_kind(r, answer):
    day = fix_program(employee(r), call_start(r)) == "day"
    r.ask("fix_day", r.t("fix_day_day" if day else "fix_day"), allowed=["1", "2"], kind="in" if answer == "1" else "out")


@step
def fix_day(r, answer, kind):
    r.ask("fix_time", r.t("fix_time"), digits=4, min_digits=4, timeout=10, kind=kind, days_ago=0 if answer == "1" else 1)


@step
def fix_time(r, answer, kind, days_ago):
    t0 = call_start(r)
    d = fix_date(fix_program(employee(r), t0), t0, days_ago == 1)
    when = parse_hhmm(answer, d)
    if when is None:
        r.say(r.t("bad_time"))
        return r.ask("fix_time", r.t("fix_time"), digits=4, min_digits=4, timeout=10, kind=kind, days_ago=days_ago)
    with db.connect() as c:
        c.execute("INSERT INTO corrections(employee_id, created_at, kind, requested_time) VALUES (?,?,?,?)",
                  (r.s["employee_id"], db.now().isoformat(), kind, when.isoformat()))
    r.say(r.t("fix_saved", kind=r.t("fix_kind_in" if kind == "in" else "fix_kind_out"), time=r.time(when)))
    main_menu(r)


# ---- 6: personal status ---------------------------------------------------

def status(r):
    me = employee(r)
    if is_manager(me):
        return r.ask("status_who", r.t("status_manager_menu"), allowed=["1", "2", "3"])
    status_of(r, me)


@step
def status_who(r, answer):
    if answer == "2" and is_manager(employee(r)):
        return pick_avrech(r, "status")
    if answer == "3" and is_manager(employee(r)):
        return who_is_here(r)
    status_of(r, employee(r))


def who_is_here(r):
    program, here, away = present_now(manager_scope(employee(r)))
    kollel = r.t("kollel_night" if program == "night" else "kollel_day")
    if here:
        r.say(r.t("present_now", n=len(here), kollel=kollel, names=", ".join(e["name"] for e in here)))
    else:
        r.say(r.t("present_none", kollel=kollel))
    r.ask("who_here_choice", r.t("present_menu"), allowed=["1", "2", "0"])


@step
def who_here_choice(r, answer):
    if answer == "1":
        _p, _here, away = present_now(manager_scope(employee(r)))
        r.say(r.t("absent_now", n=len(away), names=", ".join(e["name"] for e in away)) if away else r.t("absent_none"))
        return r.ask("who_here_choice", r.t("present_menu"), allowed=["1", "2", "0"])
    if answer == "2":
        return send_to(r, {"type": "present"})
    main_menu(r)


def send_to(r, target):
    """Record a message to a target chosen outside the send menu (present now, registered...)."""
    if not resolve_recipients(employee(r), target):
        r.say(r.t("no_recipients"))
        return main_menu(r)
    record_message(r, target)


def status_of(r, emp, name=False):
    if name:
        r.say(r.t("status_of", name=emp["name"]))
    today = schedule.session_date(db.now())
    hy, hm, _ = hebcal.from_gregorian(today)
    month = hebcal.month_name(hm, hy, "en" if r.lang == "en" else "he")
    if schedule.in_day(emp):
        dsum = schedule.day_month_summary(emp, hy, hm, until=db.now().date())
        if dsum["vacation"]:
            r.say(r.t("status_day_vacation", month=month, amount=round(dsum["total"])))
        else:
            r.say(r.t("status_day", month=month, days=dsum["days"], held=dsum["held"], kept=dsum["kept"],
                      hours=round(dsum["hours"]), pct=round(dsum["percent"] * 100), amount=round(dsum["total"])))
    if not schedule.in_night(emp):
        return main_menu(r)
    if schedule.in_day(emp):
        r.say(r.t("status_night_title"))
    hy, hm = schedule.night_month_of(today)
    month = hebcal.month_name(hm, hy, "en" if r.lang == "en" else "he")
    s = schedule.month_summary(emp, hy, hm, until=today)
    pct = round(s["percent"] * 100)
    r.say(r.t("status_month", month=month, nights=s["nights"], whole=s["whole"], early=s["early"], pct=pct))
    r.say(r.t("status_stipend", amount=round(s["stipend"])))
    missing = []
    f = schedule.month_forecast(emp, hy, hm, db.now())
    for at, _amount in schedule.PERCENT_BONUSES:
        if f[at] is None:
            missing.append(r.t("status_percent_lost", pct=at))
        elif f[at] > 0:
            missing.append(r.t("status_to_percent", nights=f[at], pct=at, left=f["remaining"]))
    if not s["eligible"]:
        if f["min"] is None:
            missing.append(r.t("status_minimum_lost"))
        elif f["min"] > 0:
            missing.append(r.t("status_to_minimum", nights=f["min"], left=f["remaining"]))
    for line in missing:
        r.say(line)
    if schedule.is_rider(emp) or s["drives"]:
        r.say(r.t("status_meron", nights=s["meron_nights"]))
        if s["drives"]:
            r.say(r.t("status_driver", drives=s["drives"]))
        total = round(s["stipend"] + s["meron"] + s["driver"])
        r.say(r.t("status_total", amount=total))
    main_menu(r)


# ---- 4: messages / 9: personal settings, switch user ---------------------

def is_manager(emp):
    return emp["role"] in db.MANAGER_ROLES


def ext4(r):
    emp = employee(r)
    if is_manager(emp):
        return r.ask("ext4_choice", r.t("ext4_manager"), allowed=["1", "2", "3"])
    r.ask("ext4_choice", r.t("ext4_employee"), allowed=["1", "2"])


@step
def ext4_choice(r, answer):
    emp = employee(r)
    if answer == "1":
        return start_listening(r, emp)
    if answer == "3" and is_manager(emp):
        return start_sent(r, emp)
    if is_manager(emp):
        return send_menu(r)
    if emp["is_driver"] and emp["shuttle_group"]:
        return r.ask("driver_send_choice", r.t("driver_send_menu"), allowed=["1", "2"])
    record_message(r, {"type": "managers"})


@step
def driver_send_choice(r, answer):
    emp = employee(r)
    if answer == "2" and emp["is_driver"] and emp["shuttle_group"]:
        return record_message(r, {"type": "driver_group"})
    record_message(r, {"type": "managers"})


def switch_user(r):
    r.s["employee_id"] = None
    r.s["data"]["own_phone"] = False
    r.s["data"]["attempts"] = 0
    r.s["data"]["waiting_asked"] = False
    r.say(r.t("switch_user"))
    ask_phone(r)


def settings_menu(r):
    emp = employee(r)
    txt, keys = r.t("settings_rest"), ["2", "3", "4", "5", "0"]
    txt = r.t("settings_meron") + ". " + r.t("settings_phone2") + ". " + txt
    if emp["shuttle_group"]:
        txt, keys = r.t("settings_shuttle") + ". " + txt, ["1"] + keys
    if is_manager(emp):
        txt = r.t("settings_new_avrech") + ". " + r.t("settings_edit_avrech") + ". " + r.t("settings_gift") + ". " + \
            r.t("settings_voice") + ". " + txt
        keys += ["6", "7", "8", "9"]
    r.ask("settings_choice", txt, allowed=keys)


@step
def settings_choice(r, answer):
    if answer == "1" and employee(r)["shuttle_group"]:
        return ask_shuttle_calls(r)
    if answer == "4":
        return r.ask("meron_group_set", r.t("meron_menu", g1=GROUPS[r.lang][1], g2=GROUPS[r.lang][2]),
                     allowed=["1", "2", "0"])
    if answer == "5":
        return phone2_menu(r)
    if answer == "6" and is_manager(employee(r)):
        return r.ask("new_avrech_phone", r.t("new_avrech_phone"), digits=None, timeout=15)
    if answer == "7" and is_manager(employee(r)):
        return pick_avrech(r, "edit")
    if answer == "8" and is_manager(employee(r)):
        return pick_avrech(r, "gift")
    if answer == "9" and is_manager(employee(r)):
        return r.ask("voice_choice", r.t("voice_menu"), allowed=["1", "2"])
    if answer == "2":
        return r.ask("new_pin", r.t("new_pin"), digits=None, timeout=10)
    if answer == "3":
        menu = ". ".join(text(lang, "language_menu") for lang in LANGUAGES)
        keys = {"he": "1", "en": "2", "yi": "3"}
        return r.ask("language_set", menu, allowed=[keys[lang] for lang in LANGUAGES])
    main_menu(r)


# -- 9 -> 9: say what to do (assistant.py)

def _spoken(answer):
    a = (answer or "").strip()
    return "" if not a or a in ("None", "0") or a.startswith("Digits-") else a


@step
def voice_choice(r, answer):
    if not is_manager(employee(r)):
        return main_menu(r)
    if answer == "2":
        return r.ask_voice("voice_request", r.t("voice_say_request"))
    if not assistant.enabled():
        r.say(r.t("voice_no_key"))
        return r.ask("voice_choice", r.t("voice_menu"), allowed=["1", "2"])
    r.ask_voice("voice_action", r.t("voice_say_action"))


def _voice_retry(r, kind):
    r.ask("voice_retry", r.t("voice_again_menu"), allowed=["1", "0"], kind=kind)


@step
def voice_retry(r, answer, kind):
    if answer == "1" and is_manager(employee(r)):
        return r.ask_voice("voice_request" if kind == "request" else "voice_action",
                           r.t("voice_say_request" if kind == "request" else "voice_say_action"))
    main_menu(r)


@step
def voice_request(r, answer, text=""):
    """A request can be said in parts: each part is added to what was said before, until saved."""
    said = _spoken(answer)
    if said:
        text = f"{text} {said}".strip()
    elif not text:
        r.say(r.t("voice_not_heard_short"))
        return _voice_retry(r, "request")
    else:
        r.say(r.t("voice_not_heard_short"))
    r.ask("voice_request_ok", r.t("voice_request_confirm", text=text), allowed=["1", "2", "3"], text=text)


@step
def voice_request_ok(r, answer, text):
    if answer == "1":
        assistant.save_request(employee(r), text)
        r.say(r.t("voice_request_saved"))
        return main_menu(r)
    if answer == "2":
        return r.ask_voice("voice_request", r.t("voice_say_more"), text=text)
    if answer == "3":
        return r.ask_voice("voice_request", r.t("voice_say_request"))
    main_menu(r)


@step
def voice_action(r, answer):
    me = employee(r)
    said = _spoken(answer)
    if not said:
        r.say(r.t("voice_not_heard_short"))
        return _voice_retry(r, "action")
    scope = manager_scope(me)
    pend = pending_corrections(me)
    a = assistant.interpret(said, scope, pend)
    r.say(r.t("voice_heard", text=said))
    kind = a.get("action")
    ids = {e["id"]: e for e in scope}
    who = ids.get(_int(a.get("employee_id")))
    if kind in ("gift", "tefila_add", "status", "message_one") and who is None:
        r.say(r.t("voice_not_allowed") if a.get("employee_id") else r.t("voice_error"))
        return _voice_retry(r, "action")
    if kind == "gift":
        try:
            amount = round(float(a.get("amount")), 2)
        except (TypeError, ValueError):
            amount = 0
        if not 0 < amount <= 100000:
            r.say(r.t("voice_error"))
            return _voice_retry(r, "action")
        amount = int(amount) if amount == int(amount) else amount
        return r.ask("voice_ok", r.t("voice_confirm", what=r.t("voice_gift", amount=amount, name=who["name"])),
                     allowed=["1", "2"], act={"action": "gift", "eid": who["id"], "amount": amount})
    if kind == "tefila_add":
        day = assistant.parse_day(a.get("day"), db.now().date())
        if day is None or not (tefila.CAMPAIGN["first"] <= day <= tefila.CAMPAIGN["last"]):
            r.say(r.t("voice_error"))
            return _voice_retry(r, "action")
        return r.ask("voice_ok", r.t("voice_confirm", what=r.t("voice_tefila", name=who["name"], day=hebcal.format_date(day, "he"))),
                     allowed=["1", "2"], act={"action": "tefila_add", "eid": who["id"], "day": day.isoformat()})
    if kind == "correction":
        cid = _int(a.get("correction_id"))
        if cid not in pend:
            r.say(r.t("voice_error"))
            return _voice_retry(r, "action")
        with db.connect() as c:
            k = c.execute("SELECT k.*, e.name FROM corrections k JOIN employees e ON e.id=k.employee_id WHERE k.id=?", (cid,)).fetchone()
        approve = a.get("approve") is not False
        what = r.t("voice_corr_yes" if approve else "voice_corr_no", name=k["name"],
                   kind=r.t("fix_kind_in" if k["kind"] == "in" else "fix_kind_out"), when=say_when(r, db.parse(k["requested_time"])))
        return r.ask("voice_ok", r.t("voice_confirm", what=what), allowed=["1", "2"],
                     act={"action": "correction", "cid": cid, "approve": approve})
    if kind == "status":
        return status_of(r, who, name=True)
    if kind == "present":
        return who_is_here(r)
    if kind == "message" and a.get("to") in ("night", "day", "night_no_shuttle", "present", "managers"):
        return send_to(r, {"type": a["to"]})
    if kind == "message_one":
        return record_message(r, {"type": "employee", "employee_id": who["id"]})
    if kind == "request":
        txt = str(a.get("text") or said)
        return r.ask("voice_ok", r.t("voice_is_request", text=txt), allowed=["1", "2"], act={"action": "request", "text": said})
    if kind == "unclear" and a.get("question"):
        r.say(r.t("voice_unclear", question=str(a["question"])))
        return _voice_retry(r, "action")
    r.say(r.t("voice_error"))
    _voice_retry(r, "action")


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


@step
def voice_ok(r, answer, act):
    me = employee(r)
    if answer != "1" or not is_manager(me):
        r.say(r.t("voice_cancelled"))
        return main_menu(r)
    if act["action"] == "gift":
        return gift_ok(r, "1", act["eid"], act["amount"])
    if act["action"] == "tefila_add":
        if _target(r, act["eid"]) is None:
            return main_menu(r)
        tefila.add_manual(act["eid"], datetime.fromisoformat(act["day"]).date(), me["id"])
        r.say(r.t("voice_done"))
        return main_menu(r)
    if act["action"] == "correction":
        return review_choice(r, "1" if act["approve"] else "2", [act["cid"]], 0)
    if act["action"] == "request":
        assistant.save_request(me, act["text"])
        r.say(r.t("voice_request_saved"))
    main_menu(r)


# -- 9 -> 6: a manager registers a new avrech

def next_emp_number(prefix, existing):
    """The next free employee number: Kollel Chatzos numbers start with 0, Kollel Horaah numbers with 1.
    Same length as the existing ones of that kind (a longer number only when that length is full)."""
    used = {str(x) for x in existing}
    mine = [x for x in used if x.isdigit() and x.startswith(prefix) and len(x) <= 8]
    width = max((len(x) for x in mine), default=3)
    n = max((int(x) for x in mine), default=int(prefix) * 10 ** (width - 1)) + 1
    while True:
        cand = str(n).zfill(width)
        if len(cand) > width or not cand.startswith(prefix):     # this length is full: one digit longer
            width += 1
            if prefix != "0":
                n = int(prefix) * 10 ** (width - 1)
            continue
        if cand not in used:
            return cand
        n += 1


def find_by_number(number, people):
    """Exact employee number; otherwise the same number without leading zeros (15 -> 015), if only one fits."""
    exact = [e for e in people if e["emp_number"] == number]
    if exact:
        return exact[0]
    loose = [e for e in people if (e["emp_number"] or "").lstrip("0") == number.lstrip("0") and number.lstrip("0")]
    return loose[0] if len(loose) == 1 else None

def _phone_owner(phone):
    p = db.normalize_phone(phone or "")
    with db.connect() as c:
        for e in c.execute("SELECT * FROM employees"):
            if p and p in (db.normalize_phone(e["phone"] or ""), db.normalize_phone(e["phone2"] or "")):
                return e
    return None


@step
def new_avrech_phone(r, answer):
    if not is_manager(employee(r)):
        return main_menu(r)
    digits = re.sub(r"\D", "", answer or "")
    if not (9 <= len(digits) <= 10 and digits.startswith("0")):
        r.say(r.t("phone2_bad"))
        return r.ask("new_avrech_phone", r.t("new_avrech_phone"), digits=None, timeout=15)
    owner = _phone_owner(digits)
    if owner:
        r.say(r.t("new_avrech_phone_taken", name=owner["name"]))
        return settings_menu(r)
    r.ask("new_avrech_id", r.t("new_avrech_id"), digits=None, min_digits=0, timeout=15, new={"phone": digits})


@step
def new_avrech_id(r, answer, new):
    ident = re.sub(r"\D", "", answer or "") if answer not in (None, "None") else ""
    if ident.strip("0") == "":
        ident = ""
    if ident:
        with db.connect() as c:
            same = [e for e in c.execute("SELECT name, id_number FROM employees WHERE id_number IS NOT NULL")
                    if re.sub(r"\D", "", e["id_number"]).lstrip("0") == ident.lstrip("0")]
        if same:
            r.say(r.t("new_avrech_id_taken", name=same[0]["name"]))
            return settings_menu(r)
    new = dict(new, id_number=ident or None)
    r.ask("new_avrech_kollel", r.t("new_avrech_kollel"), allowed=["1", "2", "3"], new=new)


@step
def new_avrech_kollel(r, answer, new):
    new = dict(new, in_night=answer in ("1", "3"), in_day=answer in ("2", "3"))
    if new["in_night"]:
        return r.ask("new_avrech_group", r.t("new_avrech_group", g1=GROUPS_SHORT[r.lang][1], g2=GROUPS_SHORT[r.lang][2]),
                     allowed=["1", "2", "0"], new=new)
    ask_new_name(r, new)


@step
def new_avrech_group(r, answer, new):
    ask_new_name(r, dict(new, group=int(answer) if answer in ("1", "2") else None))


def ask_new_name(r, new):
    r.ask_voice("new_avrech_name", r.t("new_avrech_name"), new=new)


@step
def new_avrech_name(r, answer, new):
    name = (answer or "").strip()
    if not name or name.startswith("Digits") or name in ("None",) or any(ch.isdigit() for ch in name):
        r.say(r.t("new_avrech_name_missed"))
        return r.ask("new_avrech_name_ok", r.t("new_avrech_name_retry"), allowed=["2", "3"], new=new, name="")
    r.ask("new_avrech_name_ok", r.t("new_avrech_name_heard", name=name), allowed=["1", "2", "3"], new=new, name=name)


@step
def new_avrech_name_ok(r, answer, new, name):
    if answer == "2":
        return ask_new_name(r, new)
    if answer == "3" or not name:
        name = f"אברך חדש {new['phone']}"
    me = employee(r)
    if not is_manager(me) or _phone_owner(new["phone"]):
        return main_menu(r)
    with db.connect() as c:
        number = next_emp_number("0" if new["in_night"] else "1", [x[0] for x in c.execute("SELECT emp_number FROM employees")])
        shift = me["shift"] if me["role"] == "onsite_manager" and new["in_night"] else None
        c.execute("INSERT INTO employees(emp_number, name, phone, pin, role, shift, shuttle_group, id_number, in_night, in_day, "
                  "language, joined_on) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                  (number, name, new["phone"], "1480", "employee", shift, new.get("group"), new["id_number"],
                   1 if new["in_night"] else 0, 1 if new["in_day"] else 0, "he", db.now().date().isoformat()))
    r.say(r.t("new_avrech_saved", name=name, number=say_digits(number)))
    main_menu(r)


# -- 9 -> 7: a manager edits an avrech's details

def _target(r, eid):
    me = employee(r)
    if not is_manager(me):
        return None
    with db.connect() as c:
        t = c.execute("SELECT * FROM employees WHERE id=?", (eid,)).fetchone()
    return t if t and t["id"] in {e["id"] for e in manager_scope(me)} else None


def edit_menu(r, eid):
    t = _target(r, eid)
    if t is None:
        return main_menu(r)
    kollel = r.t("edit_kollel_both" if t["in_night"] and t["in_day"] else
                 "edit_kollel_day" if t["in_day"] else "edit_kollel_night")
    r.say(r.t("edit_details", name=t["name"], phone=say_digits(t["phone"] or ""), number=say_digits(t["emp_number"]),
              kollel=kollel))
    r.ask("edit_choice", r.t("edit_menu"), allowed=["1", "2", "3", "4", "5", "6", "0"], eid=eid)


@step
def edit_choice(r, answer, eid):
    t = _target(r, eid)
    if t is None or answer == "0":
        return main_menu(r)
    if answer == "1":
        return r.ask("edit_phone", r.t("edit_enter_phone"), digits=None, timeout=15, eid=eid)
    if answer == "2":
        return r.ask("edit_id", r.t("edit_enter_id"), digits=None, timeout=15, eid=eid)
    if answer == "3":
        return r.ask("edit_kollel", r.t("new_avrech_kollel"), allowed=["1", "2", "3"], eid=eid)
    if answer == "4":
        return r.ask("edit_group", r.t("new_avrech_group", g1=GROUPS_SHORT[r.lang][1], g2=GROUPS_SHORT[r.lang][2]),
                     allowed=["1", "2", "0"], eid=eid)
    if answer == "5":
        return r.ask_voice("edit_name", r.t("new_avrech_name"), eid=eid)
    with db.connect() as c:
        c.execute("UPDATE employees SET pin='1480' WHERE id=?", (eid,))
    r.say(r.t("edit_pin_reset"))
    edit_menu(r, eid)


def _edit_saved(r, eid, field, value):
    with db.connect() as c:
        c.execute(f"UPDATE employees SET {field}=? WHERE id=?", (value, eid))
    r.say(r.t("edit_saved"))
    edit_menu(r, eid)


@step
def edit_phone(r, answer, eid):
    if _target(r, eid) is None:
        return main_menu(r)
    digits = re.sub(r"\D", "", answer or "")
    if not (9 <= len(digits) <= 10 and digits.startswith("0")):
        r.say(r.t("phone2_bad"))
        return edit_menu(r, eid)
    owner = _phone_owner(digits)
    if owner and owner["id"] != eid:
        r.say(r.t("new_avrech_phone_taken", name=owner["name"]))
        return edit_menu(r, eid)
    _edit_saved(r, eid, "phone", digits)


@step
def edit_id(r, answer, eid):
    if _target(r, eid) is None:
        return main_menu(r)
    ident = re.sub(r"\D", "", answer or "")
    if ident.strip("0") == "":
        return _edit_saved(r, eid, "id_number", None)
    with db.connect() as c:
        same = [e for e in c.execute("SELECT id, name, id_number FROM employees WHERE id_number IS NOT NULL AND id<>?", (eid,))
                if re.sub(r"\D", "", e["id_number"]).lstrip("0") == ident.lstrip("0")]
    if same:
        r.say(r.t("new_avrech_id_taken", name=same[0]["name"]))
        return edit_menu(r, eid)
    _edit_saved(r, eid, "id_number", ident)


@step
def edit_kollel(r, answer, eid):
    if _target(r, eid) is None:
        return main_menu(r)
    with db.connect() as c:
        c.execute("UPDATE employees SET in_night=?, in_day=? WHERE id=?",
                  (1 if answer in ("1", "3") else 0, 1 if answer in ("2", "3") else 0, eid))
    r.say(r.t("edit_saved"))
    edit_menu(r, eid)


@step
def edit_group(r, answer, eid):
    if _target(r, eid) is None:
        return main_menu(r)
    _edit_saved(r, eid, "shuttle_group", int(answer) if answer in ("1", "2") else None)


@step
def edit_name(r, answer, eid):
    name = (answer or "").strip()
    if not name or name.startswith("Digits") or any(ch.isdigit() for ch in name):
        r.say(r.t("new_avrech_name_missed"))
        return edit_menu(r, eid)
    r.ask("edit_name_ok", r.t("edit_name_heard", name=name), allowed=["1", "2"], eid=eid, name=name)


@step
def edit_name_ok(r, answer, eid, name):
    if _target(r, eid) is None:
        return main_menu(r)
    if answer != "1":
        return r.ask_voice("edit_name", r.t("new_avrech_name"), eid=eid)
    _edit_saved(r, eid, "name", name)


# -- 9 -> 8: a separate bonus (not part of the stipend)

@step
def gift_amount(r, answer, eid):
    t = _target(r, eid)
    amount = int(re.sub(r"\D", "", answer or "") or 0)
    if t is None or amount <= 0:
        return main_menu(r)
    r.ask("gift_ok", r.t("gift_confirm", amount=amount, name=t["name"]), allowed=["1", "2"], eid=eid, amount=amount)


@step
def gift_ok(r, answer, eid, amount):
    t = _target(r, eid)
    if t is None or answer != "1":
        r.say(r.t("gift_cancelled"))
        return main_menu(r)
    hy, hm = schedule.night_month_of(schedule.session_date(db.now()))
    with db.connect() as c:
        c.execute("INSERT INTO gifts(employee_id, heb_year, heb_month, amount, note, created_at, added_by) VALUES (?,?,?,?,?,?,?)",
                  (eid, hy, hm, amount, "added by phone", db.now().isoformat(), r.s["employee_id"]))
    r.say(r.t("gift_saved", amount=amount, name=t["name"], month=hebcal.month_name(hm, hy, "he")))
    main_menu(r)


# -- 9 -> 5: additional phone

def say_digits(p):
    return " ".join(p)


def phone2_menu(r):
    emp = employee(r)
    if not emp["phone2"]:
        return r.ask("phone2_set", r.t("phone2_enter"), digits=None, timeout=12)
    mode = r.t({"none": "phone2_mode_none", "shuttle": "phone2_mode_shuttle", "all": "phone2_mode_all"}[emp["phone2_mode"] or "none"])
    r.ask("phone2_choice", r.t("phone2_menu", phone=say_digits(emp["phone2"]), mode=mode), allowed=["1", "2", "3", "0"])


@step
def phone2_choice(r, answer):
    if answer == "1":
        return r.ask("phone2_set", r.t("phone2_enter"), digits=None, timeout=12)
    if answer == "2":
        return ask_phone2_mode(r)
    if answer == "3":
        with db.connect() as c:
            c.execute("UPDATE employees SET phone2=NULL, phone2_mode='none' WHERE id=?", (r.s["employee_id"],))
        r.say(r.t("phone2_deleted"))
    main_menu(r)


@step
def phone2_set(r, answer):
    p = db.normalize_phone(answer)
    if not nbs.israeli(p):
        r.say(r.t("phone2_bad"))
        return r.ask("phone2_set", r.t("phone2_enter"), digits=None, timeout=12)
    if any(e["id"] != r.s["employee_id"] for e in find_by_phone(p)):
        r.say(r.t("phone2_taken"))
        return main_menu(r)
    with db.connect() as c:
        c.execute("UPDATE employees SET phone2=? WHERE id=?", (p, r.s["employee_id"]))
    r.say(r.t("phone2_saved", phone=say_digits(p)))
    ask_phone2_mode(r)


def ask_phone2_mode(r):
    r.ask("phone2_mode_set", r.t("phone2_mode_ask"), allowed=["1", "2", "0"])


@step
def phone2_mode_set(r, answer):
    mode = {"1": "shuttle", "2": "all", "0": "none"}[answer]
    with db.connect() as c:
        c.execute("UPDATE employees SET phone2_mode=? WHERE id=?", (mode, r.s["employee_id"]))
    if mode == "none":
        r.say(r.t("phone2_mode_saved_none"))
        return main_menu(r)
    r.ask("phone2_calls_set", r.t("phone2_calls_ask"), allowed=[str(n) for n in range(1, MAX_SHUTTLE_CALLS + 1)])


@step
def phone2_calls_set(r, answer):
    with db.connect() as c:
        c.execute("UPDATE employees SET phone2_calls=? WHERE id=?", (int(answer), r.s["employee_id"]))
    r.say(r.t("phone2_calls_saved", n=answer))
    main_menu(r)


@step
def meron_group_set(r, answer):
    group = int(answer) or None
    with db.connect() as c:
        c.execute("UPDATE employees SET shuttle_group=? WHERE id=?", (group, r.s["employee_id"]))
    if group is None:
        r.say(r.t("meron_removed"))
        return main_menu(r)
    r.say(r.t("meron_group_saved", group=GROUPS_SHORT[r.lang][group]))
    if employee(r)["shuttle_calls"] is None:
        return ask_shuttle_calls(r)
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
            "WHERE r.employee_id=? AND m.channel='kollel' AND (r.listened_at IS NULL OR m.created_at>=?) "
            "ORDER BY (r.listened_at IS NOT NULL), m.created_at DESC", (emp["id"], since)).fetchall()
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
    play_files(r, row["recording_path"])
    r.ask("after_message", r.t("after_msg"), allowed=["1", "2"], i=i)


@step
def after_message(r, answer, i):
    if answer == "1":
        return play(r, i, header=False)
    play(r, i + 1)


def start_sent(r, emp):
    since = (db.now() - timedelta(days=MESSAGE_HISTORY_DAYS)).isoformat()
    with db.connect() as c:
        ids = [x["id"] for x in c.execute("SELECT id FROM messages WHERE sender_id=? AND created_at>=? AND channel='kollel' "
                                          "ORDER BY created_at DESC", (emp["id"], since))]
    if not ids:
        r.say(r.t("no_sent"))
        return main_menu(r)
    r.s["data"]["sentlist"] = ids
    play_sent(r, 0)


def play_sent(r, i, header=True):
    ids = r.s["data"].get("sentlist", [])
    if i >= len(ids):
        r.say(r.t("end_messages"))
        return main_menu(r)
    with db.connect() as c:
        m = c.execute("SELECT * FROM messages WHERE id=? AND sender_id=?", (ids[i], r.s["employee_id"])).fetchone()
        if m is None:
            return play_sent(r, i + 1)
        total, heard = c.execute("SELECT COUNT(*), COUNT(listened_at) FROM message_recipients WHERE message_id=? "
                                 "AND employee_id<>(SELECT sender_id FROM messages WHERE id=message_id)",
                                 (m["id"],)).fetchone()
    if header:
        r.say(r.t("sent_header", n=i + 1, total_msgs=len(ids), label=m["recipient_label"] or "",
                  when=say_when(r, db.parse(m["created_at"])), heard=heard, total=total))
    play_files(r, m["recording_path"])
    r.ask("after_sent", r.t("after_msg"), allowed=["1", "2"], i=i)


@step
def after_sent(r, answer, i):
    play_sent(r, i, header=answer != "1") if answer == "1" else play_sent(r, i + 1)


# -- 9 -> 2: send

def send_menu(r):
    emp = employee(r)
    if not is_manager(emp):                                   # regular employees never send to staff
        return ext4(r)
    r.ask("send_choice", r.t("send_menu"), allowed=["1", "2", "3", "4", "5", "6", "7", "8", "0"])


@step
def send_choice(r, answer):
    emp = employee(r)
    if not is_manager(emp):
        return ext4(r)
    if answer == "0":
        return main_menu(r)
    targets = {"1": {"type": "night"}, "2": {"type": "day"}, "3": {"type": "night_no_shuttle"}, "8": {"type": "present"},
               "4": {"type": "group", "group": 1}, "5": {"type": "group", "group": 2}}
    if answer in targets:
        if not resolve_recipients(emp, targets[answer]):
            r.say(r.t("no_recipients"))
            return send_menu(r)
        return record_message(r, targets[answer])
    if answer == "7":
        return r.ask("several_how", r.t("several_how"), allowed=["1", "2"], ids=[])
    pick_avrech(r, "message")


def find_by_any_number(digits, people):
    """Employee number (short) or ID number (7+ digits)."""
    one = find_by_number(digits, people) if len(digits) < 7 else None
    if one is None and len(digits) >= 5:
        ids = [e for e in people if e["id_number"] and re.sub(r"\D", "", e["id_number"]).lstrip("0") == digits.lstrip("0")]
        one = ids[0] if len(ids) == 1 else None
    return one


@step
def several_how(r, answer, ids):
    if answer == "2":
        return r.ask_voice("several_voice", r.t("say_name_only"), ids=ids)
    ask_several(r, ids)


def ask_several(r, ids):
    r.ask("send_several", r.t("enter_several"), digits=None, timeout=15, ids=ids)


@step
def send_several(r, answer, ids=None):
    """Several avrechim: employee / ID numbers with a star between them; 0 = say a name instead."""
    ids = list(ids or [])
    me = employee(r)
    if not is_manager(me):
        return main_menu(r)
    numbers = [x for x in re.split(r"[^0-9]+", answer or "") if x and x.strip("0")]
    people = manager_scope(me)
    for n in dict.fromkeys(numbers):
        e = find_by_any_number(n, people)
        if e is None:
            r.say(r.t("several_missing", numbers=say_digits(n)))
        else:
            ids = _several_add(r, ids, e)
    several_menu(r, ids)


def _several_add(r, ids, e):
    if e["id"] not in ids:
        ids = ids + [e["id"]]
        r.say(r.t("several_added", name=e["name"], n=len(ids)))
    return ids


def several_menu(r, ids):
    if not ids:
        return ask_several(r, [])
    r.ask("send_several_ok", r.t("several_confirm"), allowed=["1", "2", "3", "9", "0"], ids=ids)


@step
def several_voice(r, answer, ids):
    me = employee(r)
    if not is_manager(me):
        return main_menu(r)
    ans = (answer or "").strip()
    people = manager_scope(me)
    if ans.startswith("Digits-") or ans.isdigit():
        e = find_by_any_number(ans.replace("Digits-", ""), people)
        found = [e] if e else []
    else:
        found = match_names(ans, people)
    if not found or len(found) > 6:
        r.say(r.t("avrech_not_found") if not found else r.t("avrech_too_many", n=len(found)))
        return several_menu(r, ids)
    if len(found) == 1:
        return several_menu(r, _several_add(r, ids, found[0]))
    opts = ". ".join(r.t("avrech_option", name=e["name"], n=k + 1) for k, e in enumerate(found))
    r.ask("several_pick", opts + ". " + r.t("avrech_again"), allowed=[str(k + 1) for k in range(len(found))] + ["0"],
          ids=ids, options=[e["id"] for e in found])


@step
def several_pick(r, answer, ids, options):
    if answer == "0" or not answer.isdigit() or int(answer) > len(options):
        return r.ask_voice("several_voice", r.t("say_name_only"), ids=ids)
    me = employee(r)
    e = next((x for x in manager_scope(me) if x["id"] == options[int(answer) - 1]), None) if is_manager(me) else None
    several_menu(r, _several_add(r, ids, e) if e else ids)


@step
def send_several_ok(r, answer, ids):
    if answer == "2":
        return ask_several(r, ids)
    if answer == "3":
        return r.ask_voice("several_voice", r.t("say_name_only"), ids=ids)
    if answer == "9":
        return r.ask("several_how", r.t("several_how"), allowed=["1", "2"], ids=[])
    if answer == "0":
        return main_menu(r)
    with db.connect() as c:
        names = [c.execute("SELECT name FROM employees WHERE id=?", (x,)).fetchone()[0] for x in ids]
    r.say(r.t("several_chosen", n=len(ids), names=", ".join(names)))
    record_message(r, {"type": "employees", "ids": ids})


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


_FINALS = str.maketrans("ךםןףץ", "כמנפצ")


def _norm_words(text_):
    text_ = re.sub(r"[\u0591-\u05C7'\"`׳״\-.,]", " ", text_ or "")
    return [w.translate(_FINALS) for w in text_.split() if w]


def match_names(spoken, people):
    """Avrechim whose name fits what was said: every word said (2+ letters) that fits a word of the name counts."""
    import difflib
    said = [w for w in _norm_words(spoken) if len(w) >= 2 and w not in ("הרב", "רבי", "ר")]
    if not said:
        return []
    scored = []
    for e in people:
        words = _norm_words(e["name"])
        score = 0.0
        for w in said:
            best = max((1.0 if w == x else difflib.SequenceMatcher(None, w, x).ratio() for x in words), default=0)
            if best >= 0.75:
                score += best
        if score:
            scored.append((score, e))
    if not scored:
        return []
    top = max(sc for sc, _ in scored)
    return [e for sc, e in sorted(scored, key=lambda x: (-x[0], x[1]["name"])) if sc >= top - 0.25]


def pick_avrech(r, purpose, tries=0):
    r.ask_voice("picked_avrech", r.t("say_name"), purpose=purpose, tries=tries)


@step
def picked_avrech(r, answer, purpose, tries):
    me = employee(r)
    if not is_manager(me):
        return main_menu(r)
    scope = manager_scope(me)
    ans = (answer or "").strip()
    digits = ans[len("Digits-"):] if ans.startswith("Digits-") else (ans if ans.isdigit() else None)
    if digits:
        one = find_by_any_number(digits, scope)
        found = [one] if one else []
    else:
        found = match_names(ans, scope)
    if not found:
        r.say(r.t("avrech_not_found"))
        return pick_avrech(r, purpose, tries + 1) if tries < 2 else main_menu(r)
    if len(found) > 6:
        r.say(r.t("avrech_too_many", n=len(found)))
        return pick_avrech(r, purpose, tries + 1) if tries < 2 else main_menu(r)
    ids = [e["id"] for e in found]
    if len(found) == 1:
        return r.ask("avrech_choice", r.t("avrech_confirm", name=found[0]["name"]), allowed=["1", "2"],
                     purpose=purpose, ids=ids + [0])
    opts = ". ".join(r.t("avrech_option", name=e["name"], n=i + 1) for i, e in enumerate(found))
    r.ask("avrech_choice", opts + ". " + r.t("avrech_again"), allowed=[str(i + 1) for i in range(len(found))] + ["0"],
          purpose=purpose, ids=ids)


@step
def avrech_choice(r, answer, purpose, ids):
    if len(ids) == 2 and ids[1] == 0:                     # one name: 1 = yes, 2 = search again
        if answer != "1":
            return pick_avrech(r, purpose)
        chosen = ids[0]
    elif answer == "0" or not answer.isdigit() or int(answer) > len(ids):
        return pick_avrech(r, purpose)
    else:
        chosen = ids[int(answer) - 1]
    me = employee(r)
    with db.connect() as c:
        target = c.execute("SELECT * FROM employees WHERE id=? AND active=1", (chosen,)).fetchone()
    if target is None or not is_manager(me) or target["id"] not in {e["id"] for e in manager_scope(me)}:
        return main_menu(r)
    if purpose == "status":
        return status_of(r, target, name=True)
    if purpose == "tefila":
        return tefila_status_of(r, target)
    if purpose == "edit":
        return edit_menu(r, target["id"])
    if purpose == "gift":
        r.say(r.t("target_is", name=target["name"]))
        return r.ask("gift_amount", r.t("gift_amount"), digits=None, timeout=15, eid=target["id"])
    r.say(r.t("target_is", name=target["name"]))
    record_message(r, {"type": "employee", "employee_id": target["id"]})


def resolve_recipients(sender, target):
    with db.connect() as c:
        everyone = c.execute("SELECT * FROM employees WHERE active=1 AND id<>?", (sender["id"],)).fetchall()
    t = target["type"]
    if t == "managers":
        tech = [e for e in everyone if e["role"] == "tech_manager"]
        out = list(tech)
        if schedule.in_night(sender):
            onsite = [e for e in everyone if e["role"] == "onsite_manager" and schedule.in_night(e)]
            same_shift = [e for e in onsite if e["shift"] and e["shift"] == sender["shift"]]
            out += same_shift or onsite
        if schedule.in_day(sender):
            out += [e for e in everyone if e["role"] == "onsite_manager" and schedule.in_day(e) and e not in out]
        return out
    if t == "driver_group":
        if not (sender["is_driver"] and sender["shuttle_group"]):
            return []
        group = [e for e in everyone if e["shuttle_group"] == sender["shuttle_group"]
                 and (not sender["shift"] or e["shift"] == sender["shift"])]
        drivers = [e for e in everyone if e["is_driver"] and e not in group]
        return group + drivers
    if not is_manager(sender):
        return []
    scope = manager_scope(sender, everyone)
    if t == "shift":
        return scope
    if t == "night":
        return [e for e in scope if schedule.in_night(e)]
    if t == "day":
        return [e for e in scope if schedule.in_day(e)]
    if t == "night_no_shuttle":
        return [e for e in scope if schedule.in_night(e) and not e["shuttle_group"] and not e["is_driver"]]
    if t == "group":
        return [e for e in scope if e["shuttle_group"] == target["group"]]
    if t == "employee":
        return [e for e in scope if e["id"] == target["employee_id"]]
    if t == "employees":
        return [e for e in scope if e["id"] in set(target["ids"])]
    if t == "present":
        return present_now(scope)[1]
    if t == "trip":
        ids = {x["employee_id"] for x in trips.signups(target["trip"])}
        return [e for e in scope if e["id"] in ids]
    if t in ("tefila_today", "tefila_all"):
        with db.connect() as c:
            q = "SELECT DISTINCT employee_id FROM tefila_entries WHERE campaign=?" + (" AND day=?" if t == "tefila_today" else "")
            args = (tefila.CAMPAIGN["id"],) + ((db.now().date().isoformat(),) if t == "tefila_today" else ())
            ids = {x[0] for x in c.execute(q, args)}
        return [e for e in scope if e["id"] in ids]
    return []


def current_program(now):
    return "day" if 14 <= now.hour < 20 else "night"


def present_now(people, now=None):
    """(program, [present], [absent]) among `people` for the kollel running now. Present = clocked in this
    session and not yet left (no exit, or an exit still ahead)."""
    now = now or db.now()
    program = current_program(now)
    d = schedule.session_date(now) if program == "night" else now.date()
    with db.connect() as c:
        rows = c.execute("SELECT * FROM attendance WHERE program=? AND clock_in<=?", (program, now.isoformat())).fetchall()
    here = set()
    for a in rows:
        cin = db.parse(a["clock_in"])
        if (schedule.session_date(cin) if program == "night" else cin.date()) != d:
            continue
        if a["clock_out"] and db.parse(a["clock_out"]) <= now:
            continue
        here.add(a["employee_id"])
    members = [e for e in people if (schedule.in_night(e) if program == "night" else schedule.in_day(e))]
    return program, [e for e in members if e["id"] in here], [e for e in members if e["id"] not in here]


def manager_scope(sender, everyone=None):
    """Who a manager can send to / look up: everyone for the tech manager; for an on-site manager his
    shift in Kollel Chatzos (all of it if he has no shift) and Kollel Horaah if he manages it."""
    if everyone is None:                       # lookups (status, here now, bonus, raffle...): the manager himself too
        with db.connect() as c:
            everyone = c.execute("SELECT * FROM employees WHERE active=1").fetchall()
    if sender["role"] == "tech_manager":
        return list(everyone)
    scope = []
    if schedule.in_night(sender):
        scope = [e for e in everyone if schedule.in_night(e) and (not sender["shift"] or e["shift"] == sender["shift"])]
    if schedule.in_day(sender):
        scope += [e for e in everyone if schedule.in_day(e) and e not in scope]
    return scope


TARGET_LABELS = {"shift": "כל העובדים", "managers": "המנהלים", "night": "כולל חצות", "day": "כולל הוראה",
                 "night_no_shuttle": "כולל חצות, מי שלא רשום בהסעות", "present": "מי שנוכח כרגע",
                 "trip": "הנרשמים לנסיעה", "tefila_today": "הנרשמים היום לתפלה בכח",
                 "tefila_all": "כל הנרשמים לתפלה בכח"}


def target_label(target, recipients):
    if target["type"] == "driver_group":
        return "קבוצת ההסעה של הנהג והנהגים"
    if target["type"] == "employees":
        return ", ".join(e["name"] for e in recipients)
    return TARGET_LABELS.get(target["type"]) or (
        GROUPS_SHORT["he"].get(target["group"], f"קבוצה {target['group']}") if target["type"] == "group" else
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
    if is_manager(emp) and target["type"] != "managers" and all(e["id"] != emp["id"] for e in recipients):
        recipients = list(recipients) + [emp]        # the manager gets it too, so he knows it went out
    t = db.now()
    with db.connect() as c:
        msg_id = c.execute(
            "INSERT INTO messages(sender_id, created_at, heb_date, recipient_type, recipient_label, recording_path) "
            "VALUES (?,?,?,?,?,?)", (emp["id"], t.isoformat(), hebcal.format_date(t.date(), "he"), target["type"],
                                     target_label(target, recipients), recording_path(answer))).lastrowid
        c.executemany("INSERT INTO message_recipients(message_id, employee_id) VALUES (?,?)",
                      [(msg_id, e["id"]) for e in recipients])
    if NOTIFY_BY_CALL and nbs.enabled():
        by_tzintuk = target["type"] in ("shift", "group", "driver_group", "night", "day", "night_no_shuttle",
                                        "present", "trip", "tefila_today", "tefila_all")
        threading.Thread(target=notify, args=(msg_id, by_tzintuk), daemon=True).start()
    r.say(r.t("saved_to_managers" if target["type"] == "managers" else "saved_to_employees"))
    main_menu(r)


def notify_urgent(msg_id):
    """Phone every recipient who has not heard it yet; redialled every 15 minutes until answered.
    What happened is written on the message (shown on the admin Messages page)."""
    try:
        _notify_urgent(msg_id)
    except Exception as ex:                      # never lose the reason silently
        with db.connect() as c:
            c.execute("UPDATE messages SET call_result=? WHERE id=?", (f"error: {ex!r}"[:300], msg_id))


def _notify_urgent(msg_id):
    with db.connect() as c:
        rows = c.execute("SELECT rc.id, e.name, e.phone, e.language, s.name AS sender FROM message_recipients rc "
                         "JOIN employees e ON e.id=rc.employee_id JOIN messages m ON m.id=rc.message_id "
                         "JOIN employees s ON s.id=m.sender_id WHERE rc.message_id=? AND rc.listened_at IS NULL", (msg_id,)).fetchall()
    texts = {x["phone"]: text(x["language"], "urgent_call_text", name=x["name"], sender=x["sender"])
             for x in rows if nbs.israeli(x["phone"] or "")}
    if not texts:
        with db.connect() as c:
            c.execute("UPDATE messages SET call_result=? WHERE id=?", ("nobody to call (no phone numbers)", msg_id))
        return
    with db.connect() as c:
        paths = [x for x in c.execute("SELECT recording_path FROM messages WHERE id=?", (msg_id,)).fetchone()[0].split("|") if x]
    wav = nbs.join_wavs([nbs.download_recording(x) for x in paths])
    after = {x["phone"]: text(x["language"], "urgent_call_after", sender=x["sender"]) for x in rows if nbs.israeli(x["phone"] or "")}
    ok, info = nbs.voice_calls(wav, after, folder=paths[0].rsplit("/", 1)[0] or "/") if wav else \
        (False, "could not download the recording")
    how = "the recording is played in the call"
    if not ok:                                   # could not play the recording in the call: ask them to call the line
        first = info
        ok, info = nbs.urgent_calls(texts)
        how = f"a call asking to phone the line (playing the recording failed: {first})"
    result = (f"OK - campaign {info}; {how}" if ok else f"FAILED - {info}; {how}")
    with db.connect() as c:
        c.execute("UPDATE messages SET campaign_id=?, call_result=? WHERE id=?", (info if ok else None, result[:500], msg_id))
        c.executemany("UPDATE message_recipients SET delivery_status=? WHERE id=?",
                      [("calling every 15 min" if ok else "call failed", x["id"]) for x in rows])


def notify(msg_id, by_tzintuk=False):
    """Tell each recipient a new message is waiting: a tzintuk (missed-call ring) for a group or everyone,
    a short voice call for one person or for the managers. Never SMS."""
    with db.connect() as c:
        rows = c.execute("SELECT rc.id, e.name, e.phone, e.phone2, e.phone2_mode, e.language, s.name AS sender, m.channel "
                         "FROM message_recipients rc "
                         "JOIN employees e ON e.id=rc.employee_id JOIN messages m ON m.id=rc.message_id "
                         "JOIN employees s ON s.id=m.sender_id WHERE rc.message_id=?", (msg_id,)).fetchall()
    callable_rows = [x for x in rows if nbs.israeli(x["phone"] or "")]
    if not callable_rows:
        return
    extra = [x for x in callable_rows if x["phone2"] and x["phone2_mode"] == "all" and nbs.israeli(x["phone2"])]
    if by_tzintuk:
        ok, info = nbs.run_tzintuk([x["phone"] for x in callable_rows] + [x["phone2"] for x in extra])
    else:
        texts = {x["phone"]: text(x["language"], "notify_call_text_moreshet" if x["channel"] == "moreshet" else "notify_call_text",
                                  name=x["name"], sender=x["sender"])
                 for x in callable_rows}
        texts.update({x["phone2"]: texts[x["phone"]] for x in extra})
        ok, info = nbs.run_tts_calls(texts)
    with db.connect() as c:
        c.execute("UPDATE messages SET campaign_id=? WHERE id=?", (info if ok else None, msg_id))
        c.executemany("UPDATE message_recipients SET delivery_status=? WHERE id=?",
                      [(("tzintuk" if by_tzintuk else "called") if ok else "call failed", x["id"])
                       for x in callable_rows])
