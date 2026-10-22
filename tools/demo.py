"""Build a made-up fleet for the console: screenshots, demos, a first look.

    python tools/demo.py                     # writes var/demo (gitignored)
    python -m lineage.web --fleet var/demo   # http://127.0.0.1:8771

Every host, path and job here is invented. Real pulled data names every program
and file on a watched machine, so it never goes in a screenshot. The rows go
through the same store calls collection uses (merge, record_window), so the
console renders them exactly as it would a real pull. Seeded and on fixed
dates: rerunning it gives the same screenshots.
"""
from __future__ import annotations

import argparse
import json
import random
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lineage.graph import merge, open_store, record_window  # noqa: E402

T0 = 1_790_812_800.0                 # 2026-10-01 00:00Z
HOURS = 7 * 24
OFF = range(80, 84)                  # app-01 was off for four hours: shows as a gap
SHARE = "smb://fs01/finance"

# host, actor, volume, rel, direction, events per active hour (probability)
ACTIVITY = [
    ("app-01", r"C:\Apps\Billing\billing-svc.exe", SHARE, "/ledger/ledger.db", "write", 0.9),
    ("app-01", r"C:\Apps\Billing\billing-svc.exe", "dev://app-01/c:", "/Apps/Billing/billing.ini", "read", 0.3),
    ("app-01", r"C:\Apps\Billing\daily-export.exe", SHARE, "/exports/daily.csv", "write", 0.04),
    ("app-01", r"C:\Apps\Billing\daily-export.exe", SHARE, "/ledger/ledger.db", "read", 0.04),
    ("app-01", r"C:\Program Files\Backup\backup-agent.exe", "dev://app-01/c:", "/Apps/Billing/billing.ini", "read", 0.05),
    ("app-01", r"C:\Program Files\Backup\backup-agent.exe", "dev://app-01/c:", "/Apps/Billing/logs/billing.log", "read", 0.05),
    ("app-01", r"C:\Apps\Billing\billing-svc.exe", "dev://app-01/c:", "/Apps/Billing/logs/billing.log", "write", 1.0),
    ("app-01", r"C:\Windows\System32\svchost.exe", "dev://app-01/c:", "/Windows/Logs/CBS/CBS.log", "write", 0.5),
    ("app-01", r"C:\Windows\System32\taskhostw.exe", "dev://app-01/c:", "/Windows/Tasks/SA.DAT", "write", 0.2),
    ("rpt-01", "/usr/local/bin/report-build", SHARE, "/exports/daily.csv", "read", 0.05),
    ("rpt-01", "/usr/local/bin/report-build", "dev://rpt-01/259:1", "/srv/reports/daily.pdf", "write", 0.05),
    ("rpt-01", "/usr/bin/python3", SHARE, "/ledger/ledger.db", "read", 0.25),
    ("rpt-01", "/usr/sbin/nginx", "dev://rpt-01/259:1", "/srv/reports/daily.pdf", "read", 0.3),
    ("rpt-01", "/usr/sbin/rsyslogd", "dev://rpt-01/259:1", "/var/log/syslog", "write", 1.0),
]

JOBS = {
    "app-01": [
        {"id": r"\Billing\Nightly ledger close", "kind": "scheduled-task", "schedule": "CalendarTrigger",
         "command": r"C:\Apps\Billing\billing-svc.exe --close", "max_gap_days": 1.0, "frequency": "daily",
         "enabled": True, "observations": 7},
        {"id": r"\Billing\Month-end export", "kind": "scheduled-task", "schedule": "CalendarTrigger",
         "command": r"C:\Apps\Billing\eom-export.exe --month", "max_gap_days": 31.0, "frequency": "monthly",
         "enabled": True, "observations": 0},
        {"id": r"\Billing\Daily export", "kind": "scheduled-task", "schedule": "CalendarTrigger",
         "command": r"C:\Apps\Billing\daily-export.exe", "max_gap_days": 1.0, "frequency": "daily",
         "enabled": True, "observations": 7},
        {"id": r"\Backup\Weekly full", "kind": "scheduled-task", "schedule": "CalendarTrigger",
         "command": r"C:\Program Files\Backup\backup-agent.exe full", "max_gap_days": 7.0,
         "frequency": "weekly", "enabled": True, "observations": 1},
        {"id": r"\Billing\Year-end archive", "kind": "scheduled-task", "schedule": "CalendarTrigger",
         "command": r"C:\Apps\Billing\eom-export.exe --year", "max_gap_days": 366.0,
         "frequency": "yearly", "enabled": True, "observations": 0},
    ],
    "rpt-01": [
        {"id": "/etc/cron.d/reports:0", "kind": "cron", "schedule": "30 6 2 * *",
         "command": "/usr/local/bin/report-build eom", "max_gap_days": 31.0, "frequency": "monthly",
         "enabled": True, "observations": 0},
        {"id": "/etc/cron.d/reports:1", "kind": "cron", "schedule": "15 2 * * *",
         "command": "/usr/local/bin/report-build daily", "max_gap_days": 1.0, "frequency": "daily",
         "enabled": True, "observations": 7},
        {"id": "/etc/cron.d/logrotate:0", "kind": "cron", "schedule": "25 6 * * *",
         "command": "/usr/sbin/logrotate /etc/logrotate.conf", "max_gap_days": 1.0, "frequency": "daily",
         "enabled": True, "observations": 7},
    ],
}


def build(out):
    out = Path(out)
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    rng = random.Random(7)
    conn = open_store(str(out / "central.db"))
    rows = []
    for h in range(HOURS):
        for host, actor, vol, rel, direction, p in ACTIVITY:
            if host == "app-01" and h in OFF:
                continue
            for _ in range(rng.choices([0, 1, 2, 3], [1 - p, p * .5, p * .3, p * .2])[0]):
                ts = T0 + h * 3600 + rng.uniform(0, 3599)
                rows.append({"host": host, "container": "", "actor": actor, "volume": vol, "rel": rel,
                             "direction": direction, "ts": ts, "example": vol.split("://", 1)[1] + rel})
    merge(conn, rows)
    for host, tap in (("app-01", "etw"), ("rpt-01", "proc")):
        for h in range(HOURS):
            if host == "app-01" and h in OFF:
                continue
            record_window(conn, host, tap, T0 + h * 3600 + 2, T0 + (h + 1) * 3600)
    conn.close()
    for host, jobs in JOBS.items():
        snaps = out / host / "snapshots"
        snaps.mkdir(parents=True)
        (snaps / "2026-10-07.json").write_text(json.dumps(
            {"hostname": host, "jobs": [{**j, "host": host} for j in jobs]}, indent=1))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default=str(ROOT / "var" / "demo"))
    print(f"demo fleet written to {build(ap.parse_args(argv).out)}")


if __name__ == "__main__":
    main()
