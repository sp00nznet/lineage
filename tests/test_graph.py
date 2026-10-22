"""D5. The time-separated link is the reason this module exists, so it is the
first test. Real volume identities from the live node are used throughout.

Runs under pytest, or standalone: `python tests/test_graph.py`
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lineage.graph import (  # noqa: E402
    canonical_volume, dependencies, dependents, host_map, links, merge,
    observed_days, open_store, record_window, register_host,
    render_scream_test, resolve_observations, scream_test,
)
from lineage.observe import BOTH, READ, WRITE, observation  # noqa: E402
from lineage.paths import parse_exports, parse_mountinfo, resolve  # noqa: E402

DAY = 86400.0
T0 = 1_800_000_000.0


def row(host, actor, volume, rel, direction, ts, container=""):
    return {"host": host, "container": container, "actor": actor, "volume": volume,
            "rel": rel, "direction": direction, "ts": ts, "example": rel}


# ==========================================================================
def test_the_time_separated_link():
    """The whole reason D5 exists.

    A nightly job writes at 02:15 on day 1. Something reads at 03:00 on day 30.
    They are never open at once, so no snapshot could ever see this -- and a
    live sweep of four containers correctly found zero shared artifacts. The
    link exists only because observations accumulate.
    """
    conn = open_store()
    merge(conn, [row("srv-01", "/usr/bin/nightly.sh", "nfs://pve-a",
                     "/beta/out/export_<date>.csv", WRITE, T0 + 2.25 * 3600)])
    merge(conn, [row("srv-04", "/opt/legacy/load.pl", "nfs://pve-a",
                     "/beta/out/export_<date>.csv", READ, T0 + 29 * DAY + 3 * 3600)])

    found = links(conn)
    assert len(found) == 1, found
    l = found[0]
    assert l["producer"]["host"] == "srv-01" and l["consumer"]["host"] == "srv-04"
    assert l["cross_host"] is True
    assert l["consumer"]["last_seen"] - l["producer"]["last_seen"] > 28 * DAY


def test_aggregation_not_raw_storage():
    """48k observations in 30s on one node -- raw storage is not an option.
    Repeated observations of one artifact collapse to a single counted row."""
    conn = open_store()
    for day in range(60):
        merge(conn, [row("srv-01", "job.sh", "nfs://h", "/data/x_<date>.csv", WRITE,
                         T0 + day * DAY)])
    r = conn.execute("SELECT COUNT(*) n, SUM(count) c, first_seen, last_seen FROM edges").fetchone()
    assert r["n"] == 1 and r["c"] == 60
    assert round((r["last_seen"] - r["first_seen"]) / DAY) == 59


def test_merge_is_additive_across_runs_and_keeps_examples():
    conn = open_store()
    merge(conn, [row("h", "a", "dev://h/0:40", "/x", READ, T0)])
    merge(conn, [row("h", "a", "dev://h/0:40", "/x", READ, T0 + DAY)])
    r = conn.execute("SELECT count, examples FROM edges").fetchone()
    assert r["count"] == 2 and r["examples"] != "[]"


def test_a_process_reading_its_own_output_is_not_a_link():
    conn = open_store()
    merge(conn, [row("h", "app", "dev://h/0:40", "/state.db", WRITE, T0),
                 row("h", "app", "dev://h/0:40", "/state.db", READ, T0 + 60)])
    assert links(conn) == []


def test_readwrite_counts_as_both_ends():
    conn = open_store()
    merge(conn, [row("h", "producer", "dev://h/0:40", "/q.dat", BOTH, T0),
                 row("h", "consumer", "dev://h/0:40", "/q.dat", READ, T0 + DAY)])
    assert len(links(conn)) == 1


def test_links_never_form_on_path_string_alone():
    """Two containers binding different sources at the same mountpoint resolve
    to different volumes, so no link is fabricated."""
    conn = open_store()
    merge(conn, [row("pve-a", "a", "dev://pve-a/0:41", "/data.csv", WRITE, T0, container="lxc:200"),
                 row("pve-a", "b", "dev://pve-a/0:109", "/data.csv", READ, T0 + DAY, container="lxc:201")])
    assert links(conn) == []


def test_cross_container_link_on_one_node():
    """plex and rutorrent both bind the same dataset: same volume, so a write
    by one and a later read by the other genuinely links."""
    conn = open_store()
    merge(conn, [row("pve-a", "rtorrent", "dev://pve-a/0:40", "/incoming/f.mkv", WRITE,
                     T0, container="lxc:116"),
                 row("pve-a", "Plex Media Server", "dev://pve-a/0:40", "/incoming/f.mkv", READ,
                     T0 + 2 * DAY, container="lxc:114")])
    found = links(conn)
    assert len(found) == 1
    assert found[0]["cross_host"] is False and found[0]["cross_namespace"] is True


# --------------------------------------------------------------------------
# Host identity -- what actually unblocked cross-host links
# --------------------------------------------------------------------------
def test_host_aliases_unify_ip_and_hostname():
    """The limitation asserted in D4's tests, now fixed. The client mounts
    192.0.2.23:/beta; the server calls itself pve-a. Same machine."""
    conn = open_store()
    register_host(conn, "pve-a", ["192.0.2.23", "pve-a.lan"])
    aliases = host_map(conn)
    assert canonical_volume("nfs://192.0.2.23", aliases) == "nfs://pve-a"
    assert canonical_volume("nfs://pve-a.lan", aliases) == "nfs://pve-a"
    assert canonical_volume("nfs://unknown-box", aliases) == "nfs://unknown-box"
    assert canonical_volume("smb://192.0.2.23/data", aliases) == "smb://pve-a/data"


def test_the_cross_host_link_that_d4_could_not_make():
    """End to end: real mount tables, real exports, IP-vs-hostname reconciled,
    and the two sides observed a month apart."""
    conn = open_store()
    register_host(conn, "pve-a", ["192.0.2.23"])
    aliases = host_map(conn)

    host_mounts = parse_mountinfo(
        "55 32 0:40 / /beta rw shared:53 - zfs beta rw,xattr")
    client_mounts = parse_mountinfo(
        "310 25 0:57 / /mnt/store rw shared:170 - nfs4 192.0.2.23:/beta rw,vers=4.2")
    exports = parse_exports("/beta 192.0.2.0/24(rw)\n")

    server_obs = [observation("rtorrent", "/beta/incoming/f.mkv", WRITE,
                              ts=T0, host="pve-a")]
    client_obs = [observation("jellyfin", "/mnt/store/incoming/f.mkv", READ,
                              ts=T0 + 30 * DAY, host="pve-b")]

    merge(conn, resolve_observations(
        server_obs, lambda o: resolve(o["path"], host_mounts, "pve-a"),
        {"pve-a": exports}, aliases))
    merge(conn, resolve_observations(
        client_obs, lambda o: resolve(o["path"], client_mounts, "pve-b"),
        {}, aliases))

    found = links(conn)
    assert len(found) == 1, [dict(r) for r in conn.execute("SELECT * FROM edges")]
    l = found[0]
    assert l["artifact"] == "nfs://pve-a/beta/incoming/f.mkv"
    assert l["producer"]["host"] == "pve-a" and l["consumer"]["host"] == "pve-b"
    assert l["cross_host"] is True


def test_noise_is_dropped_before_it_reaches_the_store():
    conn = open_store()
    mounts = parse_mountinfo("32 2 252:1 / / rw - ext4 /dev/root rw")
    obs = [observation("p", "/proc/1/status", READ, ts=T0, host="h"),
           observation("p", "/data/real.csv", READ, ts=T0, host="h")]
    merge(conn, resolve_observations(obs, lambda o: resolve(o["path"], mounts, "h")))
    rels = [r["rel"] for r in conn.execute("SELECT rel FROM edges")]
    assert rels == ["/data/real.csv"]


# --------------------------------------------------------------------------
# Coverage and the scream test
# --------------------------------------------------------------------------
def test_observed_days_is_the_denominator():
    conn = open_store()
    assert observed_days(conn) == 0.0
    record_window(conn, "h", "proc-sample", T0, T0 + 30)
    record_window(conn, "h", "proc-sample", T0 + 9 * DAY, T0 + 9 * DAY + 30)
    assert 9.0 <= observed_days(conn) <= 9.1


def test_scream_test_keeps_its_tiers_apart():
    conn = open_store()
    record_window(conn, "srv-04", "proc-sample", T0, T0 + 9 * DAY)
    merge(conn, [row("srv-04", "/usr/bin/export.sh", "nfs://pve-a", "/beta/out/x_<date>.csv",
                     WRITE, T0),
                 row("srv-07", "/opt/bill/load.pl", "nfs://pve-a", "/beta/out/x_<date>.csv",
                     READ, T0 + 5 * DAY)])
    jobs = [
        {"id": "month-end-close", "host": "srv-04", "schedule": "0 2 28-31 * *",
         "frequency": "monthly", "max_gap_days": 28, "observations": 0},
        {"id": "annual-extract", "host": "srv-04", "schedule": "0 3 1 4 *",
         "frequency": "annual", "max_gap_days": 365, "observations": 0},
        {"id": "unattributed", "host": "", "container": "", "max_gap_days": 1,
         "observations": 0},
    ]
    r = scream_test(conn, "srv-04", jobs=jobs,
                    seasonal={"warning": "340x traffic in the last week of each of 3 months"})

    assert len(r["confirmed"]) == 1
    assert r["confirmed"][0]["consumer"]["host"] == "srv-07"
    assert {d["id"] for d in r["declared_not_observed"]} == {"month-end-close", "annual-extract"}
    assert r["unknown_jobs"] == 1
    assert 8.9 <= r["observed_window_days"] <= 9.1
    # The tiers must stay separate structures, never a merged list.
    assert "verdict" not in r and "safe" not in r
    assert "not evidence" in r["coverage_note"]


def test_declared_jobs_explain_themselves_against_the_window():
    conn = open_store()
    record_window(conn, "srv-04", "proc-sample", T0, T0 + 9 * DAY)
    jobs = [{"id": "monthly", "host": "srv-04", "frequency": "monthly",
             "max_gap_days": 28, "observations": 0},
            {"id": "hourly-but-silent", "host": "srv-04", "frequency": "subdaily",
             "max_gap_days": 0.04, "observations": 0}]
    r = scream_test(conn, "srv-04", jobs=jobs)
    by_id = {d["id"]: d for d in r["declared_not_observed"]}
    # Slower than the window: not running yet is expected, not suspicious.
    assert "may simply not have run yet" in by_id["monthly"]["why"]
    # Faster than the window and still never seen: that is worth a look.
    assert "in-window" in by_id["hourly-but-silent"]["why"]


def test_render_puts_the_seasonal_warning_first():
    conn = open_store()
    record_window(conn, "srv-04", "proc-sample", T0, T0 + 9 * DAY)
    merge(conn, [row("srv-04", "w", "nfs://h", "/x", WRITE, T0),
                 row("srv-09", "r", "nfs://h", "/x", READ, T0 + DAY)])
    text = render_scream_test(scream_test(
        conn, "srv-04",
        jobs=[{"id": "eom", "host": "srv-04", "frequency": "monthly",
               "max_gap_days": 28, "observations": 0}],
        seasonal={"warning": "quiet phase; 340x during EOM"}))
    assert text.index("SEASONAL") < text.index("CONFIRMED") < text.index("DECLARED")
    assert "UNKNOWN" in text and "not evidence" in text
    assert "safe to" not in text.lower()


def test_window_is_readable_at_every_scale():
    """"0.0 days" is what two 45-second sweeps produce, and it reads as a bug
    rather than as the warning it is. This line is the honesty line."""
    from lineage.jobs import format_window
    assert format_window(0) == "no observation"
    assert format_window(0.0008) == "1.2 minutes"
    assert format_window(0.2) == "4.8 hours"
    assert format_window(9.37) == "9.4 days"


def test_consumers_group_by_who_breaks_not_which_file():
    """A file touched by six daemons yields six join rows. Rendering those as
    six identical `host -> host` lines answers the wrong question and hides
    the actor, which is the only field that distinguishes them."""
    conn = open_store()
    merge(conn, [row("pve-a", "samba-writer", "dev://pve-a/252:1",
                     "/var/lib/samba/secrets.tdb", WRITE, T0),
                 row("pve-a", "samba-writer", "dev://pve-a/252:1",
                     "/var/lib/samba/group_mapping.tdb", WRITE, T0)])
    for actor in ("smbd", "winbindd", "nmbd"):
        merge(conn, [row("pve-a", actor, "dev://pve-a/252:1",
                         "/var/lib/samba/secrets.tdb", READ, T0 + 60),
                     row("pve-a", actor, "dev://pve-a/252:1",
                         "/var/lib/samba/group_mapping.tdb", READ, T0 + 60)])
    r = scream_test(conn, "pve-a")
    assert len(r["confirmed"]) > len(r["consumers"]), "raw links should exceed grouped consumers"
    assert {c["actor"] for c in r["consumers"]} == {"smbd", "winbindd", "nmbd"}
    assert all(c["artifact_count"] == 2 for c in r["consumers"])

    text = render_scream_test(r)
    assert "smbd" in text and "winbindd" in text
    assert text.count("secrets.tdb") <= 3, "one line per consumer, not per join row"


def test_dependents_and_dependencies_point_opposite_ways():
    conn = open_store()
    merge(conn, [row("A", "w", "nfs://h", "/x", WRITE, T0),
                 row("B", "r", "nfs://h", "/x", READ, T0 + DAY),
                 row("C", "w2", "nfs://h", "/y", WRITE, T0),
                 row("A", "r2", "nfs://h", "/y", READ, T0 + DAY)])
    assert [l["consumer"]["host"] for l in dependents(conn, "A")] == ["B"]
    assert [l["producer"]["host"] for l in dependencies(conn, "A")] == ["C"]


# --------------------------------------------------------------------------
def _main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    _main()
