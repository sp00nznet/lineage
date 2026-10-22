"""lineage.seasons — D2: compare this cycle's window against the same phase last cycle.

The originating incident:

    Someone removes a mount in a quiet week. Nothing notices. Three months
    later a client uploads 20 GB of CSVs for end-of-month processing and that
    is when everyone discovers the mount was load-bearing.

A drift alert against one frozen baseline does not catch this, because the
estate legitimately changes all the time and the alert drowns. What catches it
is a **same-phase** comparison:

    "This mount was present during last month's EOM window and is absent now."

Note what this does NOT need: no dependency graph, no observation collector, no
file I/O tracing. Only periodic snapshots and a calendar. It is a fraction of
D3-D5's work, it is useful with one month of history instead of thirteen, and
it addresses the actual failure. That is why it ships first.

opsview already has blessed baselines and a drift view; this is baselines with
a calendar attached.
"""
from __future__ import annotations

import calendar
import datetime as dt

DEFAULT_PHASES = {
    # name: (kind, params)
    "eom": {"kind": "month_end", "days": 7},
    "som": {"kind": "month_start", "days": 3},
    "quarter_end": {"kind": "quarter_end", "days": 7},
    # Tax season, or whatever the estate's annual crunch is. Configurable.
    "annual": {"kind": "annual", "start": (2, 1), "end": (4, 15)},
}


def _month_len(year, month):
    return calendar.monthrange(year, month)[1]


def _shift_month(year, month, delta):
    index = (year * 12 + (month - 1)) + delta
    return index // 12, index % 12 + 1


def month_end_window(year, month, days):
    last = _month_len(year, month)
    return dt.date(year, month, max(1, last - days + 1)), dt.date(year, month, last)


def month_start_window(year, month, days):
    return dt.date(year, month, 1), dt.date(year, month, min(days, _month_len(year, month)))


def _is_quarter_month(month):
    return month in (3, 6, 9, 12)


def phase_of(when, phases=None):
    """Which configured phases contain this date. A date can be in several."""
    phases = phases or DEFAULT_PHASES
    d = when.date() if isinstance(when, dt.datetime) else when
    hits = []
    for name, cfg in phases.items():
        kind = cfg["kind"]
        if kind == "month_end":
            start, end = month_end_window(d.year, d.month, cfg["days"])
        elif kind == "month_start":
            start, end = month_start_window(d.year, d.month, cfg["days"])
        elif kind == "quarter_end":
            if not _is_quarter_month(d.month):
                continue
            start, end = month_end_window(d.year, d.month, cfg["days"])
        elif kind == "annual":
            start = dt.date(d.year, *cfg["start"])
            end = dt.date(d.year, *cfg["end"])
        else:
            continue
        if start <= d <= end:
            hits.append(name)
    return hits


def current_window(name, when, phases=None):
    """The window of `name` that contains or most recently preceded `when`."""
    cfg = (phases or DEFAULT_PHASES)[name]
    d = when.date() if isinstance(when, dt.datetime) else when
    kind = cfg["kind"]

    if kind == "month_end":
        return month_end_window(d.year, d.month, cfg["days"])
    if kind == "month_start":
        return month_start_window(d.year, d.month, cfg["days"])
    if kind == "quarter_end":
        year, month = d.year, d.month
        while not _is_quarter_month(month):
            year, month = _shift_month(year, month, -1)
        return month_end_window(year, month, cfg["days"])
    if kind == "annual":
        year = d.year if d >= dt.date(d.year, *cfg["start"]) else d.year - 1
        return dt.date(year, *cfg["start"]), dt.date(year, *cfg["end"])
    raise ValueError(f"unknown phase kind: {kind}")


def previous_window(name, when, phases=None, back=1):
    """The equivalent window one (or `back`) cycles earlier.

    Month arithmetic, not a 30-day subtraction: the last 7 days of February and
    the last 7 days of March are different absolute offsets, and getting that
    wrong silently compares the wrong weeks.
    """
    cfg = (phases or DEFAULT_PHASES)[name]
    start, _ = current_window(name, when, phases)
    kind = cfg["kind"]

    if kind in ("month_end", "month_start"):
        year, month = _shift_month(start.year, start.month, -back)
        fn = month_end_window if kind == "month_end" else month_start_window
        return fn(year, month, cfg["days"])
    if kind == "quarter_end":
        year, month = _shift_month(start.year, start.month, -3 * back)
        return month_end_window(year, month, cfg["days"])
    if kind == "annual":
        year = start.year - back
        return dt.date(year, *cfg["start"]), dt.date(year, *cfg["end"])
    raise ValueError(f"unknown phase kind: {kind}")


# --------------------------------------------------------------------------
# Diffing
# --------------------------------------------------------------------------
def snapshot_from_reports(reports):
    """{(host, item): value} from a list of agent reports.

    Last write wins, which is right: within one phase window we care whether the
    fact was true at the end of it.
    """
    snap = {}
    for report in sorted(reports, key=lambda r: r.get("sent_at", 0)):
        host = report.get("hostname") or report.get("agent_id") or "?"
        for r in report.get("results", []):
            if r.get("error"):
                continue          # a fact we could not read is not a fact that changed
            snap[(host, r.get("id"))] = r.get("value")
    return snap


def diff_snapshots(before, after):
    """What changed between two same-phase snapshots."""
    keys_before, keys_after = set(before), set(after)
    removed = sorted(keys_before - keys_after)
    added = sorted(keys_after - keys_before)
    changed = sorted(k for k in (keys_before & keys_after) if before[k] != after[k])
    return {
        "removed": [{"host": h, "item": i, "was": before[(h, i)]} for h, i in removed],
        "added": [{"host": h, "item": i, "now": after[(h, i)]} for h, i in added],
        "changed": [{"host": h, "item": i, "was": before[(h, i)], "now": after[(h, i)]}
                    for h, i in changed],
    }


def rank_by_job_exposure(diff, jobs, phase_name):
    """Rank changes by whether a job that runs in this phase depends on them.

    Raw change counts are noise -- estates change constantly. "14 things changed,
    3 of them are touched by jobs that run during EOM" is the alert worth
    sending. Everything else is a report nobody opens.
    """
    exposed_hosts = {j.get("host") for j in jobs if phase_name in (j.get("phases") or [])}
    out = []
    for bucket in ("removed", "changed", "added"):
        for entry in diff[bucket]:
            risky = entry["host"] in exposed_hosts
            out.append({**entry, "change": bucket, "phase_exposed": risky})
    # Removals first (a thing that vanished is the incident shape), then exposure.
    order = {"removed": 0, "changed": 1, "added": 2}
    out.sort(key=lambda e: (not e["phase_exposed"], order[e["change"]]))
    return out


def seasonal_report(name, now, before_reports, after_reports, jobs=(), phases=None):
    """The whole D2 output for one phase."""
    prev_start, prev_end = previous_window(name, now, phases)
    cur_start, cur_end = current_window(name, now, phases)
    before = snapshot_from_reports(before_reports)
    after = snapshot_from_reports(after_reports)
    diff = diff_snapshots(before, after)
    ranked = rank_by_job_exposure(diff, jobs, name)
    exposed = sum(1 for e in ranked if e["phase_exposed"])
    total = len(ranked)
    return {
        "phase": name,
        "previous_window": [prev_start.isoformat(), prev_end.isoformat()],
        "current_window": [cur_start.isoformat(), cur_end.isoformat()],
        "changes": ranked,
        "summary": (
            f"{total} change(s) since the equivalent {name} window "
            f"({prev_start.isoformat()}..{prev_end.isoformat()}); "
            f"{exposed} touch hosts with jobs that run in this phase."
        ),
    }
