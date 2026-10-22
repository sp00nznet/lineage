# Driving Windows guests through the Proxmox guest agent

`tools/pve.py` deploys and pulls lineage on Windows VMs that have no SSH and no
WinRM, using only the QEMU guest agent and the Proxmox API (stdlib `urllib`).
The agent runs as SYSTEM, which is exactly what an ETW kernel session needs. It
is also flaky in specific ways, and each rule below was learned by breaking a
VM. Tested against qemu-ga 110.0.2 on Windows 10 22H2, Proxmox VE 9.

## The rules

**Retry "not running" and "got timeout".** A busy Windows agent misses PVE's
short QMP timeout. PVE then answers `500 QEMU guest agent is not running` or
`500 VM <vmid> qmp command 'guest-exec-status' failed - got timeout`, and the next
call succeeds. `Pve._call` retries those two messages up to five times.

**Never use the agent's `file-write`.** It is open, write and close as separate
agent calls. When one times out, the agent keeps the handle, and every later
access fails until the agent restarts:

```
HTTP Error 500: Agent error: failed to open file 'C:\ProgramData\lineage\app\lineage\__init__.py':
The process cannot access the file because it is being used by another process.
```

**Never use exec's `input-data` (stdin).** The Windows agent writes the child's
stdin synchronously in its main loop. A 54 KiB payload wedged it completely:
every call returned "not running" for as long as we waited.

**Put the payload on the command line instead.** `Pve.write` sends 20 KiB of
base64 per `exec` inside a `powershell -Command` string (base64 needs no
quoting) and writes each chunk at its own offset, so a retried chunk is
harmless. A Windows command line holds 32,767 characters.

**Use `-EncodedCommand` for scripts.** UTF-16LE base64 survives every layer of
quoting between Python, the PVE API, the agent and `CreateProcess`. Prefix
`$ProgressPreference = 'SilentlyContinue'`, or progress records arrive on
stderr as CLIXML.

**`exec-status` forgets a pid once it has reported it.** A retry after a
timed-out status call that *did* reach the agent gets
`Agent error: PID … does not exist`. The command ran; its output is lost.

**Restart a stuck agent from a scheduled task, not from exec.** The agent kills
its own children when it stops, so `Restart-Service QEMU-GA` run through exec
dies with it. A one-shot SYSTEM task survives:

```powershell
$a = New-ScheduledTaskAction -Execute powershell.exe -Argument "-NoProfile -Command Restart-Service QEMU-GA -Force"
Register-ScheduledTask -TaskName lineage-restart-qga -Action $a -User SYSTEM -RunLevel Highest -Force
Start-ScheduledTask lineage-restart-qga
```

## Before deploying, check the host

`tools/pve.py` writes about 30 MB to the guest (the embeddable Python), and the
collector keeps a circular ETW file of up to 256 MB. On thin-provisioned
storage that space comes out of the host's pool, so check it first:

```
lvs -o lv_name,data_percent,metadata_percent pve/data
```

A pool at 99.77% had been dropping one guest's writes for ten days (see
[findings.md](findings.md)) before a deploy tipped it into a bugcheck.

## Getting a broken guest back

- **Automatic Repair after a crash:** stop and start the VM from Proxmox
  (`qm stop`, `qm start`); WinRE ignores the ACPI power button.
- **Passthrough GPU stuck in D3cold** (`failed to reset PCI device`): a PCI
  remove/rescan did not recover an RX 6700 XT. Boot without it
  (`qm set <vmid> --delete hostpci0`, after copying `/etc/pve/qemu-server/<vmid>.conf`)
  and re-add the line after a host reboot.
- **Clock hours off after the reboot:** `Start-Service w32time; w32tm /resync /force`.
