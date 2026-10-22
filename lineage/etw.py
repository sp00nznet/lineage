"""lineage.etw — the Windows tap. No Sysmon, no driver, no dependency.

Sysmon is not installed on most Windows estates, so `best_tap()` was honestly
returning "none" there. ETW closes that gap: `Microsoft-Windows-Kernel-File` is
an in-box provider consumable from user mode, so this costs no EV certificate
and no kernel code.

## The correlation problem, which is the whole module

Read and Write events carry **no filename**. Straight from the provider
manifest on a live machine:

    id=12  Create        Irp, FileObject, IssuingThreadId, CreateOptions, ..., FileName
    id=16  Write         ByteOffset, Irp, FileObject, FileKey, IssuingThreadId, IOSize, ...
    id=10  NameCreate    FileKey, FileName

So a write says *"someone wrote to object 0xffff9e0c…"* and nothing else. The
name has to be carried forward from the Create that opened it, or from a
NameCreate that mapped the key. That bookkeeping is this file.

## Why READ is not captured

Read volume on a busy machine is enormous and mostly noise. Instead every
**open** is captured (Create carries the name), and direction is decided by
whether a Write was later seen on that same object.

That means a "read" here is really *"opened and not written"*. For dependency
discovery that is the relationship that matters -- a job that opens a file is
depending on it whether or not the bytes moved -- but it is an approximation
and it is labelled as one rather than reported as an observed read.

## Two things that must not be silently wrong

- **The maps are bounded.** A long-running agent holding a dict keyed by every
  file object the machine has ever opened is a memory leak with a schedule.
  Eviction is counted, never silent.
- **Unresolvable events are reported.** A write to a handle opened before the
  trace started can never be named. That is a coverage gap, and this module
  states its size rather than dropping the events and looking complete.
"""
from __future__ import annotations

import collections
import subprocess

from .observe import READ, WRITE, observation

PROVIDER = "Microsoft-Windows-Kernel-File"

# Actor names. A link is between actors, so `pid:29428 -> pid:97780` is a
# useless edge, and a pid snapshot taken at the end of a capture misses every
# short-lived process -- the nine-second script at 02:15 this project exists
# for. ProcessStart names each process the moment it is born.
PROCESS_PROVIDER = "Microsoft-Windows-Kernel-Process"
PROCESS_KEYWORDS = 0x10                                  # WINEVENT_KEYWORD_PROCESS
PROCESS_START = 1

# FILENAME | CREATE | WRITE | DELETE_PATH | CREATE_NEW_FILE.
# READ (0x100) is deliberately absent -- see the module docstring.
KEYWORDS = 0x10 | 0x80 | 0x200 | 0x400 | 0x1000          # 0x1690

NAME_CREATE, NAME_DELETE = 10, 11
CREATE, WRITE_EV, DELETE_PATH, RENAME_PATH, CREATE_NEW_FILE = 12, 16, 26, 27, 30

MAX_TRACKED_HANDLES = 20000
MAX_TRACKED_KEYS = 20000

# The provider ships two template versions for most events: older Windows emits
# ThreadId, newer emits IssuingThreadId. Both appear in the manifest on one
# machine, so a consumer that knows only one name silently loses half the
# fields on whichever build it was not written against.
_FIELD_ALIASES = {
    "thread_id": ("IssuingThreadId", "ThreadId"),
    "file_object": ("FileObject",),
    "file_key": ("FileKey",),
    "file_name": ("FileName", "FilePath"),
}


def field(data, logical, default=None):
    for name in _FIELD_ALIASES[logical]:
        if name in data and data[name] not in (None, ""):
            return data[name]
    return default


class _Bounded(collections.OrderedDict):
    """FIFO-evicting map that counts what it dropped."""

    def __init__(self, cap):
        super().__init__()
        self.cap = cap
        self.evicted = 0

    def put(self, key, value):
        if key in self:
            self.move_to_end(key)
        self[key] = value
        while len(self) > self.cap:
            self.popitem(last=False)
            self.evicted += 1


class Correlator:
    """Feed events in timestamp order; get observations out.

    Deliberately a pure state machine over decoded records -- no ETW calls, no
    subprocess -- so the part that is easy to get wrong is the part that can be
    tested without administrator rights or a live trace.
    """

    def __init__(self, host=None, max_handles=MAX_TRACKED_HANDLES, max_keys=MAX_TRACKED_KEYS,
                 images=None):
        self.host = host
        # pid -> image. Seeded from a process snapshot taken when the trace
        # started, then kept current by ProcessStart events, so pid reuse
        # during a long capture renames the actor at the right moment.
        self.images = dict(images or {})
        self.handles = _Bounded(max_handles)     # FileObject -> open record
        self.keys = _Bounded(max_keys)           # FileKey    -> name
        self.observations = []
        self.unresolved = collections.Counter()
        self.events = 0

    # -- ingest ----------------------------------------------------------
    def feed(self, event):
        """event: {"id": int, "pid": int, "ts": float, "data": {...}}

        Process events carry "provider": "process"; for those, `pid` is the
        new process, not the one that created it.
        """
        self.events += 1
        eid = event.get("id")
        data = event.get("data") or {}

        if event.get("provider") == "process":
            if eid == PROCESS_START and data.get("ImageName"):
                self.images[event.get("pid")] = data["ImageName"]
            return
        if not event.get("image"):
            event = {**event, "image": self.images.get(event.get("pid"))}

        if eid in (NAME_CREATE, NAME_DELETE):
            key, name = field(data, "file_key"), field(data, "file_name")
            if key and name:
                self.keys.put(key, name)
            return

        if eid in (CREATE, CREATE_NEW_FILE):
            obj, name = field(data, "file_object"), field(data, "file_name")
            if not obj:
                return
            if not name:
                name = self.keys.get(field(data, "file_key"))
            if not name:
                self.unresolved["create_without_name"] += 1
                return
            self.handles.put(obj, {
                "name": name, "pid": event.get("pid"), "ts": event.get("ts"),
                # Carried here because flush() happens long after the Create,
                # and by then the only thing naming this actor is what we kept.
                "image": event.get("image"),
                "wrote": eid == CREATE_NEW_FILE,       # creating a new file is a write
            })
            return

        if eid == WRITE_EV:
            obj = field(data, "file_object")
            rec = self.handles.get(obj)
            if rec is not None:
                rec["wrote"] = True
                return
            # The handle was opened before the trace began, or evicted. The key
            # map is the second chance; if that misses too, say so.
            name = self.keys.get(field(data, "file_key"))
            if name:
                self.observations.append(observation(
                    self._actor(event), name, WRITE,
                    ts=event.get("ts"), host=self.host, pid=event.get("pid")))
            else:
                self.unresolved["write_unnamed"] += 1
            return

        if eid in (DELETE_PATH, RENAME_PATH):
            name = field(data, "file_name") or self.keys.get(field(data, "file_key"))
            if name:
                self.observations.append(observation(
                    self._actor(event), name, WRITE,
                    ts=event.get("ts"), host=self.host, pid=event.get("pid")))
            else:
                self.unresolved["delete_unnamed"] += 1

    def _actor(self, event):
        return event.get("image") or f"pid:{event.get('pid')}"

    # -- drain -----------------------------------------------------------
    def flush(self):
        """Emit one observation per tracked open handle, then reset.

        Direction is `write` if a Write was seen on that object, else `read` --
        which strictly means "opened and not written". See the module docstring;
        the approximation is deliberate and named.
        """
        out = list(self.observations)
        for rec in self.handles.values():
            out.append(observation(
                rec.get("image") or f"pid:{rec['pid']}", rec["name"],
                WRITE if rec["wrote"] else READ,
                ts=rec["ts"], host=self.host, pid=rec["pid"]))
        self.observations = []
        self.handles = _Bounded(self.handles.cap)
        return out

    def coverage(self):
        """What this capture could not account for.

        A tap that drops what it cannot name and reports the rest looks
        complete. The same rule as everywhere else in this project: state the
        gap, do not hide it.
        """
        total_unresolved = sum(self.unresolved.values())
        return {
            "events_seen": self.events,
            "unresolved": dict(self.unresolved),
            "unresolved_total": total_unresolved,
            "handles_evicted": self.handles.evicted,
            "keys_evicted": self.keys.evicted,
            "note": (
                f"{total_unresolved} event(s) could not be resolved to a path — "
                "usually handles opened before the trace started. "
                "Not a reason to treat the rest as complete."
            ) if total_unresolved else "all observed events resolved to a path",
        }


# --------------------------------------------------------------------------
# Capture -- thin on purpose; needs administrator
# --------------------------------------------------------------------------
def capture_command(session, etl_path, seconds, max_mb=128):
    """The exact in-box commands. Returned rather than run so they can be
    reviewed, and handed to someone with rights when we do not have them.

    `logman create` takes one provider; the process provider joins the running
    session through `logman update`, which is in-box and needs no provider file.
    """
    return {
        "start": ["logman", "create", "trace", session, "-p", PROVIDER,
                  f"0x{KEYWORDS:X}", "-o", etl_path, "-ets",
                  "-bs", "64", "-nb", "16", "256",
                  "-max", str(max_mb), "-mode", "Circular"],
        "add": [["logman", "update", "trace", session, "-p", PROCESS_PROVIDER,
                 f"0x{PROCESS_KEYWORDS:X}", "-ets"]],
        "stop": ["logman", "stop", session, "-ets"],
        "seconds": seconds,
    }


def is_elevated():
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def session_exists(session):
    try:
        out = subprocess.run(["logman", "query", "-ets"], capture_output=True,
                             text=True, timeout=30).stdout
    except Exception:
        return False
    return session.lower() in out.lower()


def stop_session(session):
    """Idempotent. A trace session that outlives its capture keeps writing to
    disk forever, so this is called on every exit path including failure."""
    try:
        subprocess.run(["logman", "stop", session, "-ets"],
                       capture_output=True, text=True, timeout=60)
    except Exception:
        pass


def etw_available():
    return is_elevated()


def start_session(session, etl_path, max_mb=128):
    """Start the trace with both providers, or raise. Never leaves a
    half-started session behind."""
    cmd = capture_command(session, etl_path, 0, max_mb)
    for argv in [cmd["start"]] + cmd["add"]:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            stop_session(session)
            out = ((r.stdout or "") + (r.stderr or "")).strip()
            raise RuntimeError(f"{' '.join(argv[:3])} failed: {out}")


# --------------------------------------------------------------------------
# Decode -- unelevated is fine
# --------------------------------------------------------------------------
# Get-WinEvent reads .etl directly, so there is no dependency on TraceEvent or a
# NuGet package, and decoding works unelevated even though capturing did not.
#
# ponytail: the ETL path is substituted with a plain token replace, not
# str.format(). PowerShell is brace-dense and `-f` format strings look exactly
# like Python fields, so escaping every literal brace is a bug waiting to
# happen -- and was one.
#
# The event-id filter runs BEFORE the XML parse: parsing is the expensive step
# and most events in a file trace are ones we do not use. ProcessStart's
# EventData ProcessID is the *new* process; System.Execution.ProcessID on that
# event is its parent, which is why the two providers emit different columns.
DECODE_PS = r"""
$ErrorActionPreference = 'SilentlyContinue'
Get-WinEvent -Path '__ETL_PATH__' -Oldest | ForEach-Object {
  $id = $_.Id
  $proc = $_.ProviderName -eq 'Microsoft-Windows-Kernel-Process'
  if ($proc) { if ($id -ne 1) { return } }
  elseif ($id -notin 10,11,12,16,26,27,30) { return }
  $x = [xml]$_.ToXml()
  $d = @{}
  foreach ($n in $x.Event.EventData.Data) { $d[$n.Name] = $n.'#text' }
  $ft = $_.TimeCreated.ToFileTimeUtc()
  if ($proc) {
    'P|{0}|{1}|{2}|||{3}' -f $id, $d['ProcessID'], $ft, $d['ImageName']
  } else {
    $fn = if ($d['FileName']) { $d['FileName'] } else { $d['FilePath'] }
    'F|{0}|{1}|{2}|{3}|{4}|{5}' -f $id, $x.Event.System.Execution.ProcessID,
        $ft, $d['FileObject'], $d['FileKey'], $fn
  }
}
"""


def parse_decoded(line):
    """One decoder line -> event dict, or None.

    Pipe-separated, but a Windows path never contains '|' and the path is last,
    so split from the left a fixed number of times and the remainder is the
    path, intact.
    """
    parts = line.rstrip("\r\n").split("|", 6)
    if len(parts) != 7:
        return None
    kind, eid, pid, ft, fo, fk, fn = parts
    try:
        eid, pid = int(eid), int(pid)
        ts = int(ft) / 1e7 - 11644473600
    except ValueError:
        return None
    if kind == "P":
        return {"provider": "process", "id": eid, "pid": pid, "ts": ts,
                "data": {"ImageName": fn}}
    return {"id": eid, "pid": pid, "ts": ts, "image": None,
            "data": {"FileObject": fo, "FileKey": fk, "FileName": fn}}


def decode(etl_path):
    """Stream the decoder's stdout so a 200k-event trace does not buffer whole."""
    proc = subprocess.Popen(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command",
         DECODE_PS.replace("__ETL_PATH__", str(etl_path))],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1,
        encoding="utf-8", errors="replace")
    for line in proc.stdout:
        e = parse_decoded(line)
        if e is not None:
            yield e
    proc.stdout.close()
    proc.wait()


def process_images():
    """pid -> image path for every process alive now. Taken when a trace
    starts; ProcessStart events name everything born after.

    The separator is `|`, not a tab. The first version wrote '{0}`t{1}', and
    PowerShell does not expand escapes inside single quotes -- so no line ever
    split, the map was always empty, and every actor in the first live capture
    came out as `pid:NNNN`. A path cannot contain `|`.

    System (pid 4) and protected processes report no Path even to SYSTEM; their
    Name is a weaker identity than a path but far better than a number.
    """
    out = {}
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "Get-Process | ForEach-Object { '{0}|{1}' -f $_.Id, $(if ($_.Path) { $_.Path } else { $_.Name }) }"],
            capture_output=True, text=True, timeout=120)
        for line in proc.stdout.splitlines():
            pid, _, path = line.partition("|")
            if pid.strip().isdigit() and path.strip():
                out[int(pid)] = path.strip()
    except Exception:
        pass
    return out


def correlate(events, host, images=None, devices=None):
    """Decoded events -> a fed Correlator, every path in DOS form.

    ETW speaks NT device paths -- image names included. Translate before
    anything downstream sees them, or the noise filter and every path
    comparison silently miss.
    """
    from .paths import nt_device_map, to_dos_path
    devices = nt_device_map() if devices is None else devices
    corr = Correlator(host=host, images=images)
    for e in events:
        data = e.get("data") or {}
        for k in ("FileName", "ImageName"):
            if data.get(k):
                data[k] = to_dos_path(data[k], devices)
        corr.feed(e)
    return corr
