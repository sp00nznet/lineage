"""Drive a Windows guest through the Proxmox QEMU guest agent. Stdlib only.

    PVE_HOST=<node address> PVE_PASSWORD=... python tools/pve.py exec <vmid> -- cmd /c ver

Used to deploy and pull lineage on VMs that have no SSH and no WinRM: the
guest agent is already there on any VM built from a Proxmox Windows template,
and it runs as SYSTEM, which is exactly the right that an ETW capture needs.

ponytail: TLS verification is off. A Proxmox node ships a self-signed
certificate, and this tool only talks to a node you named on the command line.
Pin the node's CA (PVE_CA=path) if this ever leaves a lab.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# A Windows command line is capped at 32,767 characters. 20 KiB of payload is
# 27 KiB of base64, leaving room for the script around it.
CHUNK = 20 * 1024


class Pve:
    def __init__(self, host, password, user="root@pam"):
        self.base = f"https://{host}:8006/api2/json"
        ca = os.environ.get("PVE_CA")
        self.ctx = ssl.create_default_context(cafile=ca) if ca else ssl._create_unverified_context()
        t = self._call("POST", "/access/ticket", {"username": user, "password": password}, auth=False)
        self.headers = {"Cookie": f"PVEAuthCookie={t['ticket']}",
                        "CSRFPreventionToken": t["CSRFPreventionToken"]}
        self.vm_node = {}

    def _call(self, method, path, params=None, auth=True):
        # A busy Windows guest agent misses PVE's short poll, and PVE reports it
        # as "not running" or "got timeout" even though the next call succeeds.
        # Seen on every few calls to a Win10 guest; a few tries clears it.
        for attempt in range(5):
            try:
                return self._call_once(method, path, params, auth)
            except urllib.error.HTTPError as e:
                transient = "not running" in str(e.reason) or "got timeout" in str(e.reason)
                if e.code != 500 or not transient or attempt == 4:
                    raise
                time.sleep(3)

    def _call_once(self, method, path, params, auth):
        data = None
        url = self.base + path
        if params is not None:
            enc = urllib.parse.urlencode(params, doseq=True)
            if method == "GET":
                url += "?" + enc
            else:
                data = enc.encode()
        req = urllib.request.Request(url, data=data, method=method,
                                     headers=self.headers if auth else {})
        with urllib.request.urlopen(req, context=self.ctx, timeout=120) as r:
            return json.load(r)["data"]

    def _vm(self, vmid):
        if vmid not in self.vm_node:
            for vm in self._call("GET", "/cluster/resources", {"type": "vm"}):
                self.vm_node[str(vm["vmid"])] = vm["node"]
        return f"/nodes/{self.vm_node[str(vmid)]}/qemu/{vmid}/agent"

    def exec(self, vmid, argv, timeout=900):
        """Run argv in the guest, wait, return (exitcode, stdout, stderr)."""
        pid = self._call("POST", self._vm(vmid) + "/exec", {"command": argv})["pid"]
        deadline = time.time() + timeout
        while time.time() < deadline:
            s = self._call("GET", self._vm(vmid) + "/exec-status", {"pid": pid})
            if s.get("exited"):
                return s.get("exitcode", -1), s.get("out-data", ""), s.get("err-data", "")
            time.sleep(2)
        raise TimeoutError(f"guest command still running after {timeout}s: {argv}")

    def ps(self, vmid, script, timeout=900):
        # -EncodedCommand sidesteps every layer of quoting between here and the guest.
        # Progress records otherwise arrive on stderr as CLIXML.
        script = "$ProgressPreference = 'SilentlyContinue'\n" + script
        enc = base64.b64encode(script.encode("utf-16-le")).decode()
        return self.exec(vmid, ["powershell", "-NoProfile", "-NonInteractive",
                                "-ExecutionPolicy", "Bypass", "-EncodedCommand", enc], timeout)

    def write(self, vmid, path, data: bytes):
        """Upload with the payload inline on a command line. Found the hard way:

        - the agent's file-write is open, write and close as separate calls;
          when one times out (routine on a busy Windows guest) the agent keeps
          the handle and the file stays locked until the agent restarts;
        - exec's input-data wedged the Windows agent outright -- it writes the
          child's stdin synchronously, and a 54 KiB payload never drained.

        A plain exec per chunk does neither, and writing each chunk at its own
        offset makes a retried chunk harmless. base64 needs no quoting.
        """
        for i in range(0, max(len(data), 1), CHUNK):
            mode = "Create" if i == 0 else "OpenOrCreate"
            b64 = base64.b64encode(data[i:i + CHUNK]).decode()
            code, _, err = self.exec(vmid, ["powershell", "-NoProfile", "-NonInteractive", "-Command", (
                f"$b = [Convert]::FromBase64String('{b64}'); "
                f"$f = [IO.File]::Open('{path}', [IO.FileMode]::{mode}); "
                f"[void]$f.Seek({i}, 'Begin'); $f.Write($b, 0, $b.Length); $f.Close()")])
            if code:
                raise RuntimeError(f"writing {path} failed: {err}")

    def read(self, vmid, path) -> bytes:
        # file-read returns text and truncates at 16 MiB, so binary files are
        # base64'd in the guest first.
        code, out, err = self.ps(vmid, f"[Convert]::ToBase64String([IO.File]::ReadAllBytes('{path}'))")
        if code:
            raise RuntimeError(f"reading {path} failed: {err}")
        return base64.b64decode(out.strip())


# --------------------------------------------------------------------------
# lineage on a guest
# --------------------------------------------------------------------------
HOME = r"C:\ProgramData\lineage"
TASK = "lineage-collect"
# The embeddable CPython: stdlib only (sqlite3 and ctypes included), no
# installer, no PATH change, removable by deleting one folder. 3.12.10 is the
# last 3.12 with Windows binaries. Hash taken from python.org on 2026-10-14.
PY_URL = "https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip"
PY_SHA256 = "4ACBED6DD1C744B0376E3B1CF57CE906F9DC9E95E68824584C8099A63025A3C3"

# ponytail: tokens replaced with str.replace, for the reason etw.DECODE_PS gives.
INSTALL_PS = r"""
$ErrorActionPreference = 'Stop'
$py = '__HOME__\python'
if (-not (Test-Path "$py\python.exe")) {
  New-Item -ItemType Directory -Force $py | Out-Null
  $z = "$env:TEMP\lineage-python.zip"
  [Net.ServicePointManager]::SecurityProtocol = 'Tls12'
  Invoke-WebRequest '__URL__' -OutFile $z -UseBasicParsing
  if ((Get-FileHash $z -Algorithm SHA256).Hash -ne '__SHA__') { throw 'python zip hash mismatch' }
  Expand-Archive $z $py -Force
  Remove-Item $z
}
# The ._pth file is the embeddable build's entire sys.path; adding app\ is how
# `-m lineage.collect` resolves without installing anything.
Set-Content "$py\python312._pth" "python312.zip`r`n.`r`n__HOME__\app" -Encoding ascii
New-Item -ItemType Directory -Force '__HOME__\app\lineage' | Out-Null
Get-ChildItem '__HOME__\app\lineage' -Filter *.py | Remove-Item
"""

# Hourly, plus at boot so a reboot costs minutes of trace rather than an hour.
# The 50-minute limit and IgnoreNew keep a slow decode from stacking cycles.
TASK_PS = r"""
$ErrorActionPreference = 'Stop'
$cmd = '/c __HOME__\python\python.exe -m lineage.collect run --home __HOME__ >> __HOME__\collect.log 2>&1'
$action = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument $cmd -WorkingDirectory '__HOME__'
$triggers = @(
  (New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 60)),
  (New-ScheduledTaskTrigger -AtStartup)
)
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 50) `
  -MultipleInstances IgnoreNew -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName '__TASK__' -Action $action -Trigger $triggers -Principal $principal `
  -Settings $settings -Description 'lineage: read-only dependency discovery (ETW file + process)' -Force | Out-Null
Start-ScheduledTask -TaskName '__TASK__'
'registered'
"""


def _fill(script, **kw):
    for k, v in kw.items():
        script = script.replace(f"__{k.upper()}__", v)
    return script


def deploy(pve, vmid):
    """Install python + lineage on the guest and schedule the collect cycle.
    Idempotent: rerun it to ship a new lineage build."""
    code, out, err = pve.ps(vmid, _fill(INSTALL_PS, home=HOME, url=PY_URL, sha=PY_SHA256))
    if code:
        raise RuntimeError(f"install failed: {err or out}")
    src = src_dir()
    for name in sorted(os.listdir(src)):
        if name.endswith(".py"):
            with open(os.path.join(src, name), "rb") as f:
                pve.write(vmid, rf"{HOME}\app\lineage\{name}", f.read())
            print(f"  pushed lineage/{name}")
    code, out, err = pve.ps(vmid, _fill(TASK_PS, home=HOME, task=TASK))
    if code:
        raise RuntimeError(f"scheduling failed: {err or out}")
    print(f"  {out.strip()}: {TASK} hourly + at boot, as SYSTEM")


def pull(pve, vmid, into):
    """Copy the guest's store and snapshots into `into/<hostname>/`, then fold
    the store into `into/central.db`. sqlite's backup API takes a consistent
    copy even if a cycle is writing at that moment; reading the raw file would not."""
    host = pve._call("GET", pve._vm(vmid) + "/get-host-name")["result"]["host-name"].lower()
    dest = os.path.join(into, host)
    os.makedirs(os.path.join(dest, "snapshots"), exist_ok=True)
    code, out, err = pve.exec(vmid, [rf"{HOME}\python\python.exe", "-c",
                                     f"import sqlite3; s=sqlite3.connect(r'{HOME}\\graph.db'); "
                                     f"d=sqlite3.connect(r'{HOME}\\pull.db'); s.backup(d); d.close()"])
    if code:
        raise RuntimeError(f"backup on guest failed: {err or out}")
    with open(os.path.join(dest, "graph.db"), "wb") as f:
        f.write(pve.read(vmid, rf"{HOME}\pull.db"))
    code, out, _ = pve.ps(vmid, rf"Get-ChildItem '{HOME}\snapshots' -Filter *.json | % Name")
    for name in out.split():
        with open(os.path.join(dest, "snapshots", name), "wb") as f:
            f.write(pve.read(vmid, rf"{HOME}\snapshots\{name}"))
    _, log, _ = pve.ps(vmid, rf"Get-Content '{HOME}\collect.log' -Tail 3 -EA SilentlyContinue")

    sys.path.insert(0, os.path.dirname(src_dir()))
    from lineage.graph import merge_store, open_store
    conn = open_store(os.path.join(into, "central.db"))
    merge_store(conn, os.path.join(dest, "graph.db"))
    conn.close()
    print(f"{host}: store + {len(out.split())} snapshot(s) -> {dest}")
    print(log.rstrip())
    return host


def src_dir():
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lineage")


def connect():
    host, pw = os.environ.get("PVE_HOST"), os.environ.get("PVE_PASSWORD")
    if not host or not pw:
        sys.exit("set PVE_HOST and PVE_PASSWORD (root@pam)")
    return Pve(host, pw)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("exec", help="run a command in the guest")
    e.add_argument("vmid"); e.add_argument("argv", nargs="+")
    p = sub.add_parser("ps", help="run a PowerShell script in the guest")
    p.add_argument("vmid"); p.add_argument("script")
    d = sub.add_parser("deploy", help="install lineage and schedule collection")
    d.add_argument("vmid", nargs="+")
    u = sub.add_parser("pull", help="fetch stores and merge into a central one")
    u.add_argument("vmid", nargs="+")
    u.add_argument("--into", default="var/fleet")
    args = ap.parse_args(argv)
    pve = connect()
    if args.cmd == "deploy":
        for v in args.vmid:
            print(f"vm {v}:"); deploy(pve, v)
        return 0
    if args.cmd == "pull":
        for v in args.vmid:
            pull(pve, v, args.into)
        return 0
    if args.cmd == "exec":
        code, out, err = pve.exec(args.vmid, args.argv)
    else:
        code, out, err = pve.ps(args.vmid, args.script)
    sys.stdout.write(out)
    sys.stderr.write(err)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
