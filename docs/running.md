# Running lineage

## Collection on a Windows guest (Proxmox)

From a workstation with this checkout, with nothing installed in the guest
except the QEMU guest agent:

```
set PVE_HOST=<node address>
set PVE_PASSWORD=<root@pam password>
python tools\pve.py deploy <vmid> [<vmid> ...]
```

Real output:

```
vm <vmid>:
  pushed lineage/__init__.py
  ...
  pushed lineage/update.py
  registered: lineage-collect hourly + at boot, as SYSTEM
```

What it does, in order, and idempotently (rerun it to upgrade):

1. Downloads `python-3.12.10-embed-amd64.zip` from python.org *in the guest*,
   checks its SHA-256 against `PY_SHA256`, and unpacks it to
   `C:\ProgramData\lineage\python`. No installer, no PATH change.
2. Points the embeddable build's `python312._pth` at `C:\ProgramData\lineage\app`
   and copies `lineage/*.py` there.
3. Registers the `lineage-collect` scheduled task: hourly and at boot, as SYSTEM,
   50-minute execution limit, `IgnoreNew` on overlap. Starts it once.

Check the host's thin pool before deploying (`lvs pve/data`); see
[proxmox-guest-agent.md](proxmox-guest-agent.md).

## Collection anywhere else

Windows, from an elevated prompt (ETW kernel sessions need administrator):

```
python -m lineage.collect run --home C:\ProgramData\lineage
```

Schedule that hourly. Linux, as root so other users' processes are visible:

```
python3 -m lineage.collect run --home /var/lib/lineage --sample-seconds 60
```

Each cycle appends one JSON line to stdout. Real line from the first test VM:

```
{"at": "2026-10-15T01:39:36", "host": "win-a", "tap": "etw", "window": "0.6 minutes",
 "observations": 290, "resolved": 57, "edge_rows": 48, "jobs": 240, "mounts": 1,
 "coverage": {"events_seen": 18969, "unresolved": {"write_unnamed": 524}, ...}, "elapsed_s": 12.1}
```

`resolved` is lower than `observations` because `c:/windows/` and friends are
noise by design. `write_unnamed` counts writes to handles opened before the
trace started. They can never be named, so they are reported, not dropped.

## Reading it

```
python tools\pve.py pull <vmid> --into var\fleet
python -m lineage.collect report --store var\fleet\central.db --host <hostname> --snapshots var\fleet\<hostname>\snapshots
```

`pull` takes a consistent copy on the guest with sqlite's backup API, copies the
store and snapshots to `var/fleet/<hostname>/`, and merges into
`var/fleet/central.db`. Merging replaces that host's rows, so pull as often as
you like. `report` prints watched time against span, the busiest writers, and a
scream test for `--host`.

Or browse it: `python -m lineage.web --fleet varleet`, then open
http://127.0.0.1:8771. It is read-only and has no authentication, so it binds
to loopback; the data names every file and program on the watched hosts.

## Back up, restore, upgrade, remove

- **Back up:** `pull` is the backup: `var/fleet/<hostname>/graph.db` is a
  consistent copy. On the guest, copy `graph.db` and `snapshots\`; never copy
  `graph.db` mid-cycle without the backup API.
- **Restore:** stop the task, put `graph.db` back, start the task.
- **Upgrade:** rerun `deploy`. The running ETW session is restarted by the next
  cycle; no history is lost.
- **Remove:** in the guest, `Unregister-ScheduledTask lineage-collect -Confirm:$false;
  logman stop lineage-collect -ets; Remove-Item -Recurse C:\ProgramData\lineage`.

## Disk use

The ETW file is circular and capped by `--max-mb` (256 MB default); it is
renamed and deleted every cycle. `graph.db` grows with distinct
`(actor, artifact pattern)` pairs, not with events. Snapshots are one file a day.
On thin storage all of it comes out of the host's pool.
