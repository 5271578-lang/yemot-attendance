"""Hebrew calendar conversion (no external libraries).

Arithmetic Hebrew calendar, based on the standard molad / dehiyyot rules.
Months are numbered from Nisan: 1=Nisan ... 7=Tishrei ... 12=Adar (Adar I in
a leap year), 13=Adar II.
"""
from datetime import date

HEBREW_EPOCH = -1373427  # proleptic Gregorian ordinal of 1 Tishrei, year 1

MONTHS_EN = {1: "Nisan", 2: "Iyar", 3: "Sivan", 4: "Tammuz", 5: "Av", 6: "Elul",
             7: "Tishrei", 8: "Cheshvan", 9: "Kislev", 10: "Tevet", 11: "Shevat",
             12: "Adar", 13: "Adar II"}
MONTHS_HE = {1: "ניסן", 2: "אייר", 3: "סיוון", 4: "תמוז", 5: "אב", 6: "אלול",
             7: "תשרי", 8: "חשוון", 9: "כסלו", 10: "טבת", 11: "שבט",
             12: "אדר", 13: "אדר ב"}


def is_leap(y):
    return (7 * y + 1) % 19 < 7


def last_month(y):
    return 13 if is_leap(y) else 12


def _elapsed_days(y):
    months = (235 * y - 234) // 19
    parts = 12084 + 13753 * months
    day = months * 29 + parts // 25920
    if (3 * (day + 1)) % 7 < 3:
        day += 1
    return day


def _year_delay(y):
    ny0, ny1, ny2 = _elapsed_days(y - 1), _elapsed_days(y), _elapsed_days(y + 1)
    if ny2 - ny1 == 356:
        return 2
    if ny1 - ny0 == 382:
        return 1
    return 0


def new_year(y):
    return HEBREW_EPOCH + _elapsed_days(y) + _year_delay(y)


def days_in_year(y):
    return new_year(y + 1) - new_year(y)


def month_length(m, y):
    n = days_in_year(y)
    if m in (2, 4, 6, 10, 13):
        return 29
    if m == 12 and not is_leap(y):
        return 29
    if m == 8 and n % 10 != 5:          # Cheshvan is long only in 355/385-day years
        return 29
    if m == 9 and n % 10 == 3:          # Kislev is short in 353/383-day years
        return 29
    return 30


def to_ordinal(y, m, d):
    f = new_year(y) + d - 1
    if m < 7:
        for mm in range(7, last_month(y) + 1):
            f += month_length(mm, y)
        for mm in range(1, m):
            f += month_length(mm, y)
    else:
        for mm in range(7, m):
            f += month_length(mm, y)
    return f


def from_ordinal(f):
    y = int((f - HEBREW_EPOCH) / (35975351 / 98496))
    while new_year(y + 1) <= f:
        y += 1
    while new_year(y) > f:
        y -= 1
    m = 7 if f < to_ordinal(y, 1, 1) else 1
    while f > to_ordinal(y, m, month_length(m, y)):
        m += 1
    return y, m, f - to_ordinal(y, m, 1) + 1


def from_gregorian(g: date):
    return from_ordinal(g.toordinal())


def to_gregorian(y, m, d):
    return date.fromordinal(to_ordinal(y, m, d))


def month_name(m, y, lang="en"):
    if m == 12 and is_leap(y):
        return "Adar I" if lang == "en" else "אדר א"
    return (MONTHS_EN if lang == "en" else MONTHS_HE)[m]


def month_range(y, m):
    """Gregorian (first_day, last_day) of a Hebrew month."""
    return to_gregorian(y, m, 1), to_gregorian(y, m, month_length(m, y))


def months_of_year(y):
    """Hebrew months in calendar order, Tishrei first."""
    return list(range(7, last_month(y) + 1)) + list(range(1, 7))


def prev_month(y, m):
    order = months_of_year(y)
    i = order.index(m)
    if i == 0:
        return y - 1, 6
    return y, order[i - 1]


def next_month(y, m):
    order = months_of_year(y)
    i = order.index(m)
    if i == len(order) - 1:
        return y + 1, 7
    return y, order[i + 1]


def format_date(g: date, lang="en"):
    y, m, d = from_gregorian(g)
    return f"{d} {month_name(m, y, lang)} {y}"
