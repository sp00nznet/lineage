"""The unattended cycle's two promises: pulling a host's store any number of
times gives the same counts, and a day's snapshot feeds D2 unchanged.

Runs under pytest, or standalone: `python tests/test_collect.py`
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lineage import collect  # noqa: E402
from lineage.graph import merge, merge_store, open_store, record_window, register_host  # noqa: E402
from lineage.seasons import diff_snapshots, snapshot_from_reports  # noqa: E402

T0 = 1_800_000_000.0


def _host_store(path, host, n):
    conn = open_store(str(path))
    register_host(conn, host, ["192.0.2.10"])
    merge(conn, [{"host": host, "container": "", "actor": "c:/jobs/eom.exe",
                  "volume": "smb://fs01/data", "rel": "/eom.csv", "direction": "write",
                  "ts": T0 + i, "example": "//fs01/data/eom.csv"} for i in range(n)])
    record_window(conn, host, "etw", T0, T0 + 3600)
    conn.close()


def test_pulling_twice_does_not_double_count():
    """Each agent store is cumulative, so a pull replaces that host's rows.
    Adding instead would double every count on every hourly pull."""
    with tempfile.TemporaryDirectory() as d:
        src = Path(d) / "a.db"
        _host_store(src, "win-a", 5)
        central = open_store(str(Path(d) / "central.db"))
        assert merge_store(central, src) == ["win-a"]
        assert merge_store(central, src) == ["win-a"]
        assert central.execute("SELECT SUM(count) FROM edges").fetchone()[0] == 5
        assert central.execute("SELECT COUNT(*) FROM windows").fetchone()[0] == 1
        assert central.execute("SELECT canonical FROM host_aliases WHERE alias='192.0.2.10'"
                               ).fetchone()[0] == "win-a"
        central.close()


def test_snapshot_feeds_the_seasonal_diff():
    """The D2 incident: a share unmapped in a quiet month. Two daily snapshots
    must diff to exactly that removal, with the job list carried alongside."""
    jobs = [{"id": "\\EOM close", "schedule": "CalendarTrigger", "enabled": True}]
    real_jobs, real_mounts = collect.collect_jobs, collect._mounts
    try:
        collect.collect_jobs = lambda: [dict(j) for j in jobs]
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            collect._mounts = lambda: {"c:": r"\device\harddiskvolume3", "//fs01/data": r"\device\mup\fs01\data"}
            before, _, _ = collect.write_snapshot(home, "win-a", now=T0)
            collect._mounts = lambda: {"c:": r"\device\harddiskvolume3"}
            after, njobs, nmounts = collect.write_snapshot(home, "win-a", now=T0 + 31 * 86400)
            import json
            a, b = (json.loads(p.read_text()) for p in (before, after))
    finally:
        collect.collect_jobs, collect._mounts = real_jobs, real_mounts
    assert (njobs, nmounts) == (1, 1)
    assert b["jobs"][0]["host"] == "win-a"
    diff = diff_snapshots(snapshot_from_reports([a]), snapshot_from_reports([b]))
    assert [r["item"] for r in diff["removed"]] == ["mount://fs01/data"]
    assert not diff["added"] and not diff["changed"]


def _main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    _main()
