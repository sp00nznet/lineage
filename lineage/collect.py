"""lineage.collect — the unattended cycle. D1–D5, read-only, on a schedule.

    python -m lineage.collect run    --home C:\\ProgramData\\lineage    # one cycle; schedule it hourly
    python -m lineage.collect merge  central.db host-a.db host-b.db
    python -m lineage.collect report --store central.db [--host NAME]

D2 and D5 are worth exactly as much as the history behind them, and history
that was not collected cannot be recovered. So this module's whole job is to
keep collecting without anyone watching, and to record honestly when it was
and was not watching. See docs/architecture.md for how the pieces meet.

**Windows keeps one ETW session running between cycles.** A cycle stops it,
starts the next one straight away, and only then decodes the finished file, so
the unwatched gap is the stop->start, not the decode. A per-cycle capture of a
few minutes would watch a twelfth of the hour and miss the 02:15 job eleven
nights in twelve.

**Linux samples /proc for `--sample-seconds`** each cycle. Sampling misses
short-lived processes (observe.py says why); the window recorded is the
sampled span, never the hour.

Needs administrator on Windows (an ETW kernel session) and root on Linux for
other users' processes. Without it the cycle refuses rather than recording a
window it did not watch.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import platform
import socket
import sys
import time
from pathlib import Path

from .graph import (
    host_map, merge, merge_store, open_store, record_window, register_host,
    render_scream_test, resolve_observations, scream_test,
)
from .jobs import collect_jobs, format_window
from .observe import normalize_path, sample_proc_fds
from .paths import parse_mountinfo, resolve, resolve_windows

SESSION = "lineage-collect"


def self_aliases(host):
    """Every name this machine answers to, so a peer naming it by IP and this
    agent naming itself by hostname land on one host."""
    names = {host, socket.getfqdn().lower()}
    try:
        names |= {ai[4][0] for ai in socket.getaddrinfo(socket.gethostname(), None)}
    except OSError:
        pass
    return sorted(n for n in names if n and not n.startswith(("127.", "::1", "fe80")))


# --------------------------------------------------------------------------
# Taps
# --------------------------------------------------------------------------
def _windows_cycle(home, host, max_mb):
    from .etw import correlate, decode, is_elevated, process_images, start_session, stop_session
    if not is_elevated():
        raise SystemExit("needs administrator: an ETW kernel session cannot be started unelevated")
    d = home / "etw"
    d.mkdir(exist_ok=True)
    cur, state_f = d / "current.etl", d / "current.json"

    stop_session(SESSION)                       # idempotent; also clears a stale one after reboot
    prev = json.loads(state_f.read_text()) if state_f.exists() else {}
    done = None
    if cur.exists():
        done = d / f"done-{int(time.time())}.etl"
        cur.replace(done)
    start_session(SESSION, str(cur), max_mb)
    state_f.write_text(json.dumps({"started": time.time(), "mono": time.monotonic(),
                                   "images": process_images()}))

    if done is None:
        return "etw", [], None, {"note": "first cycle: trace started, nothing to decode yet"}
    try:
        images = {int(k): v for k, v in prev.get("images", {}).items()}
        corr = correlate(decode(done), host, images)
        obs = corr.flush()
        # The session is circular, so an hour busier than max_mb loses its
        # start. The window begins at the first event we still hold, never at
        # the session start, so a wrapped trace cannot overstate coverage.
        # After a reboot the file stopped growing at shutdown: end at its mtime.
        first = min((o["ts"] for o in obs), default=None)
        ended = done.stat().st_mtime
        started = prev.get("started", ended)
        mono = prev.get("mono")
        if mono is not None and time.monotonic() >= mono:
            # Same boot. The wall clock can jump -- NTP correcting a VM whose
            # RTC came up hours off, seen on the first deploy -- and uptime
            # cannot, so the session's real start is measured by uptime.
            started = ended - (time.monotonic() - mono)
        if first is not None:
            started = max(started, first)
        cov = corr.coverage()
        if ended <= started:
            cov["note"] = "clock moved during this window; observations kept, window not counted"
            return "etw", obs, None, cov
        return "etw", obs, (started, ended), cov
    finally:
        done.unlink(missing_ok=True)


def _linux_cycle(host, seconds, every=5):
    started = time.time()
    obs = []
    while True:
        obs += sample_proc_fds(host=host)
        if time.time() - started >= seconds:
            break
        time.sleep(every)
    return "proc-sample", obs, (started, time.time()), {"samples_every_s": every}


def _linux_resolver(host):
    """Resolve each path in its own process's mount namespace: a container's
    `/data` is not the host's `/data`."""
    cache = {}

    def mounts_for(pid):
        if pid not in cache:
            for src in (f"/proc/{pid}/mountinfo", "/proc/self/mountinfo"):
                try:
                    with open(src, encoding="utf-8", errors="replace") as f:
                        cache[pid] = parse_mountinfo(f.read())
                    break
                except OSError:
                    continue
        return cache.get(pid, [])

    return lambda o: resolve(o["path"], mounts_for(o.get("pid")), host, o.get("container"))


# --------------------------------------------------------------------------
# D1 + D2 snapshot
# --------------------------------------------------------------------------
def _mounts():
    if platform.system() == "Windows":
        from .paths import nt_device_map
        return {target: device for device, target in nt_device_map().items()}
    try:
        with open("/proc/self/mountinfo", encoding="utf-8", errors="replace") as f:
            return {m.mountpoint: m.source for m in parse_mountinfo(f.read()) if not m.is_pseudo()}
    except OSError:
        return {}


def write_snapshot(home, host, now=None):
    """One file per day, in agent-report shape so seasons.snapshot_from_reports
    reads it unchanged. A removed mount shows up as a `removed` row in the
    next same-phase diff -- the incident D2 exists for."""
    now = time.time() if now is None else now
    jobs = collect_jobs()
    for j in jobs:
        j.setdefault("host", host)
    mounts = _mounts()
    results = [{"id": f"job:{j['id']}", "value": j.get("schedule") or j.get("frequency")}
               for j in jobs if j.get("enabled", True)]
    results += [{"id": f"mount:{mp}", "value": src} for mp, src in sorted(mounts.items())]
    snap = {"hostname": host, "sent_at": now, "results": results, "jobs": jobs}
    d = home / "snapshots"
    d.mkdir(exist_ok=True)
    path = d / f"{dt.date.fromtimestamp(now).isoformat()}.json"
    path.write_text(json.dumps(snap, default=str))
    return path, len(jobs), len(mounts)


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------
def run(home, max_mb=256, sample_seconds=60):
    home = Path(home)
    home.mkdir(parents=True, exist_ok=True)
    host = socket.gethostname().lower()
    t0 = time.time()
    conn = open_store(str(home / "graph.db"))
    register_host(conn, host, self_aliases(host))

    if platform.system() == "Windows":
        tap, obs, window, cov = _windows_cycle(home, host, max_mb)
        resolver = lambda o: resolve_windows(o["path"], host)  # noqa: E731
    else:
        tap, obs, window, cov = _linux_cycle(host, sample_seconds)
        resolver = _linux_resolver(host)

    # The collector's own reads and writes are not the estate's. The first
    # live report listed python.exe reading lineage's source as a dependent.
    own = normalize_path(str(home.resolve())).rstrip("/") + "/"
    # Compared with a trailing slash so opens of the home directory itself go too.
    obs = [o for o in obs if not (o["path"].rstrip("/") + "/").lower().startswith(own.lower())]
    rows = resolve_observations(obs, resolver, aliases=host_map(conn))
    merged = merge(conn, rows)
    if window:
        record_window(conn, host, tap, *window)
    snap, njobs, nmounts = write_snapshot(home, host)

    summary = {
        "at": dt.datetime.now().isoformat(timespec="seconds"), "host": host, "tap": tap,
        "window": format_window((window[1] - window[0]) / 86400) if window else None,
        "observations": len(obs), "resolved": len(rows), "edge_rows": merged,
        "jobs": njobs, "mounts": nmounts, "coverage": cov,
        "elapsed_s": round(time.time() - t0, 1),
    }
    print(json.dumps(summary, default=str))
    return summary


def report(store, host=None, snapshots=None):
    conn = open_store(str(store))
    out = []
    for r in conn.execute("""SELECT host, tap, COUNT(*) n, MIN(started) a, MAX(ended) b,
                                    SUM(ended - started) watched FROM windows GROUP BY host, tap"""):
        span = (r["b"] - r["a"]) / 86400
        out.append(f"{r['host']:<20} {r['tap']:<12} {r['n']:>4} windows  "
                   f"watched {format_window(r['watched'] / 86400)} of {format_window(span)}  "
                   f"last {dt.datetime.fromtimestamp(r['b'], dt.timezone.utc).isoformat(timespec='minutes')}")
    if not out:
        return "no windows recorded yet"
    out.append("")
    for r in conn.execute("""SELECT host, COUNT(*) rows, SUM(count) obs, COUNT(DISTINCT actor) actors
                             FROM edges GROUP BY host"""):
        out.append(f"{r['host']:<20} {r['rows']:>6} edge rows  {r['obs']:>8} observations  "
                   f"{r['actors']:>4} actors")
    out.append("\nbusiest writers:")
    for r in conn.execute("""SELECT host, actor, SUM(count) n, COUNT(*) artifacts FROM edges
                             WHERE direction != 'read' GROUP BY host, actor ORDER BY n DESC LIMIT 10"""):
        out.append(f"  {r['n']:>7}  {r['host']:<16} {os.path.basename(r['actor'].replace(chr(92), '/'))[:40]:<40} "
                   f"{r['artifacts']} artifact(s)")
    if host:
        jobs = []
        snap_dir = Path(snapshots) if snapshots else None
        if snap_dir and snap_dir.is_dir():
            latest = sorted(snap_dir.glob("*.json"))
            if latest:
                jobs = json.loads(latest[-1].read_text()).get("jobs", [])
        out += ["", render_scream_test(scream_test(conn, host, jobs=jobs))]
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m lineage.collect", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="one collection cycle")
    r.add_argument("--home", default=os.environ.get("LINEAGE_HOME", "var/collect"))
    r.add_argument("--max-mb", type=int, default=256, help="ETW circular buffer cap (Windows)")
    r.add_argument("--sample-seconds", type=int, default=60, help="/proc sampling span (Linux)")
    m = sub.add_parser("merge", help="fold per-host stores into a central one")
    m.add_argument("store")
    m.add_argument("sources", nargs="+")
    p = sub.add_parser("report", help="coverage, busiest actors, and a scream test")
    p.add_argument("--store", required=True)
    p.add_argument("--host")
    p.add_argument("--snapshots", help="that host's snapshot directory, for its declared jobs")
    args = ap.parse_args(argv)

    if args.cmd == "run":
        run(args.home, args.max_mb, args.sample_seconds)
    elif args.cmd == "merge":
        conn = open_store(args.store)
        for src in args.sources:
            print(f"{src}: {', '.join(merge_store(conn, src)) or 'empty'}")
    else:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        print(report(args.store, args.host, args.snapshots))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
