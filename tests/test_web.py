"""The console reads what a pull leaves behind. These check the numbers it
shows, not the HTML.

Runs under pytest, or standalone: `python tests/test_web.py`
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lineage import web  # noqa: E402
from lineage.graph import merge, open_store, record_window  # noqa: E402

T0 = 1_800_000_000.0


def _fleet(d):
    conn = open_store(str(Path(d) / "central.db"))
    merge(conn, [{"host": "win-a", "container": "", "actor": r"C:\jobs\eom.exe", "volume": "smb://fs01/data",
                  "rel": "/eom.csv", "direction": "write", "ts": T0, "example": "//fs01/data/eom.csv"},
                 {"host": "win-a", "container": "", "actor": "pid:42", "volume": "smb://fs01/data",
                  "rel": "/eom.csv", "direction": "read", "ts": T0 + 5, "example": "//fs01/data/eom.csv"}])
    record_window(conn, "win-a", "etw", T0, T0 + 3600)
    record_window(conn, "win-a", "etw", T0 + 3601, T0 + 7200)        # cycle hand-off: not a gap
    record_window(conn, "win-a", "etw", T0 + 9000, T0 + 10800)       # VM was off: a gap
    snaps = Path(d) / "win-a" / "snapshots"
    snaps.mkdir(parents=True)
    (snaps / "2027-01-15.json").write_text(json.dumps({"jobs": [{"id": "old", "host": "win-a"}]}))
    (snaps / "2027-01-16.json").write_text(json.dumps({"jobs": [{"id": "new", "host": "win-a"}]}))
    return conn


def test_console_numbers():
    with tempfile.TemporaryDirectory() as d:
        conn = _fleet(d)
        ov = web.overview(conn)
        h = ov["hosts"][0]
        assert (h["windows"], h["actors"], h["artifacts"], h["observations"]) == (3, 2, 1, 2)
        assert h["watched_s"] < h["span_s"]          # the gap is not counted as watched
        assert ov["links"] == 1 and ov["cross_host_links"] == 0

        det = web.host_detail(conn, d, "win-a")
        assert [g["seconds"] for g in det["gaps"]] == [1800]
        assert [j["id"] for j in det["jobs"]] == ["new"]          # latest snapshot only
        assert det["actors"][0]["name"] in ("eom.exe", "pid:42")
        assert {a["name"] for a in det["actors"]} == {"eom.exe", "pid:42"}

        assert len(web.edges(conn, "win-a", "eom.exe")) == 1
        assert web.edges(conn, "win-b") == []
        conn.close()


def test_demo_fleet():
    """tools/demo.py feeds the README screenshots; it must show what they claim."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
    import demo
    with tempfile.TemporaryDirectory() as d:
        demo.build(d)
        conn = open_store(str(Path(d) / "central.db"))
        ov = web.overview(conn)
        assert [h["host"] for h in ov["hosts"]] == ["app-01", "rpt-01"]
        assert ov["cross_host_links"] == 2
        det = web.host_detail(conn, d, "app-01")
        assert [g["seconds"] for g in det["gaps"]] == [4 * 3600 + 2]
        assert [j["id"] for j in det["scream"]["declared_not_observed"]] == [
            r"\Billing\Month-end export", r"\Billing\Year-end archive"]
        conn.close()


def _main():
    for name, t in sorted((k, v) for k, v in globals().items() if k.startswith("test_")):
        t()
        print(f"  ok  {name}")


if __name__ == "__main__":
    _main()
