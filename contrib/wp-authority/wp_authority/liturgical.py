# SPDX-License-Identifier: AGPL-3.0-only
"""Liturgical day names for a date under the LCMS three-year (LSB) calendar.

`days(date)` returns every day that may be kept on that date: the Sunday or festival of
the temporal cycle, fixed feasts that fall on a Sunday, and the observed festivals (All
Saints' Day on the first Sunday of November, Reformation Day on the last Sunday of
October). Other weekdays have no day, so their sermons go to "needs a human".
"""

from dataclasses import dataclass
from datetime import date, timedelta

from wp_authority.lectionary import feasts, readings
from wp_authority.names import ordinal

FEASTS = "lsb-feasts"


@dataclass(frozen=True)
class Day:
    names: tuple[str, ...]
    table: str
    key: str | None

    @property
    def name(self) -> str:
        return self.names[0]

    def readings(self) -> list[str]:
        return readings(self.table, self.key)


def easter(year: int) -> date:
    """Gregorian Easter (anonymous algorithm)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    g = (b - (b + 8) // 25 + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    m = (32 + 2 * e + 2 * i - h - k) % 7
    n = (a + 11 * h + 22 * m) // 451
    month, dom = divmod(h + m - 7 * n + 114, 31)
    return date(year, month, dom + 1)


def advent1(year: int) -> date:
    start = date(year, 11, 27)
    return start + timedelta((6 - start.weekday()) % 7)


def church_year(day: date) -> int:
    """The calendar year in which the church year containing `day` ends."""
    return day.year + 1 if day >= advent1(day.year) else day.year


def series(day: date) -> str:
    """LCMS three-year series: the church year ending in 2017 was Series A."""
    return "ABC"[(church_year(day) - 2017) % 3]


def table_for(day: date) -> str:
    return "lsb-3" + series(day).lower()


def _weeks(later: date, earlier: date) -> int:
    return (later - earlier).days // 7


LSB_TITLES = {
    "St. Andrew": "St. Andrew, Apostle",
    "St. Thomas": "St. Thomas, Apostle",
    "St. Stephen": "St. Stephen, Martyr",
    "Holy Innocents": "The Holy Innocents, Martyrs",
    "Confession of St. Peter": "The Confession of St. Peter",
    "St. Timothy": "St. Timothy, Pastor and Confessor",
    "Conversion of St. Paul": "The Conversion of St. Paul",
    "St. Titus": "St. Titus, Pastor and Confessor",
    "Purification of Mary and Presentation of Our Lord": (
        "The Purification of Mary and the Presentation of Our Lord"
    ),
    "St. Matthias": "St. Matthias, Apostle",
    "St. Joseph": "St. Joseph, Guardian of Jesus",
    "Annunciation of Our Lord": "The Annunciation of Our Lord",
    "St. Mark": "St. Mark, Evangelist",
    "St. Philip and St. James": "St. Philip and St. James, Apostles",
    "Visitation": "The Visitation",
    "St. Barnabas": "St. Barnabas, Apostle",
    "Nativity of St. John the Baptist": "The Nativity of St. John the Baptist",
    "St. Peter and St. Paul": "St. Peter and St. Paul, Apostles",
    "St. James the Elder": "St. James the Elder, Apostle",
    "St. Bartholomew": "St. Bartholomew, Apostle",
    "Martyrdom of St. John the Baptist": "The Martyrdom of St. John the Baptist",
    "St. Matthew": "St. Matthew, Apostle and Evangelist",
    "St. Luke": "St. Luke, Evangelist",
    "St. James of Jerusalem": "St. James of Jerusalem, Brother of Jesus and Martyr",
    "St. Simon and St. Jude": "St. Simon and St. Jude, Apostles",
}
"""LSB's full feast names; the lectionary source uses the short ones."""


def days(day: date) -> list[Day]:
    """Every day that may be kept on `day` (three-year calendar); [] for ordinary weekdays."""
    table = table_for(day)
    found = [Day(n, table, n[0]) for n in _temporal(day)]
    sunday = day.weekday() == 6
    if sunday:
        for name, (month, dom) in feasts().items():
            if (day.month, day.day) == (month, dom):
                title = LSB_TITLES.get(name, name)
                found.append(Day((title, *([name] if title != name else [])), FEASTS, name))
    if sunday and day.month == 11 and day.day <= 7:
        observed = ("All Saints' Day", "All Saints' Day (Observed)")
        found.append(Day(observed, FEASTS, "All Saints' Day"))
    if sunday and day.month == 10 and day.day >= 25:
        found.append(Day(("Reformation Day", "Reformation Sunday"), FEASTS, "Reformation Day"))
    unique: dict[str, Day] = {}
    for found_day in found:
        unique.setdefault(found_day.name, _with_key(found_day, day))
    return list(unique.values())


def _with_key(found: Day, day: date) -> Day:
    if found.table == FEASTS:
        return found
    if found.name.endswith("Sunday after Pentecost"):
        return Day(found.names, found.table, _proper(day))
    return found


def _proper(day: date) -> str | None:
    if date(day.year, 5, 24) <= day <= date(day.year, 5, 28):
        return "Proper 3"
    n = 4 + _weeks(day, date(day.year, 5, 29))
    return f"Proper {n}" if 4 <= n <= 29 else None


def _temporal(day: date) -> list[tuple[str, ...]]:
    fixed = {
        (12, 24): ("Christmas Eve",),
        (12, 25): ("Christmas Day", "The Nativity of Our Lord"),
        (1, 6): ("The Epiphany of Our Lord", "Epiphany"),
    }
    out = [fixed[(day.month, day.day)]] if (day.month, day.day) in fixed else []
    e = easter(day.year)
    weekday = {
        e - timedelta(46): ("Ash Wednesday",),
        e - timedelta(6): ("Monday of Holy Week",),
        e - timedelta(5): ("Tuesday of Holy Week",),
        e - timedelta(4): ("Wednesday of Holy Week",),
        e - timedelta(3): ("Holy Thursday", "Maundy Thursday"),
        e - timedelta(2): ("Good Friday",),
        e - timedelta(1): ("Holy Saturday", "The Vigil of Easter", "Easter Vigil"),
        e + timedelta(39): ("The Ascension of Our Lord", "Ascension Day"),
    }
    if day in weekday:
        out.append(weekday[day])
    if day.weekday() == 6:
        sunday = _sunday(day, e)
        if sunday:
            out.append(sunday)
    return out


def _sunday(day: date, e: date) -> tuple[str, ...] | None:
    y = day.year
    adv = advent1(y)
    if adv <= day < date(y, 12, 25):
        n = _weeks(day, adv) + 1
        return (f"{ordinal(n)} Sunday in Advent", f"Advent {['I', 'II', 'III', 'IV'][n - 1]}")
    if (day.month == 12 and day.day >= 26) or (day.month, day.day) == (1, 1):
        return ("First Sunday after Christmas", "First Sunday of Christmas")
    if day.month == 1 and 2 <= day.day <= 5:
        return ("Second Sunday after Christmas", "Second Sunday of Christmas")
    if day.month == 12:
        return None
    baptism = date(y, 1, 7) + timedelta((6 - date(y, 1, 7).weekday()) % 7)
    transfiguration = e - timedelta(49)
    if day == baptism and day < transfiguration:
        return ("The Baptism of Our Lord", "First Sunday after the Epiphany")
    if baptism < day < transfiguration:
        nth = ordinal(_weeks(day, baptism) + 1)
        return (f"{nth} Sunday after the Epiphany", f"{nth} Sunday of the Epiphany")
    if day == transfiguration:
        return (
            "The Transfiguration of Our Lord",
            "Transfiguration",
            "Last Sunday after the Epiphany",
        )
    if e - timedelta(42) <= day <= e - timedelta(14):
        return (f"{ordinal(_weeks(day, e - timedelta(42)) + 1)} Sunday in Lent",)
    if day == e - timedelta(7):
        return ("Palm Sunday", "Sunday of the Passion", "Palm / Passion Sunday")
    if day == e:
        return ("The Resurrection of Our Lord", "Easter Sunday", "Easter Day", "Easter")
    if e < day < e + timedelta(49):
        n = _weeks(day, e) + 1
        return (f"{ordinal(n)} Sunday of Easter", *(["Good Shepherd Sunday"] if n == 4 else []))
    if day == e + timedelta(49):
        return ("The Day of Pentecost", "Pentecost Sunday", "Pentecost")
    if day == e + timedelta(56):
        return ("The Holy Trinity", "Holy Trinity Sunday", "Trinity Sunday", "Trinity")
    if not e + timedelta(56) < day < adv:
        return None
    before_advent = _weeks(adv, day)
    church_year_end = {
        1: ("Last Sunday of the Church Year", "Christ the King", "Christ the King Sunday"),
        2: (
            "Second-Last Sunday of the Church Year",
            "Second-to-Last Sunday of the Church Year",
            "Next to Last Sunday of the Church Year",
        ),
        3: ("Third-Last Sunday of the Church Year", "Third-to-Last Sunday of the Church Year"),
    }
    nth = ordinal(_weeks(day, e + timedelta(49)))
    return (
        f"{nth} Sunday after Pentecost",
        f"{nth} Sunday of Pentecost",
        *church_year_end.get(before_advent, ()),
    )
