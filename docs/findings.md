# Findings

Recorded as they turn up, so they don't get re-learned. Each one came from
running against real hardware, not from a test.


- **A failed check must not look like a finding.** The first `cim_value`
  returned `""` on failure, so `cim_value(…) != "1"` reported every machine as
  BitLocker-unencrypted. Caught on the first real run. Inspectors now raise and
  the agent records the raise — `value: null, error: "…Access denied"`. Any
  inspector that returns a sentinel on failure is a bug.
- **`Win32_EncryptableVolume` is not in `root/cimv2`**, and it needs admin.
  `cim_value` takes an explicit namespace argument for exactly this.
- **`ProductName` still reads "Windows 10 Pro" on Windows 11.** Verified on
  build 26200. Never write content against `os_caption()` to distinguish
  versions — use `os_build() >= "10.0.22000"`.
- **String comparison on versions is wrong and looks right.** `"1.10" < "1.9"`
  lexically. Hence the `Version` type, and hence it coerces whenever either side
  of a comparison is one.
- **The agent is a user-mode service, not a driver.** No EV certificate, no
  attestation signing, no HVCI compatibility work. The expense here is
  reliability, not signing.
- **cron ORs day-of-month and day-of-week when both are restricted.** `0 0 1 * 1`
  is "the 1st *or* every Monday" — a weekly job, not a monthly one. Reading it as
  AND overstates the interval and would wrongly mark a weekly job invisible to a
  two-week window. `cron_max_gap_days` simulates four years of days rather than
  reasoning symbolically, which gets this and leap years for free.
- **Most Windows scheduled tasks have no period at all.** On a real laptop: 189
  tasks, 159 with no declared frequency — `WnfStateChangeTrigger`, `LogonTrigger`,
  `BootTrigger`, or no trigger at all. They are event-driven, and calling that
  "annual" would be a lie in the dangerous direction.
- **Disabled triggers still appear in the XML.** Windows' own *Data Integrity
  Check And Scan* ships a daily trigger and a boot trigger, both
  `Enabled=false`. Counting them would put a job that never runs into the
  coverage numbers.
- **Two firings a day is not twice the coverage.** 01:00 and 23:00 leaves a
  22-hour blind spot. Max gap, not average spacing, is what exposure means.
- **ETW closes the Windows gap with no driver and no certificate.**
  `Microsoft-Windows-Kernel-File` is an in-box provider consumable from user
  mode, so the Windows tap costs no EV cert and no kernel code — it costs
  administrator rights, which is a deployment question rather than a money one.
- **ETW Read and Write events carry no filename.** Only `FileObject`/`FileKey`;
  the name arrives on a separate Create or NameCreate event and must be carried
  forward. That bookkeeping is the entire Windows tap, and it is why the
  correlator is a pure state machine that can be tested without a live trace.
- **The provider ships two template versions in one manifest** — older Windows
  emits `ThreadId`, newer emits `IssuingThreadId`. A consumer that knows one
  name silently loses the field on the other build. Exactly the class of thing a
  spread of Windows ISOs is worth testing against.
- **READ is deliberately not captured.** Read volume on a busy machine is
  enormous and mostly noise; every *open* carries the name anyway, so direction
  is decided by whether a Write followed on that handle. A "read" here strictly
  means "opened and not written" — an approximation, labelled as one.
- **ETW speaks NT device paths, not DOS paths.** The first live capture returned
  `\Device\HarddiskVolume10\projects\x` for everything, so the noise filter never
  fired (it matches `c:/windows/`) and no path could match one from another
  source. `QueryDosDevice` over the 26 letters fixes it; `Win32_Volume` does not,
  because its `DeviceID` is a `\\?\Volume{guid}` form ETW never emits. Longest
  match first, or `HarddiskVolume1` shadows `HarddiskVolume10`.
- **A mapped network drive must resolve to its server, not its letter.** On this
  machine `Y:` is `\Device\LanmanRedirector\;Y:…\192.0.2.23\beta` — the *same*
  ZFS dataset already observed directly on that server. Filing it under
  `dev://thisbox/y:` would make the cross-machine link permanently unformable.
  This is the container-path mistake one level up, and only real hardware showed
  it.
- **A tap must state what it could not name.** Writes to handles opened before
  the trace began can never be resolved to a path. They are counted and
  reported, because a collector that drops those and reports the rest looks
  complete.
- **The tap cannot live inside an LXC container.** Audit netlink is namespaced
  and blocked, eBPF will not load. It runs on the hypervisor host and attributes
  back through cgroups — two agents per Proxmox pair instead of sixteen inside
  guests. Verified live: 48k observations in 30s, four containers, correct VMIDs.
- **`lxc-monitord.service` is not a container.** A cgroup pattern that accepts
  `lxc-` as a separator invents one named "monitord" from a host systemd unit.
  Proxmox writes `/lxc/114/`, `/lxc.payload.114/` *and* `/lxc.monitor.114/`;
  matching only the first silently attributes half a node to the node itself.
- **A path string tells you nothing, in either direction.** Same string can be
  the same file (two containers binding one dataset) *or* a different one (two
  containers binding different sources at the same mountpoint); different
  strings are routinely the same file (a bind of a nested dataset). All three
  are live on one node. Identity is `(device, path-within-filesystem)` for local
  storage and `(nfs://server, server-side path)` for network — never the string.
- **`mountinfo` is the only acceptable mount source.** It carries the `root`
  field — the path *inside* the filesystem that a mount exposes — which is what
  makes bind mounts and nested datasets resolvable with no per-container
  config. `findmnt` and `/etc/mtab` do not have it.
- **Device numbers are global to a kernel**, so containers on one node resolve a
  shared dataset to the same volume for free. Verified: `dev://pve-a/0:40`
  observed from both the host and inside `lxc:114`.
- **A producer/consumer link is time-separated by nature.** A job writes at
  02:15 and something reads at 03:00; descriptor sampling will essentially never
  catch both ends in one window. A live sweep found zero shared artifacts across
  four containers, correctly — links come from observations persisted across
  sessions, not from a snapshot. That is why D5 stores at all.
- **Raw observations are never stored.** 48k in 30 seconds on one node. They are
  resolved, collapsed to a pattern, and aggregated into one counted row per
  `(namespace, actor, artifact, direction)` on write. 15,913 observations became
  394 rows in a live run — a 40x reduction, and what makes a thirteen-month
  window affordable rather than theoretical.
- **Group the blast radius by who breaks, not by which file.** The link join
  yields one row per writer×reader pair, so a file touched by six samba daemons
  renders as six identical `pve-a → pve-a` lines with the actor — the only
  distinguishing field — hidden. Caught by reading real output, not by a test.
- **"0.0 days of observation" reads as a bug, not a warning.** It is what two
  45-second sweeps produce. The coverage line is the honesty line; if it is
  unreadable the caveat gets ignored, so the window formats as minutes, hours or
  days and both the job coverage statement and the scream test use the same
  formatter — two renderings of one number is how a caveat gets discounted.
- **We repeated the estate's own stale documentation and had to correct it.**
  An earlier version of `DISCOVERY.md` described plex and rutorrent as binding
  `/alpha` and `/gamma`, because the estate's hand-kept inventory file says so. The live
  configs say both bind `/beta`. Read the machine, not the notes about it —
  including when the notes are ours.
- **Hand-maintained inventories drift, provably.** The estate's own
  hand-kept inventory file documents thirteen guests. `pct list` shows twelve
  on one node alone, including a running `pihole` that appears nowhere in the
  file. That drift is the entire reason this product exists, and it was visible
  within a minute of pointing the collector at a real node.


## From the first unattended deployment (2026-10-15)

- **The process snapshot had never worked.** It formatted with ``'{0}`t{1}'``,
  and PowerShell does not expand escapes inside single quotes, so no line ever
  split and the pid map was always empty. That, not only short-lived processes,
  is why the first capture's actors were all `pid:NNNN`. Fixed with a `|`
  separator (a path cannot contain one), and the ProcessStart events from
  `Microsoft-Windows-Kernel-Process` now name everything born mid-trace.
- **One trace session takes both providers.** `logman create trace` accepts one
  provider; `logman update trace <name> -p Microsoft-Windows-Kernel-Process 0x10
  -ets` adds the second to the running session. No provider file needed.
- **Protected processes have no Path, even to SYSTEM.** pid 4 (System),
  csrss, MsMpEng. Their `Name` is the fallback identity.
- **The collector observes itself.** The first report's scream test listed
  `python.exe` reading lineage's own source as a dependent. The collector's
  home directory is filtered before anything is stored.
- **A VM's wall clock can be hours wrong after a reboot.** Proxmox gives
  Windows guests a local-time RTC; a guest set to UTC with `w32time` stopped
  came up seven hours behind, and NTP later jumped it forward mid-window. That
  one window claimed 423 minutes for about four of watching. Windows are now
  measured by uptime (`time.monotonic()`, which is consistent across processes
  within a boot) and the wall clock only anchors the end.
- **Decode throughput is about 1,500 events a second** through `Get-WinEvent`
  plus the XML parse, on a 4-vCPU Win10 guest. An idle-ish guest just after
  boot produced ~19,000 file and process events a minute.
- **A full thin pool loses a guest's writes for days and nothing notices.** The
  Proxmox node hosting the first test VM had its LVM thin pool at 99.77%. The
  host kernel logged 247,847 `Buffer I/O error … lost async page write` lines
  for that VM's disk over ten days while the guest kept running; it finally
  bugchecked during a deploy and came up in Automatic Repair. This is the
  estate-knowledge gap the product exists for, one layer down: storage headroom
  belongs in the inventory (ROADMAP).
- **A passthrough GPU does not survive an in-guest reboot.** An RX 6700 XT
  (Navi 22) went to D3cold when its guest rebooted; `qm start` then fails with
  `failed to reset PCI device`, and a PCI remove/rescan does not bring it back.
  Only a host reboot does.
- **The cron interpreter was wrong for multi-run schedules with day rules.**
  `0 8-18/2 * * 1-5` reported a 14-hour max gap; Friday 18:00 to Monday 08:00 is
  2.6 days. Found by the conformance harness on its first run against croniter.
- **Proxmox guest-agent quirks** are in [proxmox-guest-agent.md](proxmox-guest-agent.md).
