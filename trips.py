"""Temporary registration extensions (a trip, an event). Each one is open until its deadline, on its own key.

To add one: append to TRIPS. To end one early: change its `until`. Registrations stay in the database
(table trip_signups, created in db.py) and are shown in the admin page "Registrations".
"""
from datetime import datetime

import db

TRIPS = [
    {
        "id": "kivrei_tzadikim_kislev_5787",
        "key": "7",
        "audience": "night",
        "title": "נסיעה לקברי הצדיקים, ליל ערב ראש חודש כסלו תשפ\"ז (ליל שני)",
        "opens": datetime(2026, 11, 6, 8, 0, tzinfo=db.TZ),
        "until": datetime(2026, 11, 8, 12, 0, tzinfo=db.TZ),          # Sunday, 12:00
        "menu": {"he": "להרשמה לנסיעה לקברי הצדיקים הקישו 7",
                 "en": "To register for the trip to the graves of the tzadikim, press 7"},
        # TTS: no quotes or hyphens; numbers in words
        "intro": {
            "he": "הגעת לשלוחת ההרשמה לנסיעה לקברי הצדיקים, בעזרת השם בליל שני, ליל ערב ראש חודש כסלו. "
                  "סדר הנסיעה. יציאה בשעה שתים עשרה ארבעים וחמש בלילה. "
                  "התבודדות ביער נבוריה, ליד ציון התנא רבי אלעזר המודעי. "
                  "סדר הלימוד בציון רבי ישמעאל כהן גדול, בסאג׳ור. "
                  "חזרה לתפילת שחרית בבית המדרש. "
                  "ההרשמה פתוחה עד יום ראשון בשתים עשרה בצהריים. "
                  "להרשמה לנסיעה הקש 1",
            "en": "This is the registration for the trip to the graves of the tzadikim, with Hashem's help on Sunday night, "
                  "the night before erev Rosh Chodesh Kislev. "
                  "We leave at twelve forty five at night. "
                  "Hisbodedus in the Nevoria forest, near the tziyun of the Tanna Rabbi Elazar HaModai. "
                  "Learning at the tziyun of Rabbi Yishmael Kohen Gadol in Sajur. "
                  "Back for Shacharis in the beis medrash. "
                  "Registration is open until Sunday at twelve noon. "
                  "To register, press 1",
        },
        "options": {
            "1": {"he": "נרשם לנסיעה", "en": "registered", "admin": "Registered"},
        },
        "saved": {"he": "נרשמת בהצלחה לנסיעה. נסיעה טובה",
                  "en": "You are registered for the trip"},
        "already": {"he": "אתה כבר רשום לנסיעה",
                    "en": "You are already registered for the trip"},
    },
    {
        "id": "teveria_5787",
        "key": "7",
        "audience": "night",          # only Kollel Chatzos avrechim hear it ("day" / "all")
        "title": "נסיעה לטבריה, יארצייט רבי ישראל קארדונער, אור לט' חשוון תשפ\"ז",
        "until": datetime(2026, 10, 19, 15, 0, tzinfo=db.TZ),          # Monday, 15:00
        "menu": {"he": "להרשמה לנסיעה לטבריה הקישו 7",
                 "en": "To register for the trip to Tiberias, press 7"},
        # TTS: no quotes or hyphens; numbers in words
        "intro": {
            "he": "הגעת לשלוחת ההרשמה לנסיעה לטבריה, ליארצייט של הרב הקדוש רבי ישראל קארדונער, זכר צדיק לברכה, "
                  "בליל שלישי, פרשת לך לך, אור לתשעה בחשוון. "
                  "סדר הנסיעה יהיה לפי התוכנית בינתיים כדלהלן. "
                  "יוצאים מצפת בשעה שתים עשרה וחצי בלילה. "
                  "סדר התבודדות בציון רבי מאיר בעל הנס, במקום במירון. "
                  "עליה לקברו, ולימוד בציון הקדוש של רבי עקיבא, זכותו יגן עלינו. "
                  "אם אתה רוצה לנסוע ולהישאר לתפילת שחרית בציון רבי עקיבא, הקש 1. "
                  "אם אתה רוצה לנסוע ולחזור לבית מדרשנו לתפילת שחרית, הקש 2. "
                  "שימו לב, הנסיעה תלויה במספר הנרשמים",
            "en": "This is the registration for the trip to Tiberias, for the yahrzeit of the holy Rabbi Yisroel Kardoner, "
                  "on the night before Tuesday, parshas Lech Lecha, the ninth of Cheshvan. "
                  "The plan for now is as follows. "
                  "We leave Tzfas at twelve thirty at night. "
                  "Hisbodedus at the tziyun of Rabbi Meir Baal Haness, instead of Meron. "
                  "Going up to his kever, and learning at the holy tziyun of Rabbi Akiva. "
                  "To travel and stay for Shacharis at the tziyun of Rabbi Akiva, press 1. "
                  "To travel and return to our beis medrash for Shacharis, press 2. "
                  "Please note, the trip depends on the number of people who register",
        },
        "options": {
            "1": {"he": "כולל תפילת שחרית", "en": "including Shacharis", "admin": "Stays for Shacharis at Rabbi Akiva"},
            "2": {"he": "לא כולל תפילת שחרית", "en": "not including Shacharis", "admin": "Returns for Shacharis in the beis medrash"},
        },
        "saved": {"he": "נרשמת בהצלחה לנסיעה, {option}",
                  "en": "You are registered for the trip, {option}"},
        "already": {"he": "אתה כבר רשום לנסיעה, {option}. אפשר לשנות בהקשה חדשה",
                    "en": "You are already registered, {option}. You can change it by pressing again"},
    },
]

def for_employee(trip, emp):
    who = trip.get("audience", "all")
    if who == "night":
        return bool(emp["in_night"])
    if who == "day":
        return bool(emp["in_day"])
    return True


def open_trips(emp=None, now=None):
    """Registrations open now (and meant for this avrech). A closed one keeps its data and admin page."""
    now = now or db.now()
    return [t for t in TRIPS if t.get("opens", now) <= now < t["until"] and (emp is None or for_employee(t, emp))]


MANAGER_DAYS_AFTER = 14     # managers can still hear the status this long after it closed


def recent_trips(now=None):
    from datetime import timedelta
    now = now or db.now()
    return [t for t in TRIPS if t.get("opens", now) <= now < t["until"] + timedelta(days=MANAGER_DAYS_AFTER)]


def by_key(key, emp=None, now=None):
    return next((t for t in open_trips(emp, now) if t["key"] == key), None)


def by_id(trip_id):
    return next((t for t in TRIPS if t["id"] == trip_id), None)


def signup_of(trip_id, emp_id):
    with db.connect() as c:
        return c.execute("SELECT * FROM trip_signups WHERE trip=? AND employee_id=?", (trip_id, emp_id)).fetchone()


def register(trip_id, emp_id, choice):
    with db.connect() as c:
        c.execute("INSERT OR REPLACE INTO trip_signups(trip, employee_id, choice, created_at) VALUES (?,?,?,?)",
                  (trip_id, emp_id, choice, db.now().isoformat()))


def signups(trip_id):
    with db.connect() as c:
        return c.execute("SELECT s.*, e.name, e.phone, e.emp_number FROM trip_signups s JOIN employees e ON e.id = s.employee_id "
                         "WHERE s.trip=? ORDER BY s.choice, e.name", (trip_id,)).fetchall()
