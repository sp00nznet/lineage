"""lineage.observe — D3: who touched what, and which way.

One record shape for every platform and every tap:

    Observation(actor, path, direction, ts, host, pid, container)

Everything above this line is a tap (platform-specific, replaceable, dumb).
Everything below it is rollup and pattern logic (portable, testable, and the
part that decides whether the output is a pipeline or a thousand orphans).

## The container constraint, learned from a real estate

Most guests in the target environment are **LXC containers**, and kernel-level
file observation does not work inside an unprivileged one: audit netlink is
namespaced and blocked, and eBPF generally cannot load. So the tap runs on the
**hypervisor host**, where there is a real kernel, and attributes activity back
to a container through its cgroup path. That is fewer agents (two hosts, not
sixteen guests) and one extra attribution step.

## What sampling cannot see, stated up front

`/proc`-descriptor sampling catches any process that holds a file open longer
than the sample interval. It **misses short-lived processes** -- and a cron'd
Perl script that runs for nine seconds at 02:15 is exactly that, which is
exactly the case this project exists for.

So sampling is the zero-dependency floor, not the answer. It is paired with
exec-event capture (D1 knows the job exists and when it should have run), and
the real upgrade is fanotify or auditd **on the host**, which works there
precisely because the host is not a container. Never let the UI imply that a
sampled window saw everything.
"""
from __future__ import annotations

import os
import platform
import re
import subprocess
import time
from collections import defaultdict

READ, WRITE, BOTH = "read", "write", "readwrite"

# --------------------------------------------------------------------------
# Path patterns -- make-or-break #2 from the design notes
# --------------------------------------------------------------------------
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
_ISO_DATE = re.compile(r"\d{4}[-_]?\d{2}[-_]?\d{2}")
_LONG_HEX = re.compile(r"\b[0-9a-f]{12,}\b", re.I)
_DIGIT_RUN = re.compile(r"\d{4,}")
_WHOLE_NUM_PART = re.compile(r"(?<=\.)\d{1,3}(?=\.|$)")

NOISE_PREFIXES = (
    "/proc/", "/sys/", "/dev/", "/run/", "/tmp/systemd-",
    "c:/windows/", "c:/programdata/microsoft/", "c:/$",
)
NOISE_SUFFIXES = (".pyc", ".lock", ".pid", ".swp", ".tmp~")


def normalize_path(path):
    """Separators and case only. Canonicalisation across hosts is D4's job."""
    p = str(path).replace("\\", "/").strip()
    p = re.sub(r"/{2,}", "/", p) if not p.startswith("//") else p
    if re.match(r"^[a-zA-Z]:/", p):
        p = p[0].lower() + p[1:]
    return p


def collapse_path(path):
    """`/data/export_2026-08-09.csv` -> `/data/export_<date>.csv`.

    Without this, every night mints fresh nodes and the graph grows forever
    without ever converging on the pipeline it is supposed to show.

    Deliberately conservative -- only the filename is collapsed, and only for
    shapes that are unambiguously serial. `load.pl` must survive untouched, and
    a version number in a directory (`/opt/proton/GE-Proton8-25/`) is part of
    the identity of that path, not noise.
    """
    p = normalize_path(path)
    head, sep, base = p.rpartition("/")
    if not sep:
        head, base = "", p
    base = _UUID.sub("<uuid>", base)
    base = _ISO_DATE.sub("<date>", base)
    base = _LONG_HEX.sub("<hex>", base)
    base = _DIGIT_RUN.sub("<n>", base)
    base = _WHOLE_NUM_PART.sub("<n>", base)      # logrotate's syslog.1, syslog.2
    return f"{head}{sep}{base}" if sep else base


def is_noise(path):
    """Includes the noise directories themselves and bare drive roots: every
    process opens `c:/windows` and `c:/` while resolving paths, and the first
    unattended report listed exactly those as "dependents"."""
    p = normalize_path(path).lower()
    return (p.startswith(NOISE_PREFIXES) or p.endswith(NOISE_SUFFIXES)
            or p + "/" in NOISE_PREFIXES or re.fullmatch(r"[a-z]:/?", p) is not None)


# --------------------------------------------------------------------------
# Observations
# --------------------------------------------------------------------------
def observation(actor, path, direction, ts=None, host=None, pid=None, container=None):
    return {
        "actor": str(actor),
        "path": normalize_path(path),
        "pattern": collapse_path(path),
        "direction": direction,
        "ts": time.time() if ts is None else ts,
        "host": host or platform.node(),
        "pid": pid,
        "container": container,
    }


def rollup(observations, drop_noise=True):
    """Observations -> edges. One per (actor, artifact pattern, direction).

    Counts and first/last seen are kept because "read once, three months ago"
    and "read every night" are different facts that a boolean edge destroys.
    """
    edges = {}
    for o in observations:
        if drop_noise and is_noise(o["path"]):
            continue
        key = (o["host"], o["container"], o["actor"], o["pattern"], o["direction"])
        e = edges.get(key)
        if e is None:
            e = edges[key] = {
                "host": o["host"], "container": o["container"], "actor": o["actor"],
                "pattern": o["pattern"], "direction": o["direction"],
                "count": 0, "first_seen": o["ts"], "last_seen": o["ts"],
                "examples": [],
            }
        e["count"] += 1
        e["first_seen"] = min(e["first_seen"], o["ts"])
        e["last_seen"] = max(e["last_seen"], o["ts"])
        if o["path"] not in e["examples"] and len(e["examples"]) < 3:
            e["examples"].append(o["path"])
    return sorted(edges.values(), key=lambda e: (-e["count"], e["pattern"]))


def link_pipelines(edges):
    """writer -> artifact -> reader. The graph, such as it is.

    Cross-host links are called out because they are both the most valuable
    finding and the ones no single machine could have told you. They are also
    the only ones that depend on D4's canonicalisation being right -- a
    same-host link is trustworthy today; a cross-host one is a hypothesis until
    the mount join confirms both sides mean the same bytes.
    """
    writers, readers = defaultdict(list), defaultdict(list)
    for e in edges:
        if e["direction"] in (WRITE, BOTH):
            writers[e["pattern"]].append(e)
        if e["direction"] in (READ, BOTH):
            readers[e["pattern"]].append(e)

    links = []
    for pattern, ws in writers.items():
        for w in ws:
            for r in readers.get(pattern, ()):
                if (w["actor"], w["host"], w["container"]) == (r["actor"], r["host"], r["container"]):
                    continue                     # a process reading what it wrote
                cross = (w["host"], w["container"]) != (r["host"], r["container"])
                links.append({
                    "artifact": pattern,
                    "producer": {"host": w["host"], "container": w["container"], "actor": w["actor"]},
                    "consumer": {"host": r["host"], "container": r["container"], "actor": r["actor"]},
                    "cross_host": cross,
                    "confidence": "observed-both-sides",
                    "needs_path_join": cross,
                    "observations": min(w["count"], r["count"]),
                })
    links.sort(key=lambda l: (not l["cross_host"], -l["observations"]))
    return links


def orphan_artifacts(edges):
    """Written by someone, read by nobody -- or the reverse.

    A written-never-read artifact is a decommissioning candidate. A
    read-never-written one means the producer is outside what we are watching,
    which is usually the more interesting finding: something off-agent matters.
    """
    written, read = set(), set()
    for e in edges:
        if e["direction"] in (WRITE, BOTH):
            written.add(e["pattern"])
        if e["direction"] in (READ, BOTH):
            read.add(e["pattern"])
    return {
        "written_never_read": sorted(written - read),
        "read_never_written": sorted(read - written),
    }


# --------------------------------------------------------------------------
# Taps
# --------------------------------------------------------------------------
# Proxmox writes both `/lxc/114/ns/...` and `/lxc.payload.114/...` depending on
# version and cgroup mode; `lxc.monitor.<id>` is the supervisor side of the same
# container. Matching only the first form silently attributes half a host's
# containers to the host itself.
#
# The separator after `lxc` must be `.` or `/`, never `-`: the host runs a
# systemd unit called `lxc-monitord.service`, and a looser pattern invents a
# container named "monitord" out of it. Caught on a live Proxmox node, which is
# the only place it shows up.
_CGROUP_LXC = re.compile(r"/lxc(?:\.(?:payload|monitor))?[./](\d+|[A-Za-z0-9_\-]+)(?=[/.]|$)")
_CGROUP_QEMU = re.compile(r"/qemu\.slice/(\d+)\.scope")
_CGROUP_DOCKER = re.compile(r"/docker[-/]([0-9a-f]{12,})")


def container_of(pid, root="/proc"):
    """Map a host pid to the guest it belongs to, via its cgroup.

    This is the piece that makes a hypervisor-host tap usable: without it every
    observation is attributed to the host and the per-guest picture is lost.
    """
    try:
        with open(f"{root}/{pid}/cgroup", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return None
    m = _CGROUP_LXC.search(text)
    if m:
        return f"lxc:{m.group(1)}"
    m = _CGROUP_QEMU.search(text)
    if m:
        return f"qemu:{m.group(1)}"
    m = _CGROUP_DOCKER.search(text)
    if m:
        return f"docker:{m.group(1)[:12]}"
    return None


def _proc_exe(pid, root="/proc"):
    try:
        return os.readlink(f"{root}/{pid}/exe")
    except OSError:
        pass
    try:
        with open(f"{root}/{pid}/comm", encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return f"pid:{pid}"


def sample_proc_fds(root="/proc", host=None, include_sockets=False):
    """One sweep of every process's open file descriptors.

    Zero dependencies, works unprivileged for your own processes and fully as
    root, and works on a Proxmox host across every container at once. Direction
    comes from the descriptor's access mode in fdinfo.

    ponytail: sampling, not tracing. It sees anything held open longer than the
    interval and misses everything shorter -- which includes the short cron job
    that is the motivating case. It is the floor, not the destination; fanotify
    on the host is the upgrade, and the UI must never imply otherwise.
    """
    out = []
    now = time.time()
    if not os.path.isdir(root):
        return out
    for pid in os.listdir(root):
        if not pid.isdigit():
            continue
        fd_dir = f"{root}/{pid}/fd"
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            continue                       # exited, or not ours to look at
        actor, container = None, None
        for fd in fds:
            try:
                target = os.readlink(f"{fd_dir}/{fd}")
            except OSError:
                continue
            if target.startswith(("socket:", "pipe:", "anon_inode:")):
                if not include_sockets:
                    continue
            elif not target.startswith("/"):
                continue
            if actor is None:
                actor = _proc_exe(pid, root)
                container = container_of(pid, root)
            out.append(observation(actor, target, _fd_direction(root, pid, fd),
                                   ts=now, host=host, pid=int(pid), container=container))
    return out


_ACCMODE = {0: READ, 1: WRITE, 2: BOTH}


def _fd_direction(root, pid, fd):
    try:
        with open(f"{root}/{pid}/fdinfo/{fd}", encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("flags:"):
                    return _ACCMODE.get(int(line.split()[1], 8) & 3, READ)
    except (OSError, ValueError, IndexError):
        pass
    return READ


def read_sysmon(max_events=5000, host=None):
    """Windows: Sysmon's operational log if it is installed.

    Far lazier than an ETW consumer and the right default *when present* --
    but it is not present by default, so `available()` decides rather than
    assuming.
    """
    if platform.system() != "Windows":
        return []
    ps = (
        "Get-WinEvent -LogName Microsoft-Windows-Sysmon/Operational "
        f"-MaxEvents {int(max_events)} -ErrorAction Stop | "
        "Where-Object Id -in 11,23,26 | "
        "ForEach-Object { $x=[xml]$_.ToXml(); $d=@{}; "
        "$x.Event.EventData.Data | ForEach-Object { $d[$_.Name]=$_.'#text' }; "
        "'{0}`t{1}`t{2}' -f $d['Image'],$d['TargetFilename'],$_.TimeCreated.ToFileTimeUtc() }"
    )
    try:
        proc = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                              capture_output=True, text=True, timeout=120)
    except Exception:
        return []
    if proc.returncode != 0:
        return []
    out = []
    for line in proc.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 3 or not parts[1]:
            continue
        image, target, filetime = parts
        try:
            ts = int(filetime) / 1e7 - 11644473600
        except ValueError:
            ts = time.time()
        out.append(observation(image or "?", target, WRITE, ts=ts, host=host))
    return out


def sysmon_available():
    if platform.system() != "Windows":
        return False
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "if (Get-WinEvent -ListLog Microsoft-Windows-Sysmon/Operational "
             "-ErrorAction SilentlyContinue) { 'yes' } else { 'no' }"],
            capture_output=True, text=True, timeout=60)
        return proc.stdout.strip() == "yes"
    except Exception:
        return False


def best_tap(host=None):
    """Pick the collector this machine can actually support, and say which.

    Returned alongside the observations so the report can state its own
    provenance. "Sampled" and "traced" are different evidence and a consumer
    that cannot tell them apart will over-trust the weaker one.
    """
    if platform.system() == "Windows":
        if sysmon_available():
            return "sysmon", lambda: read_sysmon(host=host)
        # Imported lazily: etw imports this module, so a top-level import here
        # would be a cycle.
        from .etw import etw_available
        if etw_available():
            return "etw", lambda: []      # driven by tools/etw_capture.py, not a one-shot sweep
        # ETW needs administrator. Without it there is no tap, and saying so is
        # the only acceptable answer -- claiming coverage we do not have is the
        # one unrecoverable failure for a tool people decommission boxes with.
        return "none", lambda: []
    if os.path.isdir("/proc"):
        return "proc-sample", lambda: sample_proc_fds(host=host)
    return "none", lambda: []


def collect(host=None):
    name, tap = best_tap(host)
    obs = tap()
    return {
        "tap": name,
        "sampled": name == "proc-sample",
        "observations": obs,
        "collected_at": time.time(),
    }
