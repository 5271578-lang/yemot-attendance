"""9 -> 9 for managers: say what to do instead of going through the menus.

1 - an action now: the spoken sentence (the phone system's speech recognition) is understood by Claude
    (ANTHROPIC_API_KEY) into one of the actions below. Nothing changes before the manager confirms with 1.
2 - a request for a change in the system: saved with its text on the admin page "Requests" (and e-mailed),
    to be done later.

Only actions the phone menus already have, and only on avrechim in the manager's scope.
"""
import json
import os
import re
import threading
from datetime import date, timedelta

import requests

import db

API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MODEL = os.environ.get("ASSISTANT_MODEL", "claude-sonnet-5-5")
API_URL = "https://api.anthropic.com/v1/messages"

ACTIONS = """Actions (answer with exactly one JSON object, nothing else):
{"action": "gift", "employee_id": <id>, "amount": <shekels, number>}          a separate bonus this month
{"action": "tefila_add", "employee_id": <id>, "day": "YYYY-MM-DD"}             add a Tefila B'Koach raffle entry for that day
{"action": "correction", "correction_id": <id>, "approve": true|false}         approve / reject a pending correction
{"action": "status", "employee_id": <id>}                                      hear an avrech's attendance this month
{"action": "present"}                                                          who is in the kollel now
{"action": "message", "to": "night"|"day"|"night_no_shuttle"|"present"|"managers"}   record a message to a group
{"action": "message_one", "employee_id": <id>}                                 record a message to one avrech
{"action": "request", "text": "<the request in Hebrew>"}                       anything else: a change to the system itself
{"action": "unclear", "question": "<short Hebrew question to ask back>"}       not clear which avrech / what to do
Rules: use only ids from the lists. Names are spoken and transcribed by speech recognition, so match by sound
(spelling mistakes, first name only, a nickname). If two avrechim fit equally, answer "unclear" and name both.
Dates: today is {today}; "yesterday" = the day before.""".replace("{today}", "%s")


def enabled():
    return bool(API_KEY)


def _people_lines(people):
    out = []
    for e in people:
        k = "/".join(x for x, on in (("כולל חצות", e["in_night"]), ("כולל הוראה", e["in_day"])) if on)
        out.append(f'{e["id"]}: {e["name"]} (מספר {e["emp_number"]}{", " + k if k else ""})')
    return "\n".join(out)


def _corrections_lines(ids):
    if not ids:
        return "(none)"
    with db.connect() as c:
        rows = [c.execute("SELECT k.*, e.name FROM corrections k JOIN employees e ON e.id=k.employee_id WHERE k.id=?",
                          (i,)).fetchone() for i in ids]
    return "\n".join(f'{k["id"]}: {k["name"]}, {"כניסה" if k["kind"] == "in" else "יציאה"} ב־{k["requested_time"][:16]}'
                     for k in rows if k)


def interpret(spoken, people, correction_ids, today=None):
    """The spoken sentence -> an action dict (see ACTIONS), or {"action": "error", "why": ...}."""
    if not enabled():
        return {"action": "error", "why": "no key"}
    today = today or db.now().date()
    system = ("You turn a kollel manager's spoken Hebrew instruction (from a phone line) into one action.\n"
              + ACTIONS % today.isoformat()
              + "\n\nAvrechim (id: name):\n" + _people_lines(people)
              + "\n\nPending corrections (id: avrech, kind, requested time):\n" + _corrections_lines(correction_ids))
    try:
        r = requests.post(API_URL, timeout=25, headers={"x-api-key": API_KEY, "anthropic-version": "2023-06-01",
                                                        "content-type": "application/json"},
                          json={"model": MODEL, "max_tokens": 300, "system": system,
                                "messages": [{"role": "user", "content": spoken}]})
        r.raise_for_status()
        txt = "".join(b.get("text", "") for b in r.json().get("content", []))
        m = re.search(r"\{.*\}", txt, re.S)
        return json.loads(m.group(0)) if m else {"action": "error", "why": "no answer"}
    except (requests.RequestException, ValueError) as ex:
        code = getattr(getattr(ex, "response", None), "status_code", None)
        return {"action": "error", "why": f"HTTP {code}" if code else type(ex).__name__}


def parse_day(v, today):
    try:
        d = date.fromisoformat(str(v))
    except ValueError:
        return None
    return d if today - timedelta(days=60) <= d <= today else None


# ---- requests for changes ----------------------------------------------------

def save_request(emp, text):
    with db.connect() as c:
        rid = c.execute("INSERT INTO change_requests(employee_id, created_at, text) VALUES (?,?,?)",
                        (emp["id"], db.now().isoformat(), text)).lastrowid
    threading.Thread(target=_email, args=(emp["name"], text), daemon=True).start()
    return rid


def _email(name, text):
    import report
    from html import escape
    try:
        report.send(0, 0, builder=lambda: (f"בקשה חדשה מהקו - {name}",
                                           f'<div dir="rtl"><p>{escape(name)} ביקש בקו:</p><p><b>{escape(text)}</b></p>'
                                           f'<p>כל הבקשות: בממשק הניהול, דף Requests.</p></div>', ""))
    except Exception:
        pass


def read_key():
    """The secret of the read-only address of the open requests (made once, kept in the settings)."""
    import secrets
    k = os.environ.get("REQUESTS_KEY") or db.get_setting("requests_key")
    if not k:
        k = secrets.token_urlsafe(18)
        db.set_setting("requests_key", k)
    return k


def as_text(rows):
    if not rows:
        return "אין בקשות פתוחות."
    return "\n\n".join(f'#{x["id"]} | {x["created_at"][:16].replace("T", " ")} | {x["name"]}\n{x["text"]}' for x in rows)


def requests_list(status=None):
    q = "SELECT r.*, e.name FROM change_requests r JOIN employees e ON e.id=r.employee_id"
    args = ()
    if status:
        q += " WHERE r.status=?"
        args = (status,)
    with db.connect() as c:
        return c.execute(q + " ORDER BY r.created_at DESC", args).fetchall()
