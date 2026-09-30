"""Cron expressions and the loose "every day at 8" phrasing behind them.

The model is asked to turn what the operator said into a cron expression, and
this module is what has to agree with it. It is deliberately dependency-free
(no croniter) so it can be unit tested on its own, and deliberately strict: a
schedule that parses is a schedule that will fire, and anything ambiguous is
rejected with a message the model can act on rather than silently defaulting to
"every minute".

Accepted forms
--------------
  ``*/5 * * * *``      five-field cron (minute hour day-of-month month day-of-week)
  ``0 8 * * 1-5``      names work too: ``0 8 * * mon-fri``
  ``@daily``           ``@hourly @daily @midnight @weekly @monthly @yearly``
  ``in 20 minutes``    one-shot, resolved against "now"
  ``every 15 minutes`` ``*/15 * * * *``
  ``every weekday at 09:15``
  ``2026-03-01T08:30`` one-shot at a wall-clock time

Day-of-week is 0-7 with 0 and 7 both Sunday, matching Vixie cron. When both
day-of-month and day-of-week are restricted, cron fires if *either* matches;
that is the traditional rule and the one operators expect from a crontab.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

__all__ = [
    "CronError",
    "Schedule",
    "parse_cron",
    "parse_schedule",
    "next_run",
    "describe_schedule",
]


class CronError(ValueError):
    """A schedule that cannot be honoured. The message goes back to the model."""


@dataclass(frozen=True)
class Schedule:
    """A resolved schedule: either a repeating cron or a single wall-clock run.

    ``run_at`` is kept absolute rather than "in N minutes" so a restart, a sleep
    or a laptop waking up cannot make the task fire at the wrong moment.
    """

    kind: str  # "cron" | "once"
    cron: str = ""
    run_at: float = 0.0

    @property
    def repeating(self) -> bool:
        return self.kind == "cron"


_MONTH_NAMES = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_DOW_NAMES = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}

# @macro -> expression. `@reboot` is intentionally absent: this scheduler only
# ever exists for the life of the backend process, so "on boot" has no stable
# meaning to promise the operator.
_MACROS = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}

_FIELD_NAMES = (None, None, None, _MONTH_NAMES, _DOW_NAMES)
_FIELD_LABELS = ("minute", "hour", "day-of-month", "month", "day-of-week")
_BOUNDS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))

_UNITS = {
    "second": 1, "seconds": 1, "sec": 1, "secs": 1, "s": 1,
    "minute": 60, "minutes": 60, "min": 60, "mins": 60, "m": 60,
    "hour": 3600, "hours": 3600, "hr": 3600, "hrs": 3600, "h": 3600,
    "day": 86400, "days": 86400, "d": 86400,
    "week": 604800, "weeks": 604800, "w": 604800,
    "month": 2592000, "months": 2592000,
}

_WEEKDAYS = (
    (r"sunday|sun", 0), (r"monday|mon", 1), (r"tuesday|tues|tue", 2),
    (r"wednesday|weds|wed", 3), (r"thursday|thurs|thu", 4),
    (r"friday|fri", 5), (r"saturday|sat", 6),
)
_WEEKDAY_PATTERN = "|".join(words for words, _ in _WEEKDAYS)
_WEEKDAY_ALIASES = {alias: value for words, value in _WEEKDAYS for alias in words.split("|")}


def _name_to_number(token: str, index: int) -> int | None:
    """Resolve a three-letter month/day name, or None if it is not one."""
    names = _FIELD_NAMES[index]
    return names.get(token) if names else None


def _parse_field(field: str, index: int) -> tuple[set[int], bool]:
    """Expand one cron field. Returns the matching values and whether the
    operator actually restricted it (needed for the dom/dow OR rule)."""
    lo, hi = _BOUNDS[index]
    field = field.strip()
    if not field:
        raise CronError(f"empty {_FIELD_LABELS[index]} field")
    restricted = field != "*"
    values: set[int] = set()

    for part in field.split(","):
        part = part.strip()
        if not part:
            raise CronError(f"empty entry in the {_FIELD_LABELS[index]} field {field!r}")
        step = 1
        if "/" in part:
            part, _, raw_step = part.partition("/")
            step_part = raw_step.strip()
            if not step_part.isdigit() or int(step_part) < 1:
                raise CronError(f"bad step {raw_step!r} in {field!r}")
            step = int(step_part)
            part = part.strip() or "*"

        if part == "*":
            start, end = lo, hi
        elif "-" in part.lstrip("-"):
            raw_start, _, raw_end = part.partition("-")
            start = _resolve(raw_start.strip(), index)
            end = _resolve(raw_end.strip(), index)
            if start is None or end is None:
                raise CronError(f"bad range {part!r} in the {_FIELD_LABELS[index]} field")
        else:
            single = _resolve(part, index)
            if single is None:
                raise CronError(f"{part!r} is not a valid {_FIELD_LABELS[index]} value")
            start = end = single
            if "/" in field and step > 1:
                # "5/15" means "from 5 to the end, every 15" - the Vixie reading.
                end = hi
        if end < start:
            raise CronError(f"range {part!r} runs backwards")
        # Day-of-week accepts 7 as a second spelling of Sunday, so the ceiling is
        # only lowered for that field.
        ceiling = 6 if (index == 4 and 7 in values) else hi
        for value in range(start, min(end, ceiling) + 1, step):
            values.add(0 if index == 4 and value == 7 else value)

    if not values:
        raise CronError(f"the {_FIELD_LABELS[index]} field {field!r} matches nothing")
    return values, restricted


def _resolve(token: str, index: int) -> int | None:
    lo, hi = _BOUNDS[index]
    named = _name_to_number(token.lower(), index)
    if named is not None:
        return named
    if not re.fullmatch(r"-?\d+", token):
        return None
    value = int(token)
    if value < lo or value > hi:
        return None
    return value


def parse_cron(expression: str) -> tuple[list[set[int]], list[bool]]:
    """Validate a five-field cron expression.

    Six fields are rejected on purpose: the leading seconds column is ambiguous
    with a five-field expression in several ways, and the model has no reason to
    need second-level scheduling for a task it also has to report on.
    """
    text = (expression or "").strip().lower()
    if not text:
        raise CronError("empty schedule")
    fields = text.split()
    if len(fields) == 6:
        raise CronError(
            "six-field cron is not supported; drop the leading seconds field "
            "(use five fields: minute hour day-of-month month day-of-week)"
        )
    if len(fields) != 5:
        raise CronError(
            f"expected 5 cron fields (minute hour day-of-month month day-of-week), got {len(fields)}"
        )
    parsed: list[set[int]] = []
    restricted: list[bool] = []
    for index, field in enumerate(fields):
        values, is_restricted = _parse_field(field, index)
        parsed.append(values)
        restricted.append(is_restricted)
    return parsed, restricted


def _first_of_next_month(moment: datetime) -> datetime:
    year, month = (moment.year + 1, 1) if moment.month == 12 else (moment.year, moment.month + 1)
    return moment.replace(year=year, month=month, day=1, hour=0, minute=0, second=0, microsecond=0)


def _day_matches(moment: datetime, parsed: list[set[int]], restricted: list[bool]) -> bool:
    minutes, hours, days, months, dows = parsed
    if moment.month not in months:
        return False
    dom_hit = moment.day in days
    dow_hit = ((moment.weekday() + 1) % 7) in dows
    if restricted[2] and restricted[4]:
        return dom_hit or dow_hit
    return dom_hit and dow_hit


def next_cron_run(parsed: list[set[int]], restricted: list[bool], after: float, horizon_days: int = 1500) -> float:
    """First matching minute strictly after ``after``. 0.0 if there is none.

    Walks days rather than minutes: a schedule like "0 0 29 2 *" (29 February)
    is four years away, and stepping minute-by-minute to get there would burn
    half a million iterations on every reschedule.
    """
    moment = datetime.fromtimestamp(after).replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(horizon_days):
        if moment.month not in parsed[3]:
            moment = _first_of_next_month(moment)
            continue
        if not _day_matches(moment, parsed, restricted):
            moment = (moment + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            continue
        if moment.hour not in parsed[1]:
            moment = (moment + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
            continue
        if moment.minute not in parsed[0]:
            moment = moment + timedelta(minutes=1)
            continue
        return moment.timestamp()
    return 0.0


# ---- loose phrasing ----------------------------------------------------

_CLOCK = r"(\d{1,2})(?::(\d{2}))?\s*([ap]m)?"
_AT_CLAUSE = re.compile(rf"\bat\s+{_CLOCK}\b")
_EVERY = re.compile(r"\bevery\s+(\d+|a|an|one|two|three|four|five|six|seven)\s*([a-z]+)\b")
_EVERY_BARE = re.compile(
    r"\bevery\s+(minute|hour|day|week|month|weekday|weekend|" + _WEEKDAY_PATTERN + r")\b"
)
# "every 6pm", "every 18:30" - a number with a meridiem or a colon is a time of
# day, not an interval, and it has to be read before _EVERY, which would
# otherwise take "6 pm" as the quantity 6 and "pm" as the unit.
_EVERY_CLOCK = re.compile(rf"\bevery\s+{_CLOCK}\b")
_IN = re.compile(r"\bin\s+(\d+)\s*([a-z]+)\b")
# Words that open a phrased schedule. A cron expression never starts with one,
# so this decides the routing before the token-shape heuristic gets a look.
_LOOSE_START = re.compile(
    r"^(?:every|in|at|on|once|next|after|from|starting|noon|midnight|"
    r"κάθε|σε|στις|στη|την|το|μία|μια|από|μετά)\b"
)
_ISO = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})[t ](\d{1,2}):(\d{2})(?::(\d{2}))?(?:\.\d+)?"
    r"\s*(z|[+-]\d{2}:?\d{2})?"
)
_DATE_ONLY = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
# The whole utterance is nothing but a time of day: "6pm", "at 08:30". Anchored
# so it can only claim a phrase that has no schedule in it.
_CLOCK_ONLY = re.compile(rf"^(?:at\s+|around\s+|περίπου\s+)?{_CLOCK}\.?$")

_SMALL_WORDS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "ten": 10, "fifteen": 15, "twenty": 20, "thirty": 30,
    "sixty": 60,
}


def _duration_seconds(count: int, unit: str) -> int | None:
    seconds = _UNITS.get(unit.rstrip("s"))
    if seconds is None:
        return None
    return count * seconds


def _clock_parts(match: re.Match) -> tuple[int, int]:
    """`(hour, minute)` from an ``8`` / ``08:30`` / ``8pm`` match.

    The meridiem has to be read as a captured group, not by looking for "am"
    somewhere in the matched text: with the old substring test ``6:30 pm``
    reported no meridiem at all and the task was scheduled for 06:30 in the
    morning, with no error anywhere. A schedule that silently fires at the
    wrong time is the one failure this module exists to prevent.
    """
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    suffix = (match.group(3) or "").lower()
    if suffix and not 1 <= hour <= 12:
        raise CronError(f"{match.group(0).strip()!r} is not a time of day")
    if hour > 23 or minute > 59:
        raise CronError(f"{match.group(0).strip()!r} is not a time of day")
    if suffix == "pm" and hour < 12:
        hour += 12
    elif suffix == "am" and hour == 12:
        hour = 0
    return hour, minute


def _resolve_dow(words: str) -> str:
    """Turn "weekdays"/"monday"/"weekends" into a cron day-of-week field."""
    text = words.strip().lower()
    if text.startswith("every"):
        text = text[5:].strip()
    if not text:
        raise CronError(f"cannot work out which days {words!r} means")
    if text.startswith("weekday"):
        return "1-5"
    if text.startswith("weekend"):
        return "0,6"
    if text in _WEEKDAY_ALIASES:
        return str(_WEEKDAY_ALIASES[text])
    raise CronError(
        f"{words!r} is not a day of the week; use names like monday or a range like mon-fri"
    )


def _cron_for_every(count: int, unit: str, whole_phrase: str = "", now: float | None = None) -> str:
    unit = unit.rstrip("s")
    if unit in ("second", "sec"):
        raise CronError(
            "a task cannot run every few seconds; the shortest schedule is one minute "
            "(`* * * * *`)"
        )
    if unit in ("minute", "min"):
        if count < 1:
            raise CronError("the interval has to be at least one minute")
        return f"*/{count} * * * *" if count > 1 else "* * * * *"
    if unit in ("hour", "hr"):
        if count < 1:
            raise CronError("the interval has to be at least one hour")
        return f"0 */{count} * * *" if count > 1 else "0 * * * *"
    if unit == "day":
        clock = _AT_CLAUSE.search(whole_phrase)
        hour, minute = _clock_parts(clock) if clock else (0, 0)
        return f"{minute} {hour} */{count} * *" if count > 1 else f"{minute} {hour} * * *"
    if unit == "week":
        clock = _AT_CLAUSE.search(whole_phrase)
        if count > 1:
            # Day-of-month stepping wraps at the end of every month, so "every 2
            # weeks" is expressed as a fortnight step and the description says so
            # rather than pretending the day-of-month step is exact.
            hour, minute = _clock_parts(clock) if clock else (0, 0)
            return f"{minute} {hour} */{count * 7} * *"
        # A week is a weekday in cron, and the weekday has to be the one the
        # request came in on - "every week" read as "a week from today".
        # datetime.weekday() counts Monday as 0 while a cron day-of-week counts
        # Sunday as 0, so the shift matters: without it every Tuesday landed on
        # Monday and the schedule was a day early.
        hour, minute = _clock_parts(clock) if clock else (9, 0)
        weekday = (datetime.fromtimestamp(now or datetime.now().timestamp()).weekday() + 1) % 7
        return f"{minute} {hour} * * {weekday}"
    if unit == "month":
        return f"0 0 1 */{count} *" if count > 1 else "0 0 1 * *"
    # "every monday", "every tuesday and friday"
    days = _resolve_dow(unit)
    clock = _AT_CLAUSE.search(whole_phrase)
    hour, minute = _clock_parts(clock) if clock else (9, 0)
    return f"{minute} {hour} * * {days}"


def _parse_loose(text: str, now: float) -> Schedule:
    lowered = text.strip().lower()

    # "every 6pm" / "every 18:30" first: the number is a time of day, and
    # _EVERY would otherwise read "6 pm" as the interval 6 with the unit "pm"
    # and answer that pm is not a unit of time.
    every_clock = _EVERY_CLOCK.search(lowered)
    if every_clock and (every_clock.group(3) or ":" in every_clock.group(0)):
        hour, minute = _clock_parts(every_clock)
        return Schedule("cron", cron=f"{minute} {hour} * * *")

    # "every <something>" before anything else, because an "in 20 minutes" that
    # also contains the word "every" is a repeating schedule, not a delay.
    if _EVERY.search(lowered):
        every = _EVERY.search(lowered)
        raw_count = every.group(1)
        count = int(raw_count) if raw_count.isdigit() else _SMALL_WORDS.get(raw_count)
        if count is None or count < 1:
            raise CronError(f"{raw_count!r} is not an interval this scheduler understands")
        return Schedule("cron", cron=_cron_for_every(count, every.group(2), lowered, now))

    bare = _EVERY_BARE.search(lowered)
    if bare:
        word = bare.group(1)
        clock = _AT_CLAUSE.search(lowered)
        if word == "weekday":
            hour, minute = _clock_parts(clock) if clock else (9, 0)
            return Schedule("cron", cron=f"{minute} {hour} * * 1-5")
        if word == "weekend":
            hour, minute = _clock_parts(clock) if clock else (10, 0)
            return Schedule("cron", cron=f"{minute} {hour} * * 0,6")
        return Schedule("cron", cron=_cron_for_every(1, word, lowered, now))

    relative = _IN.search(lowered)
    if relative:
        seconds = _duration_seconds(int(relative.group(1)), relative.group(2))
        if seconds is None:
            raise CronError(f"{relative.group(2)!r} is not a unit of time this scheduler understands")
        return Schedule("once", run_at=now + seconds)

    absolute = _ISO.search(lowered)
    if absolute:
        moment = _iso_moment(absolute)
        if moment.timestamp() <= now:
            raise CronError(
                f"{absolute.group(0)} is in the past; give a future time or a repeating schedule"
            )
        return Schedule("once", run_at=moment.timestamp())

    date_only = _DATE_ONLY.search(lowered)
    if date_only:
        moment = _make_datetime(int(date_only.group(1)), int(date_only.group(2)), int(date_only.group(3)), 9, 0)
        if moment.timestamp() <= now:
            raise CronError(f"{date_only.group(0)} is in the past")
        return Schedule("once", run_at=moment.timestamp())

    clock_only = _CLOCK_ONLY.match(lowered.strip())
    if clock_only:
        hour, minute = _clock_parts(clock_only)
        moment = datetime.fromtimestamp(now).replace(hour=hour, minute=minute, second=0, microsecond=0)
        if moment.timestamp() <= now:
            moment += timedelta(days=1)
        return Schedule("once", run_at=moment.timestamp())

    raise CronError(
        f"{text!r} is not a schedule. Use a cron expression (`0 8 * * 1-5` = 08:00 on weekdays), "
        "a macro (`@daily`), a relative delay (`in 30 minutes`), or a repeating phrase "
        "(`every 15 minutes`, `every weekday at 09:00`)."
    )


def _iso_moment(match: re.Match) -> datetime:
    """Build the moment an `_ISO` match names.

    A string that carries its own UTC offset (or `Z`) is turned into an aware
    datetime and converted by the standard library. Without that, the time
    would be read as machine-local and every such task would fire an hour or
    three off - the timestamp the browser hands back for a one-off edit is
    always in UTC, so this path is the normal one, not an exotic one.
    """
    parts = [int(g) for g in match.groups()[:6] if g is not None]
    while len(parts) < 6:
        parts.append(0)
    zone = (match.group(7) or "").strip()
    if zone:
        text = f"{parts[0]:04d}-{parts[1]:02d}-{parts[2]:02d}T{parts[3]:02d}:{parts[4]:02d}:{parts[5]:02d}"
        if zone.lower() in ("z", "utc", "gmt"):
            text += "+00:00"
        else:
            digits = zone[1:].replace(":", "")
            if len(digits) != 4 or int(digits[:2]) > 14 or int(digits[2:]) > 59:
                raise CronError(f"{zone!r} is not a UTC offset")
            text += f"{zone[0]}{digits[:2]}:{digits[2:]}"
        try:
            return datetime.fromisoformat(text)
        except ValueError as exc:
            raise CronError(f"{match.group(0)!r} is not a time ({exc})") from exc
    return _make_datetime(*parts)


def _make_datetime(year: int, month: int, day: int, hour: int, minute: int, second: int = 0) -> datetime:
    try:
        return datetime(year, month, day, hour, minute, second)
    except ValueError as exc:
        raise CronError(f"{year}-{month:02d}-{day:02d} {hour:02d}:{minute:02d} is not a real time ({exc})") from exc


def parse_schedule(text: str, now: float | None = None) -> Schedule:
    """Turn whatever the model passed into a Schedule.

    A cron expression is preferred and is what the tool description asks for;
    the loose phrasing is a safety net for the cases where the model writes
    "every morning" instead of doing the conversion. Anything still ambiguous is
    a CronError rather than a guess, because a task that fires at the wrong time
    is worse than one that was refused.
    """
    now = float(now if now is not None else datetime.now().timestamp())
    source = (text or "").strip()
    if not source:
        raise CronError("no schedule given")

    lowered = source.lower()
    if lowered in _MACROS:
        return Schedule("cron", cron=_MACROS[lowered])

    # A phrase that opens with a word no cron field can be is phrasing, however
    # many digits it goes on to contain: "every weekday at 5 pm" is five tokens
    # with a digit in it, and the shape test below was therefore reading it as a
    # mistyped expression and answering "'every' is not a valid minute value".
    # The first field of a cron expression is a number, a `*` or a range, so a
    # leading English or Greek word settles it.
    if _LOOSE_START.match(lowered):
        return _parse_loose(source, now)

    # Five or six whitespace-separated tokens made only of characters a cron
    # field can contain, and containing at least one digit or a `*`, are treated
    # as cron even when they are wrong, so the error names the field that is
    # wrong. Five bare words ("whenever i feel like it") are not an expression
    # the model mistyped, so they go to the phrasing parser, which knows how to
    # explain "every other week at six".
    tokens = source.split()
    cron_shaped = (
        len(tokens) in (5, 6)
        and all(re.fullmatch(r"[A-Za-z0-9*/,-]+", token) for token in tokens)
        and any(any(char.isdigit() or char == "*" for char in token) for token in tokens)
    )
    if cron_shaped:
        parsed, restricted = parse_cron(source)
        canonical = " ".join(lowered.split())
        return Schedule("cron", cron=canonical, run_at=next_cron_run(parsed, restricted, now))

    return _parse_loose(source, now)


def next_run(schedule: dict | Schedule, after: float | None = None) -> float:
    """Epoch seconds of the next firing, or 0.0 when there is none.

    Accepts either a Schedule or a task row, because the scheduler thread works
    with rows straight out of the database and never builds a Schedule first.
    """
    after = float(after if after is not None else datetime.now().timestamp())
    if isinstance(schedule, Schedule):
        if not schedule.repeating:
            return schedule.run_at
        expression = schedule.cron
    else:
        if (schedule.get("schedule") or "cron") != "cron":
            return float(schedule.get("run_at") or 0.0)
        expression = str(schedule.get("cron") or "")
    if not expression:
        return 0.0
    parsed, restricted = parse_cron(expression)
    return next_cron_run(parsed, restricted, after)


_WEEKDAY_LABELS = ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat")
_MONTH_LABELS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def describe_schedule(expression: str) -> str:
    """A short human phrase for a cron expression, for the Tasks tab.

    Built from the *raw* fields, not from the expanded value sets: `*/5` expands
    to {0,5,...,55}, and describing that as "every 55 minutes" is worse than
    useless when the operator is checking that APEX understood "every five
    minutes".

    Falls back to the raw expression when it cannot be parsed. Showing something
    odd the operator can compare against what they asked for beats showing
    nothing.
    """
    if not expression:
        return ""
    try:
        parse_cron(expression)
    except CronError:
        return expression
    minute_f, hour_f, day_f, month_f, dow_f = expression.strip().lower().split()

    if minute_f == "*" and hour_f == "*":
        when = "every minute"
    elif minute_f.startswith("*/"):
        when = f"every {minute_f[2:]} minutes"
    elif hour_f.startswith("*/"):
        when = f"every {hour_f[2:]} hours, at minute {minute_f}"
    elif hour_f == "*":
        when = "every hour" if minute_f == "0" else f"every hour at minute {minute_f}"
    elif minute_f == "*":
        when = f"every minute between {hour_f}:00 and {hour_f}:59"
    else:
        if hour_f.isdigit() and minute_f.isdigit():
            when = "at " + ", ".join(
                _clock(hour, minute) for hour in hour_f.split(",") for minute in minute_f.split(",")
            )
        elif minute_f.isdigit():
            # "0 9-17 * * *" - an hour range/list, so the minute is what repeats.
            when = f"at :{int(minute_f):02d} every hour, hours {hour_f}"
        else:
            when = f"minutes {minute_f} of every hour, hours {hour_f}"

    day_part = _day_phrase(day_f, dow_f)
    month_part = "" if month_f == "*" else " in " + ", ".join(
        _MONTH_LABELS[int(value) - 1] if value.isdigit() else value for value in month_f.split(",")
    )
    return f"{when}{day_part}{month_part}"


def _day_phrase(day_field: str, dow_field: str) -> str:
    """Day part of the description, honouring cron's dom-or-dow rule."""
    dom = "" if day_field == "*" else (
        "day " + day_field if "," not in day_field and "-" not in day_field else f"days {day_field}"
    )
    if dow_field == "*":
        return f" on {dom}" if dom else ""
    names = ", ".join(_dow_label(value) for value in dow_field.split(","))
    if not dom:
        return f" on {names}"
    return f" on {dom} or {names}"


def _clock(hour: str, minute: str) -> str:
    """`08:00` for `0 8`, left alone for a named or ranged field."""
    return f"{int(hour):02d}:{int(minute):02d}" if hour.isdigit() and minute.isdigit() else f"{hour}:{minute}"


def _dow_label(value: str) -> str:
    if value == "1-5":
        return "weekdays"
    if value == "0,6":
        return "weekends"
    if value in _WEEKDAY_ALIASES:
        return value.capitalize()
    if value.isdigit():
        return _WEEKDAY_LABELS[int(value) % 7]
    return value


def format_next_run(epoch: float, fmt: str = "%Y-%m-%d %H:%M") -> str:
    if not epoch:
        return "-"
    return datetime.fromtimestamp(float(epoch)).strftime(fmt)
