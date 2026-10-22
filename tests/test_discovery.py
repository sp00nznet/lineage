"""D1 + D2. The cases that matter are the ones that would have missed the incident.

Runs under pytest, or standalone: `python tests/test_discovery.py`
"""
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lineage.jobs import (  # noqa: E402
    CronError, classify_frequency, coverage, cron_max_gap_days, parse_crontab,
    task_max_gap_days,
)
from lineage.seasons import (  # noqa: E402
    current_window, diff_snapshots, phase_of, previous_window,
    rank_by_job_exposure, seasonal_report, snapshot_from_reports,
)


def approx(a, b, tol=0.51):
    return a is not None and abs(a - b) <= tol


# ==========================================================================
# D1 -- declared frequency
# ==========================================================================
def test_cron_basic_periods():
    assert approx(cron_max_gap_days("0 2 * * *"), 1)          # daily
    assert approx(cron_max_gap_days("0 2 * * 1"), 7)          # weekly
    assert approx(cron_max_gap_days("0 0 1 * *"), 31)         # monthly
    assert approx(cron_max_gap_days("@daily"), 1)
    assert approx(cron_max_gap_days("@monthly"), 31)
    assert approx(cron_max_gap_days("@weekly"), 7)


def test_the_month_end_job():
    """`0 2 28-31 * *` -- the shape the whole feature exists for.

    It fires on consecutive days at month end, so the *gap* is ~4 weeks: a
    two-week observation window cannot see it, and that is exactly what the
    coverage math has to know.
    """
    gap = cron_max_gap_days("0 2 28-31 * *")
    assert 27 <= gap <= 29, gap
    assert classify_frequency(gap) == "monthly"


def test_the_annual_job():
    gap = cron_max_gap_days("0 3 1 4 *")           # 03:00, April 1st
    assert 364 <= gap <= 366, gap
    assert classify_frequency(gap) == "annual"
    # Quarterly: Jan/Apr/Jul/Oct
    assert classify_frequency(cron_max_gap_days("0 3 1 1,4,7,10 *")) == "quarterly"


def test_subdaily():
    assert cron_max_gap_days("*/5 * * * *") < 0.01
    assert approx(cron_max_gap_days("0 */6 * * *"), 0.25, 0.01)
    assert classify_frequency(cron_max_gap_days("*/15 * * * *")) == "subdaily"
    # Two firings a day is not "twice as covered": 01:00 and 23:00 leaves a 22h
    # blind spot, and max gap -- not average spacing -- is what exposure means.
    assert approx(cron_max_gap_days("0 1,23 * * *"), 22 / 24.0, 0.01)


def test_dom_and_dow_are_ORed_not_ANDed():
    """The classic cron gotcha. Both restricted -> either matches.

    `0 0 1 * 1` is "the 1st of the month OR every Monday", so the gap is a week,
    not a month. Reading it as AND overstates the interval and would make the
    tool report a weekly job as invisible to a two-week window.
    """
    assert approx(cron_max_gap_days("0 0 1 * 1"), 7), cron_max_gap_days("0 0 1 * 1")
    # Only one restricted -> normal AND semantics.
    assert approx(cron_max_gap_days("0 0 * * 1"), 7)
    assert approx(cron_max_gap_days("0 0 1 * *"), 31)


def test_cron_syntax_variants():
    assert approx(cron_max_gap_days("0 0 * * MON"), 7)
    assert approx(cron_max_gap_days("0 0 1 JAN,JUL *"), 181, 3)
    assert approx(cron_max_gap_days("0 0 * * 0"), 7)
    assert approx(cron_max_gap_days("0 0 * * 7"), 7)       # 7 is also Sunday
    assert approx(cron_max_gap_days("0 0 1,15 * *"), 17, 2)


def test_cron_rejects_junk_and_non_periodic():
    for bad in ("", "not a cron", "0 0 * *", "99 0 * * *", "0 0 * * 9"):
        try:
            cron_max_gap_days(bad)
            raise AssertionError(f"{bad!r} should not parse")
        except CronError:
            pass
    try:
        cron_max_gap_days("@reboot")
        raise AssertionError("@reboot has no period and must not claim one")
    except CronError:
        pass


def test_crontab_parsing():
    text = """
# comment
SHELL=/bin/sh
0 2 28-31 * *  root  /opt/legacy/month_end.pl
*/5 * * * *    nobody /usr/local/bin/poll.sh
@daily         root  /usr/bin/backup
"""
    jobs = parse_crontab(text, "/etc/cron.d/legacy")
    assert len(jobs) == 3
    by_cmd = {j["command"].split()[0]: j for j in jobs}
    assert by_cmd["/opt/legacy/month_end.pl"]["frequency"] == "monthly"
    assert by_cmd["/opt/legacy/month_end.pl"]["user"] == "root"
    assert by_cmd["/usr/local/bin/poll.sh"]["frequency"] == "subdaily"
    assert by_cmd["/usr/bin/backup"]["frequency"] == "daily"


def test_task_scheduler_triggers():
    def xml(inner):
        return f'<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task"><Triggers>{inner}</Triggers></Task>'

    daily = xml('<CalendarTrigger><ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay></CalendarTrigger>')
    assert approx(task_max_gap_days(daily), 1)

    monthly = xml('<CalendarTrigger><ScheduleByMonth><DaysOfMonth><Day>1</Day></DaysOfMonth></ScheduleByMonth></CalendarTrigger>')
    assert classify_frequency(task_max_gap_days(monthly)) == "monthly"

    annual = xml('<CalendarTrigger><ScheduleByMonth><Months><April/></Months>'
                 '<DaysOfMonth><Day>15</Day></DaysOfMonth></ScheduleByMonth></CalendarTrigger>')
    assert classify_frequency(task_max_gap_days(annual)) == "annual"

    # Event-driven has no period, and must not be given one.
    assert task_max_gap_days(xml("<BootTrigger/>")) is None
    assert task_max_gap_days(xml("<LogonTrigger/>")) is None

    # Repetition overrides the outer schedule.
    rep = xml('<TimeTrigger><Repetition><Interval>PT1H</Interval></Repetition></TimeTrigger>')
    assert approx(task_max_gap_days(rep), 1 / 24.0, 0.01)

    # Two triggers: exposure is the shorter gap.
    both = xml('<CalendarTrigger><ScheduleByMonth><DaysOfMonth><Day>1</Day></DaysOfMonth></ScheduleByMonth></CalendarTrigger>'
               '<CalendarTrigger><ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay></CalendarTrigger>')
    assert approx(task_max_gap_days(both), 1)

    # Disabled triggers do not count. Found on real hardware: Windows' own
    # "Data Integrity Check And Scan" ships with a daily CalendarTrigger and a
    # BootTrigger, both Enabled=false. Reporting it as daily would put a job
    # that never runs into the observed-coverage numbers.
    disabled = xml('<CalendarTrigger><Enabled>false</Enabled>'
                   '<ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay></CalendarTrigger>')
    assert task_max_gap_days(disabled) is None
    mixed = xml('<CalendarTrigger><Enabled>false</Enabled>'
                '<ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay></CalendarTrigger>'
                '<CalendarTrigger><ScheduleByDay><DaysInterval>7</DaysInterval></ScheduleByDay></CalendarTrigger>')
    assert approx(task_max_gap_days(mixed), 7)

    assert task_max_gap_days("<not xml") is None


def test_coverage_tells_the_truth_about_the_window():
    jobs = [
        {"id": "poll", "max_gap_days": 0.01, "observations": 400},
        {"id": "backup", "max_gap_days": 1, "observations": 9},
        {"id": "month-end-close", "max_gap_days": 28, "observations": 0},
        {"id": "annual-extract", "max_gap_days": 365, "observations": 0},
        {"id": "on-boot", "max_gap_days": None, "observations": 0},
    ]
    c = coverage(jobs, window_days=9)
    assert c["jobs_slower_than_window"] == 2
    assert c["jobs_unknown_frequency"] == 1
    assert set(c["declared_never_observed"]) == {"month-end-close", "annual-extract"}
    assert "9.0 days" in c["statement"]
    # Sub-day windows must stay legible: "0.0 days" reads as a bug, not a warning.
    assert "minutes" in coverage(jobs, window_days=0.0008)["statement"]
    # A longer window sees the monthly job but still not the annual one.
    assert coverage(jobs, window_days=45)["jobs_slower_than_window"] == 1


# ==========================================================================
# D2 -- seasonal comparison
# ==========================================================================
def test_phase_detection():
    assert "eom" in phase_of(dt.date(2026, 3, 28))
    assert "eom" not in phase_of(dt.date(2026, 3, 15))
    assert "som" in phase_of(dt.date(2026, 3, 2))
    assert "annual" in phase_of(dt.date(2026, 3, 15))       # tax season window
    # Quarter end is also a month end.
    q = phase_of(dt.date(2026, 3, 31))
    assert "quarter_end" in q and "eom" in q
    assert "quarter_end" not in phase_of(dt.date(2026, 5, 31))


def test_previous_window_uses_month_arithmetic_not_30_days():
    """February and March end on different day numbers. Subtracting 30 days
    silently compares the wrong weeks."""
    prev = previous_window("eom", dt.date(2026, 3, 30))
    assert prev == (dt.date(2026, 2, 22), dt.date(2026, 2, 28)), prev

    cur = current_window("eom", dt.date(2026, 3, 30))
    assert cur == (dt.date(2026, 3, 25), dt.date(2026, 3, 31))

    # Leap year: 2028 February has 29 days.
    assert previous_window("eom", dt.date(2028, 3, 30))[1] == dt.date(2028, 2, 29)

    # Year boundary.
    assert previous_window("eom", dt.date(2026, 1, 30))[1] == dt.date(2025, 12, 31)

    # Quarters step by three months, annual by a year.
    assert previous_window("quarter_end", dt.date(2026, 6, 30))[1] == dt.date(2026, 3, 31)
    assert previous_window("annual", dt.date(2026, 3, 1))[0].year == 2025


def test_the_mount_incident():
    """The whole reason D2 ships before the graph.

    A mount present during last month's EOM is gone during this one. No
    dependency graph involved -- two snapshots and a calendar.
    """
    last_eom = [{"hostname": "srv-04", "sent_at": 100, "results": [
        {"id": "mount-data", "value": "/mnt/fs01/data"},
        {"id": "os-build", "value": "10.0.26100"},
    ]}]
    this_eom = [{"hostname": "srv-04", "sent_at": 200, "results": [
        {"id": "os-build", "value": "10.0.26100"},
    ]}]

    jobs = [{"host": "srv-04", "id": "month-end-close", "phases": ["eom"]}]
    report = seasonal_report("eom", dt.date(2026, 3, 30), last_eom, this_eom, jobs)

    gone = [c for c in report["changes"] if c["change"] == "removed"]
    assert len(gone) == 1 and gone[0]["item"] == "mount-data"
    assert gone[0]["phase_exposed"] is True, "must flag that an EOM job runs on this host"
    assert report["previous_window"][0] == "2026-02-22"
    assert "1 touch hosts" in report["summary"]


def test_exposed_changes_rank_above_noise():
    before = {("srv-04", "mount-data"): "/mnt/x", ("ws-91", "chrome"): "150.0"}
    after = {("ws-91", "chrome"): "151.0"}
    jobs = [{"host": "srv-04", "phases": ["eom"]}]
    ranked = rank_by_job_exposure(diff_snapshots(before, after), jobs, "eom")
    assert ranked[0]["item"] == "mount-data" and ranked[0]["phase_exposed"]
    assert ranked[-1]["item"] == "chrome" and not ranked[-1]["phase_exposed"]


def test_errored_facts_are_not_changes():
    """A check that could not run must not read as "the fact disappeared" --
    the same rule as the report protocol, applied to diffing."""
    before = [{"hostname": "a", "sent_at": 1, "results": [{"id": "bitlocker", "value": True}]}]
    after = [{"hostname": "a", "sent_at": 2, "results": [
        {"id": "bitlocker", "value": None, "error": "Access denied"}]}]
    diff = diff_snapshots(snapshot_from_reports(before), snapshot_from_reports(after))
    assert diff["changed"] == []
    assert len(diff["removed"]) == 1        # absent, and reported as absent -- not as changed
    assert diff["removed"][0]["item"] == "bitlocker"


def test_snapshot_last_write_wins_within_a_window():
    reports = [
        {"hostname": "a", "sent_at": 1, "results": [{"id": "x", "value": "old"}]},
        {"hostname": "a", "sent_at": 9, "results": [{"id": "x", "value": "new"}]},
    ]
    assert snapshot_from_reports(reports)[("a", "x")] == "new"


# --------------------------------------------------------------------------
def _main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    _main()
