"""lineage.jobs — D1: what is scheduled to run, and how often it says it runs.

The point of this module is one number: **max_gap_days**, the longest interval
between two consecutive firings of a job.

That number is what makes discovery useful in week one instead of in thirteen
months. A job with max_gap_days=31 that we have never observed is not an
unknown -- it is a known monthly job with no observations yet, and those are
completely different claims. Schedules *declare* their frequency in
machine-readable form; we do not have to wait for an end-of-month to learn that
the month-end close exists.

Pure functions first (parsing, gap computation, coverage math); collectors that
shell out to the OS at the bottom.
"""
from __future__ import annotations

import calendar
import datetime as dt
import json
import os
import platform
import re
import subprocess
import xml.etree.ElementTree as ET

# Bands are for humans; max_gap_days is the number that drives decisions.
FREQUENCY_BANDS = [
    (1.0, "subdaily"),
    (1.5, "daily"),
    (8.0, "weekly"),
    (32.0, "monthly"),
    (95.0, "quarterly"),
    (370.0, "annual"),
]

_MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_abbr) if m}
_DOWS = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}

CRON_ALIASES = {
    "@yearly": "0 0 1 1 *", "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *", "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *", "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}


class CronError(ValueError):
    pass


# --------------------------------------------------------------------------
# cron
# --------------------------------------------------------------------------
def _parse_field(spec, lo, hi, names=None):
    """One cron field to a set of ints. Handles * , - / and names."""
    spec = str(spec).strip()
    if not spec:
        raise CronError("empty field")
    out = set()
    for part in spec.split(","):
        step = 1
        if "/" in part:
            part, _, step_s = part.partition("/")
            if not step_s.isdigit() or int(step_s) < 1:
                raise CronError(f"bad step in {spec!r}")
            step = int(step_s)
        if part in ("*", ""):
            start, end = lo, hi
        elif "-" in part[1:] or (part.startswith("-") and False):
            a, _, b = part.partition("-")
            start, end = _named(a, names, lo, hi), _named(b, names, lo, hi)
        else:
            start = end = _named(part, names, lo, hi)
            if step > 1:                     # `5/10` means 5,15,25... in most crons
                end = hi
        if start > end:
            raise CronError(f"reversed range in {spec!r}")
        out.update(range(start, end + 1, step))
    if not out:
        raise CronError(f"field matches nothing: {spec!r}")
    return out


def _named(token, names, lo, hi):
    token = token.strip().lower()
    if names and token in names:
        value = names[token]
    elif re.fullmatch(r"\d+", token):
        value = int(token)
    else:
        raise CronError(f"unparseable value: {token!r}")
    if value == 7 and names is _DOWS:        # both 0 and 7 mean Sunday
        value = 0
    if not lo <= value <= hi:
        raise CronError(f"{value} out of range {lo}-{hi}")
    return value


def parse_cron(expr):
    """-> (minutes, hours, doms, months, dows, dom_restricted, dow_restricted)."""
    expr = str(expr).strip()
    if expr.startswith("@"):
        alias = CRON_ALIASES.get(expr.lower())
        if alias is None:
            # @reboot and friends have no periodicity to compute.
            raise CronError(f"non-periodic schedule: {expr}")
        expr = alias
    fields = expr.split()
    if len(fields) < 5:
        raise CronError(f"expected 5 fields, got {len(fields)}: {expr!r}")
    minute, hour, dom, month, dow = fields[:5]
    return (
        _parse_field(minute, 0, 59),
        _parse_field(hour, 0, 23),
        _parse_field(dom, 1, 31),
        _parse_field(month, 1, 12, _MONTHS),
        _parse_field(dow, 0, 6, _DOWS),
        dom.strip() != "*",
        dow.strip() != "*",
    )


def cron_max_gap_days(expr, years=4, start_year=2025):
    """Longest interval between consecutive firings, in days.

    ponytail: simulated day by day over four years rather than reasoned about
    symbolically. Cron's day rules are genuinely fiddly -- notably that when
    BOTH day-of-month and day-of-week are restricted they are OR'd, not AND'd,
    which is the classic source of wrong answers here. Simulation gets that and
    leap years for free at ~1,500 cheap iterations. Four years covers annual and
    quarterly patterns; anything rarer than that reports as annual+ anyway.
    """
    minutes, hours, doms, months, dows, dom_r, dow_r = parse_cron(expr)

    fires = []
    day = dt.date(start_year, 1, 1)
    end = dt.date(start_year + years, 1, 1)
    while day < end:
        if day.month in months:
            dom_hit = day.day in doms
            dow_hit = ((day.weekday() + 1) % 7) in dows      # Monday=0 -> Sunday=0
            # The OR quirk: restricted on both means either matches.
            hit = (dom_hit or dow_hit) if (dom_r and dow_r) else (dom_hit and dow_hit)
            if hit:
                fires.append(day)
        day += dt.timedelta(days=1)

    # Gaps inside a firing day, then from one firing day's last run to the next
    # firing day's first. The first version short-circuited any schedule with
    # several runs a day to its intraday gap, so `0 8-18/2 * * 1-5` read as
    # 14 hours when Friday 18:00 -> Monday 08:00 is 2.6 days. Caught by the
    # conformance harness on its first run (tools/conformance.py).
    times = sorted(h * 60 + m for h in hours for m in minutes)
    if len(fires) * len(times) < 2:
        return None                     # fires once or never in the window
    gaps = [b - a for a, b in zip(times, times[1:])]
    gaps += [(b - a).days * 1440 + times[0] - times[-1] for a, b in zip(fires, fires[1:])]
    return max(gaps) / 1440.0


def format_window(days):
    """A coverage claim nobody can read is not an honest one.

    "0.0 days" is what two 45-second sweeps produce, and it reads as a bug or a
    shrug rather than as the warning it is. Lives here because both the job
    coverage statement and the scream test must phrase the window identically --
    two different renderings of the same number is how a caveat gets ignored.
    """
    days = float(days)
    if days <= 0:
        return "no observation"
    if days < 1 / 24.0:
        return f"{days * 24 * 60:.1f} minutes"
    if days < 1:
        return f"{days * 24:.1f} hours"
    return f"{days:.1f} days"


def classify_frequency(max_gap_days):
    if max_gap_days is None:
        return "unknown"
    for threshold, name in FREQUENCY_BANDS:
        if max_gap_days < threshold:
            return name
    return "rare"


# --------------------------------------------------------------------------
# Windows Task Scheduler
# --------------------------------------------------------------------------
def _strip_ns(tag):
    return tag.rsplit("}", 1)[-1]


def _iso_duration_days(text):
    """P1D / PT12H / P1M -- enough of ISO 8601 for Task Scheduler intervals."""
    m = re.fullmatch(r"P(?:(\d+)Y)?(?:(\d+)M)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?",
                     str(text).strip(), re.I)
    if not m:
        return None
    y, mo, d, h, mi, s = (int(g or 0) for g in m.groups())
    total = y * 365 + mo * 30 + d + h / 24 + mi / 1440 + s / 86400
    return total or None


def task_max_gap_days(xml_text):
    """Longest gap declared by a Task Scheduler trigger set, in days.

    Returns None for event-driven triggers (boot, logon, on-event): they have no
    period, and calling that "annual" would be a lie in the dangerous direction.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None

    triggers = None
    for el in root.iter():
        if _strip_ns(el.tag) == "Triggers":
            triggers = el
            break
    if triggers is None:
        return None

    gaps = []
    for trig in triggers:
        if trig.findtext("./{*}Enabled", default="true").strip().lower() == "false":
            continue
        kind = _strip_ns(trig.tag)

        # A repetition interval overrides the outer schedule's spacing.
        rep = trig.find("./{*}Repetition/{*}Interval")
        if rep is not None and rep.text:
            days = _iso_duration_days(rep.text)
            if days:
                gaps.append(days)
                continue

        if kind == "CalendarTrigger":
            gaps.append(_calendar_gap(trig))
        elif kind == "TimeTrigger":
            gaps.append(None)            # one-shot unless it had a Repetition
        else:
            gaps.append(None)            # Boot/Logon/Event/Session: not periodic

    real = [g for g in gaps if g is not None]
    if not real:
        return None
    # Multiple triggers: the job fires on whichever comes first, so the estate's
    # exposure is the *smallest* gap.
    return min(real)


def _calendar_gap(trig):
    by_day = trig.find("./{*}ScheduleByDay")
    if by_day is not None:
        return max(1, int(by_day.findtext("./{*}DaysInterval", default="1") or 1))

    by_week = trig.find("./{*}ScheduleByWeek")
    if by_week is not None:
        weeks = max(1, int(by_week.findtext("./{*}WeeksInterval", default="1") or 1))
        days = [_strip_ns(d.tag) for d in by_week.findall("./{*}DaysOfWeek/*")]
        if weeks > 1 or len(days) <= 1:
            return 7 * weeks
        order = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
        idx = sorted(order.index(d) for d in days if d in order)
        if not idx:
            return 7 * weeks
        gaps = [b - a for a, b in zip(idx, idx[1:])] + [idx[0] + 7 - idx[-1]]
        return max(gaps)

    by_month = trig.find("./{*}ScheduleByMonth")
    if by_month is not None:
        months = [_strip_ns(m.tag) for m in by_month.findall("./{*}Months/*")]
        n = len(months) or 12
        return 366.0 / n if n < 12 else 31.0

    by_mdow = trig.find("./{*}ScheduleByMonthDayOfWeek")
    if by_mdow is not None:
        months = [_strip_ns(m.tag) for m in by_mdow.findall("./{*}Months/*")]
        n = len(months) or 12
        return 366.0 / n if n < 12 else 31.0

    return None


# --------------------------------------------------------------------------
# Coverage -- the sentence the UI has to show
# --------------------------------------------------------------------------
def coverage(jobs, window_days):
    """How much of the estate can this observation window actually see?

    "No activity observed" is only honest next to this. A tool that reports
    quiet nodes without reporting that a quarter of the estate runs less often
    than it has been watching will get someone to decommission the wrong box.
    """
    total = len(jobs)
    observed = [j for j in jobs if j.get("observations", 0) > 0]
    invisible, unknown = [], []
    for j in jobs:
        gap = j.get("max_gap_days")
        if gap is None:
            unknown.append(j)
        elif gap > window_days:
            invisible.append(j)

    pct = round(100.0 * len(invisible) / total, 1) if total else 0.0
    return {
        "window_days": window_days,
        "jobs_total": total,
        "jobs_observed": len(observed),
        "jobs_slower_than_window": len(invisible),
        "pct_slower_than_window": pct,
        "jobs_unknown_frequency": len(unknown),
        "declared_never_observed": [
            j["id"] for j in jobs
            if j.get("observations", 0) == 0 and j.get("max_gap_days") is not None
        ],
        "statement": (
            f"Observed {format_window(window_days)}. {len(observed)}/{total} jobs seen running; "
            f"{len(invisible)} ({pct}%) run less often than the window and "
            f"{len(unknown)} have no declared period."
        ),
    }


# --------------------------------------------------------------------------
# Collectors
# --------------------------------------------------------------------------
_CRON_FILES = ("/etc/crontab",)
_CRON_DIRS = ("/etc/cron.d", "/var/spool/cron/crontabs", "/var/spool/cron")
_CRON_ENV = re.compile(r"^\s*[A-Za-z_][A-Za-z0-9_]*\s*=")


def parse_crontab(text, source="", system_format=True):
    """System crontabs have a user field between schedule and command."""
    jobs = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#") or _CRON_ENV.match(line):
            continue
        try:
            if line.startswith("@"):
                schedule, _, rest = line.partition(" ")
            else:
                parts = line.split(None, 5)
                if len(parts) < 6:
                    continue
                schedule, rest = " ".join(parts[:5]), parts[5]
            user = ""
            if system_format:
                bits = rest.split(None, 1)
                if bits:
                    user, rest = bits[0], (bits[1] if len(bits) > 1 else "")
            gap = cron_max_gap_days(schedule)
        except CronError:
            schedule, gap, user, rest = line, None, "", line
        jobs.append({
            "id": f"{source}:{lineno}",
            "kind": "cron",
            "source": source,
            "schedule": schedule,
            "user": user,
            "command": rest.strip(),
            "max_gap_days": gap,
            "frequency": classify_frequency(gap),
            "observations": 0,
        })
    return jobs


def collect_cron_jobs():
    jobs = []
    for path in _CRON_FILES:
        if os.path.isfile(path):
            try:
                jobs += parse_crontab(open(path, encoding="utf-8", errors="replace").read(), path)
            except OSError:
                pass
    for d in _CRON_DIRS:
        if not os.path.isdir(d):
            continue
        system = not d.startswith("/var/spool")
        for name in sorted(os.listdir(d)):
            p = os.path.join(d, name)
            if not os.path.isfile(p) or name.endswith((".dpkg-dist", ".rpmnew", "~")):
                continue
            try:
                jobs += parse_crontab(open(p, encoding="utf-8", errors="replace").read(), p,
                                      system_format=system)
            except OSError:
                pass
    return jobs


def collect_scheduled_tasks():
    """Windows Task Scheduler, via schtasks /xml. One spawn for the whole tree."""
    if platform.system() != "Windows":
        return []
    try:
        proc = subprocess.run(["schtasks", "/query", "/xml", "ONE"],
                              capture_output=True, text=True, timeout=120)
    except Exception:
        return []
    if proc.returncode != 0 or not proc.stdout.strip():
        return []

    # schtasks concatenates <Task> documents under one root; wrap defensively.
    text = proc.stdout.lstrip("﻿")
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        try:
            root = ET.fromstring(f"<Tasks>{re.sub(r'<[?]xml[^>]*[?]>', '', text)}</Tasks>")
        except ET.ParseError:
            return []

    tasks = [root] if _strip_ns(root.tag) == "Task" else [
        e for e in root.iter() if _strip_ns(e.tag) == "Task"]

    jobs = []
    for task in tasks:
        xml_text = ET.tostring(task, encoding="unicode")
        gap = task_max_gap_days(xml_text)
        uri = task.findtext(".//{*}RegistrationInfo/{*}URI") or ""
        command = " ".join(filter(None, [
            (task.findtext(".//{*}Exec/{*}Command") or "").strip(),
            (task.findtext(".//{*}Exec/{*}Arguments") or "").strip(),
        ]))
        jobs.append({
            "id": uri or (task.findtext(".//{*}RegistrationInfo/{*}Description") or "task"),
            "kind": "scheduled-task",
            "source": "Task Scheduler",
            "schedule": _trigger_summary(task),
            "user": task.findtext(".//{*}Principal/{*}UserId") or "",
            "command": command,
            "max_gap_days": gap,
            "frequency": classify_frequency(gap),
            "enabled": (task.findtext(".//{*}Settings/{*}Enabled") or "true").lower() != "false",
            "observations": 0,
        })
    return jobs


def _trigger_summary(task):
    kinds = []
    for el in task.iter():
        tag = _strip_ns(el.tag)
        if tag.endswith("Trigger"):
            kinds.append(tag)
    return ",".join(sorted(set(kinds))) or "none"


def collect_jobs():
    """Everything schedulable on this host."""
    if platform.system() == "Windows":
        return collect_scheduled_tasks()
    return collect_cron_jobs()


def main(argv=None):
    jobs = collect_jobs()
    print(json.dumps({"jobs": jobs, "coverage": coverage(jobs, 14)}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
