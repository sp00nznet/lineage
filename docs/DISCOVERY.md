# Discovery: dependency observation across a mixed estate

Design notes for the read-only half — what depends on what, and what breaks if
you turn it off. Sequenced *before* actions, because it needs none of the
signing, mutual auth or action-executor work that blocks the rest.

---

## The estate this is actually for

| Population | Count | Wants |
|---|---|---|
| Windows laptops | hundreds | inventory, patch, compliance |
| Windows servers | dozens | inventory + **discovery** |
| Linux servers | hundreds | **discovery**, mostly |
| macOS | a handful | inventory, eventually |

Two conclusions fall straight out of that table.

**The Linux server fleet is the biggest population that matters here**, so
"Windows fleet manager" was the wrong framing from the start. What is
Windows-specific is the *inspector table*, not the design: the relevance engine
never imports an inspector, it takes a table. A Linux table is a separate file,
not a fork — and fleetview's `inspect/probes.py` already has debugged parsers
for `dpkg`, `rpm`, `systemctl`, `ss` and `ps` to fill it with.

**A laptop and a job-running server want completely different things from the
same agent**, and that difference is already expressible: it is a policy, not a
build. Laptops get inventory and patch items; servers get discovery items. Same
binary, same engine, different questions. macOS gets a reduced inspector set
when someone actually needs it — a handful of machines does not justify a third
platform's worth of native code yet.

---

## The problem with observation windows

The failure mode, restated with the estate's real calendar:

> End-of-month processing runs in the last week of the month. Annual processing
> runs around tax season. A client uploads 20 GB of CSVs and *that* is when we
> discover a mount someone removed three months ago was load-bearing.

A tool that observes for two weeks and reports "no activity on this path" is not
merely unhelpful here — it would have **endorsed** removing the mount. Being
wrong in that direction is worse than not having the tool.

Waiting out the calendar is not an answer either. Honest coverage of an annual
job means a thirteen-month observation window, and nobody deploys a tool that is
useless for a year.

### So confidence comes from three sources, not one

| Source | What it gives | Available |
|---|---|---|
| **Observed edges** | Ground truth. A process really did read that file. | Immediately, bounded by the window |
| **Declared schedules** | *Intent.* `0 2 1 * *` is monthly whether or not you have seen it fire. | **Day one** |
| **Seasonal baselines** | Same-phase comparison — this period vs. the same period last month/year | After 2+ cycles |

**The declared-schedule source is what makes this usable in week one**, and it is
the cheapest of the three. Cron expressions, Task Scheduler triggers and systemd
timers all state their frequency in machine-readable form. A job declared
monthly that has not been observed is not an unknown — it is a *known job with a
known frequency and no observations yet*, which is a completely different claim
and should be displayed as one.

That single distinction closes most of the EOM gap without waiting for an EOM.

### What the UI must show, always

Not a verdict. A coverage statement:

```
Observed 9 days (since 2026-07-31).
  118 jobs declared across the estate
   86  observed running            ← ground truth
   27  declared monthly or less    ← 23% of the estate is invisible at this window
    5  declared, never observed, no schedule found   ← the honest unknown bucket
```

**"No activity observed in the 9 days watched"** — never "unused". The window is
configurable and the confidence is always computed against the *actual* window,
not a default.

---

## Seasonal baselines — and the cheap win hiding in them

Once there are two same-phase periods to compare, the highest-value query is not
the graph at all. It is the diff:

> **This mount was present during last month's EOM window and is absent now.**

That is the incident, caught in advance, and it needs **no dependency graph
whatsoever** — only periodic snapshots and a calendar-aware comparison. opsview
already has the machinery: blessed baselines and a drift view. This is
*baselines with a calendar* — compare against the same phase of the previous
cycle rather than against one frozen snapshot.

**Ship this before the graph.** It is a fraction of the work, it addresses the
exact failure that prompted the requirement, and it is useful with one month of
history instead of a year.

Phases worth tracking, because they are where this estate's risk lives:

- last N days of the month (EOM)
- first N days of the month
- quarter boundaries
- a configurable annual window (tax season)

Alert shape: *"14 things changed since the equivalent window last cycle — 3 of
them were touched by EOM jobs."* Ranked by whether a declared job depends on
them, not by raw change count.

---

## The scream test button

Named after the real practice: turn it off and see who screams. The point of the
button is that **nobody should have to actually do that.**

It does not turn anything off. It previews the blast radius, in three tiers that
are never merged, because merging them is how a confident tool causes an outage:

```
  Scream test — srv-legacy-04

  CONFIRMED   observed dependencies                          4
    • nightly-export (srv-01)      reads  \\fs01\data\out\*.csv    last seen 6h ago
    • billing-load   (srv-04)      reads  \\fs01\data\out\*.csv    last seen 6h ago
    …

  DECLARED    scheduled jobs referencing this host or its paths,
              not yet observed in the current window            3
    • month-end-close (srv-07)     cron 0 2 28-31 * *    monthly — 0 observations
    • annual-extract  (srv-07)     cron 0 3 1 4 *        yearly  — 0 observations

  SEASONAL    activity in the same phase of previous cycles     ⚠
    • During the last week of each of the last 3 months this host
      handled 340x its current traffic. You are looking at it in a quiet phase.

  UNKNOWN     jobs we cannot attribute to any host              5
```

Three rules for this screen:

- **The seasonal warning outranks everything else on the page.** A quiet box in
  the third week of the month is the exact shape of the mistake being prevented.
- **`UNKNOWN` is never hidden and never zero-by-omission.** A tool that shows
  only what it knows reads as complete. The unknown count is what stops that.
- **No overall verdict.** No "safe to decommission" badge, no traffic light. The
  tool presents evidence and coverage; a person decides. The moment it renders a
  verdict, people stop reading the tiers — and the tiers are the product.

Optionally, later: a **soft scream test** — firewall or deny-ACL the resource
rather than removing it, watch for failures, revert automatically. That is a
real action with a real blast radius and belongs after the action path exists,
with its own audit trail. Not now.

---

## Sequencing

Read-only throughout, so none of this is blocked on signing or auth.

| # | Step | Useful on its own? |
|---|---|---|
| D1 | Job inventory — cron, Task Scheduler, systemd timers, with **declared frequency** | Yes: "every scheduled job in the estate" is more than most sites have |
| D2 | Periodic snapshots + **seasonal baseline diff** | Yes: catches the removed-mount class of incident with one month of history |
| D3 | Observation collector — Sysmon/ETW on Windows, `/proc` sampling then fanotify on Linux | Feeds everything below |
| D4 | Path canonicalisation + the mount join (opsview's `mounts`) | The make-or-break piece |
| D5 | The graph + scream test | The reason anyone installs it |

---

## Where the tap runs — settled by the estate, not by preference

**Kernel-level file observation does not work inside an unprivileged LXC
container.** Audit netlink is namespaced and blocked; eBPF generally cannot
load. Since most guests in the target estate are LXC, the tap runs on the
**hypervisor host** and attributes activity back to guests through the cgroup
path (`container_of()` in `observe.py`).

That is a better deployment story than it sounds — two agents on two Proxmox
nodes instead of sixteen inside guests — at the cost of one attribution step.

Verified on a live Proxmox 9.1 node: 48,058 observations in 30 seconds across
four running containers, each correctly attributed to its VMID.

### Two things only real hardware taught us

**1. `lxc-monitord.service` is not a container.** The host runs a systemd unit
by that name, and a cgroup pattern that accepts `lxc-` as a separator invents a
container called "monitord" from it. The separator after `lxc` must be `.` or
`/`. Proxmox writes all of `/lxc/114/`, `/lxc.payload.114/` and
`/lxc.monitor.114/`, so matching only the first form silently attributes half a
node's containers to the node itself.

**2. A path string tells you nothing, in either direction.**

The first version of this section was wrong, and how it was wrong is the point.
It described plex as binding `/alpha` and rutorrent as binding `/gamma`,
because that is what the estate's hand-kept inventory file says. The live container configs
say both bind `/beta`. **The documentation had drifted, and this document
repeated it** — in a project whose entire thesis is that documentation drifts.
Read the machine, not the notes about the machine.

Here is what is actually true on the node, from `/proc/<pid>/mountinfo`:

| Reference | Resolves to | |
|---|---|---|
| `plex:/mnt/store/x` | `dev://pve-a/0:40` `/x` | same string, |
| `rutorrent:/mnt/store/x` | `dev://pve-a/0:40` `/x` | **same file** |
| `gitlab:/mnt/releases/y` | `dev://pve-a/0:43` `/y` | different string, |
| `host:/beta/releases/y` | `dev://pve-a/0:43` `/y` | **same file** |
| `plex:/mnt/store/releases/y` | `dev://pve-a/0:40` `/releases/y` | same-ish string, |
| `host:/beta/releases/y` | `dev://pve-a/0:43` `/y` | **different file** |

That last row is the subtle one: `/beta/releases` is its own ZFS dataset and is
*not* propagated into plex's mount namespace, so inside plex that path is the
empty underlying directory of `beta`. All three cases fall out of one model
with no per-container configuration — see below.

**Both failure directions are live here**, which is why the resolver is not an
optimisation. Missing a link loses a real dependency; fabricating one makes the
scream test cry wolf until people stop reading it.

### The model that handles all of it

Identity is `(volume, path-within-volume)`:

- **Local** → `dev://<host>/<major:minor>` + the path inside that filesystem.
  Device numbers are global to a kernel, so every container on a node resolves
  a shared dataset to the same volume *automatically*. Bind mounts and nested
  datasets are handled by `mountinfo`'s `root` field, which is why that file is
  the only acceptable source — `findmnt` and `/etc/mtab` do not carry it.
- **Network** → `nfs://<server>` or `smb://<server>/<share>` + the path *as the
  server sees it*. This is the only identity that crosses machines.
- **The bridge**: a local path under an NFS export gains a second key in that
  server's `nfs://` namespace. That is what lets "the server wrote it" meet
  "the client read it" without either side knowing about the other.

Verified live: `dev://pve-a/0:40` was observed from both the host and inside
`lxc:114`, and plex's `/mnt/store/TV Shows/…` reduced correctly to host storage
identity.

**Known limitation, recorded rather than papered over:** the bridge only fires
when both sides name the server identically. A client mounting
`192.0.2.23:/beta` does not currently unify with a server that calls itself
`pve-a`. Host-identity reconciliation — IP ↔ hostname ↔ FQDN — is its own
problem and is not solved yet.

### And a limitation of sampling, demonstrated

A single sweep found **zero shared artifacts** across four running containers,
and that is the correct answer: same volume, different files. Nobody had the
same file open during a twelve-second window.

Which makes the real point — **a producer/consumer link is time-separated by
nature.** A nightly job writes at 02:15; something reads at 03:00. Descriptor
sampling will essentially never catch both ends in one window. Links come from
observations *accumulated and persisted across sessions*, not from a snapshot.
That is D5's job, and it is another reason long windows are the product rather
than a caveat on it.

D1 and D2 are the ones to build first, and they are small. D2 in particular
addresses the originating incident without needing D3–D5 to exist at all.
