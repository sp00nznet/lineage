"""D3. Pattern collapsing and rollup are the parts that decide whether the
output is a pipeline or a thousand orphans, so they are the parts with tests.

Runs under pytest, or standalone: `python tests/test_observe.py`
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lineage.observe import (  # noqa: E402
    BOTH, READ, WRITE, collapse_path, container_of, is_noise, link_pipelines,
    normalize_path, observation, orphan_artifacts, rollup,
)


def obs(actor, path, direction, ts=0, host="h1", container=None):
    return observation(actor, path, direction, ts=ts, host=host, container=container)


# --------------------------------------------------------------------------
# Pattern collapsing
# --------------------------------------------------------------------------
def test_dated_exports_collapse_to_one_node():
    """Without this the graph mints new nodes nightly and never converges."""
    days = [f"/data/out/export_2026-08-{d:02d}.csv" for d in range(1, 15)]
    assert len({collapse_path(p) for p in days}) == 1
    assert collapse_path(days[0]) == "/data/out/export_<date>.csv"
    # Compact and underscored date forms too.
    assert collapse_path("/d/rpt20260809.dat") == "/d/rpt<date>.dat"
    assert collapse_path("/d/rpt_2026_08_09.dat") == "/d/rpt_<date>.dat"


def test_serial_and_uuid_and_hex():
    assert collapse_path("/spool/part00123.dat") == "/spool/part<n>.dat"
    assert collapse_path("/var/log/syslog.1") == "/var/log/syslog.<n>"
    assert collapse_path("/var/log/syslog.12") == "/var/log/syslog.<n>"
    u = "/cache/3f2c9a1b-1111-2222-3333-444455556666.bin"
    assert collapse_path(u) == "/cache/<uuid>.bin"
    assert collapse_path("/objects/a1b2c3d4e5f6a7b8.pack") == "/objects/<hex>.pack"


def test_collapsing_is_conservative():
    """Over-collapsing merges unrelated files, which is worse than not
    collapsing at all -- it invents dependencies that do not exist."""
    for keep in ("/opt/legacy/load.pl", "/etc/fstab", "/data/report.csv",
                 "/usr/bin/python3", "/srv/app/v2.conf", "/media/mp3/track.mp3"):
        assert collapse_path(keep) == keep, keep
    # A versioned directory is part of the path's identity, not serial noise.
    assert collapse_path("/opt/proton/GE-Proton8-25/run.sh") == "/opt/proton/GE-Proton8-25/run.sh"


def test_normalize_separators_and_drive_case():
    assert normalize_path(r"C:\data\out\x.csv") == "c:/data/out/x.csv"
    assert normalize_path("/data//out///x.csv") == "/data/out/x.csv"
    assert normalize_path(r"\\fs01\data\x.csv").startswith("//fs01/")


def test_noise_filter():
    assert is_noise("/proc/self/status") and is_noise("/sys/fs/cgroup/x")
    assert is_noise("/srv/app/thing.pyc") and is_noise("/run/lock/a.lock")
    assert not is_noise("/data/out/export.csv")
    assert not is_noise("/opt/legacy/load.pl")
    # Directory opens of the noise roots themselves, from the first live report.
    assert is_noise("c:/Windows") and is_noise("c:/") and is_noise("C:")
    assert not is_noise("d:/data") and not is_noise("c:/windowsapps-not-really/x")


# --------------------------------------------------------------------------
# Rollup
# --------------------------------------------------------------------------
def test_rollup_counts_and_keeps_time_span():
    """"Read once three months ago" and "read every night" are different facts;
    a boolean edge destroys the difference."""
    observations = [obs("/opt/legacy/load.pl", f"/data/out/export_2026-08-{d:02d}.csv", READ, ts=d)
                    for d in range(1, 15)]
    edges = rollup(observations)
    assert len(edges) == 1
    e = edges[0]
    assert e["count"] == 14 and e["first_seen"] == 1 and e["last_seen"] == 14
    assert e["pattern"] == "/data/out/export_<date>.csv"
    assert len(e["examples"]) == 3          # a few real paths, not all 14


def test_rollup_drops_noise_by_default():
    edges = rollup([obs("p", "/proc/1/status", READ), obs("p", "/data/x.csv", READ)])
    assert [e["pattern"] for e in edges] == ["/data/x.csv"]
    assert len(rollup([obs("p", "/proc/1/status", READ)], drop_noise=False)) == 1


def test_rollup_separates_actors_and_directions():
    edges = rollup([
        obs("writer", "/data/x.csv", WRITE),
        obs("reader", "/data/x.csv", READ),
        obs("reader", "/data/x.csv", READ),
    ])
    assert len(edges) == 2
    assert edges[0]["actor"] == "reader" and edges[0]["count"] == 2


# --------------------------------------------------------------------------
# Linking -- the pipeline
# --------------------------------------------------------------------------
def test_the_cross_host_pipeline():
    """job A writes on one box, job B reads on another. The finding no single
    machine could have produced."""
    edges = rollup([
        obs("/usr/bin/nightly.sh", "/data/out/export_2026-08-01.csv", WRITE, ts=1, host="srv-01"),
        obs("/opt/legacy/load.pl", "/mnt/fs01/out/export_2026-08-01.csv", READ, ts=2, host="srv-04"),
        obs("/opt/legacy/load.pl", "/data/out/export_2026-08-01.csv", READ, ts=2, host="srv-04"),
    ])
    links = link_pipelines(edges)
    cross = [l for l in links if l["cross_host"]]
    assert len(cross) == 1
    link = cross[0]
    assert link["producer"]["host"] == "srv-01"
    assert link["consumer"]["host"] == "srv-04"
    # A cross-host link is a hypothesis until D4 proves both sides are the same
    # bytes; it must say so rather than presenting as settled.
    assert link["needs_path_join"] is True


def test_a_process_reading_its_own_output_is_not_a_pipeline():
    edges = rollup([
        obs("app", "/data/state.db", WRITE, ts=1),
        obs("app", "/data/state.db", READ, ts=2),
    ])
    assert link_pipelines(edges) == []


def test_readwrite_descriptors_count_as_both_ends():
    edges = rollup([
        obs("producer", "/data/q.dat", BOTH, ts=1),
        obs("consumer", "/data/q.dat", READ, ts=2),
    ])
    links = link_pipelines(edges)
    assert len(links) == 1 and links[0]["producer"]["actor"] == "producer"


def test_orphans_point_in_two_useful_directions():
    edges = rollup([
        obs("a", "/data/dead.csv", WRITE),
        obs("b", "/data/mystery.csv", READ),
        obs("c", "/data/live.csv", WRITE),
        obs("d", "/data/live.csv", READ),
    ])
    o = orphan_artifacts(edges)
    assert o["written_never_read"] == ["/data/dead.csv"]
    # The more interesting one: something outside our observation is producing it.
    assert o["read_never_written"] == ["/data/mystery.csv"]


# --------------------------------------------------------------------------
# Container attribution -- what makes a hypervisor-host tap usable
# --------------------------------------------------------------------------
def test_container_attribution_from_cgroup(tmp_path=None):
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        cases = {
            "101": ("0::/lxc/112/ns/system.slice/pzserver.service\n", "lxc:112"),
            "102": ("0::/lxc.payload.114/system.slice/plexmediaserver.service\n", "lxc:114"),
            "103": ("0::/qemu.slice/104.scope\n", "qemu:104"),
            "104": ("0::/system.slice/docker-a1b2c3d4e5f6a7b8c9d0.scope\n", "docker:a1b2c3d4e5f6"),
            "105": ("0::/system.slice/sshd.service\n", None),
            # Real cgroup lines from a live Proxmox node. The host's own
            # lxc-monitord unit must not be mistaken for a container called
            # "monitord" -- a looser pattern invented one here.
            "106": ("0::/system.slice/lxc-monitord.service\n", None),
            "107": ("0::/lxc.monitor.111/system.slice/init.scope\n", "lxc:111"),
            "108": ("0::/system.slice/pvestatd.service\n", None),
        }
        for pid, (text, _) in cases.items():
            (root / pid).mkdir()
            (root / pid / "cgroup").write_text(text)
        for pid, (_, expected) in cases.items():
            assert container_of(pid, root=str(root)) == expected, pid
        assert container_of("999", root=str(root)) is None


# --------------------------------------------------------------------------
def _main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    _main()
