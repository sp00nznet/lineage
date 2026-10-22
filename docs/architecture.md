# Architecture

Two products share one package. **Management** is an agent that evaluates
relevance expressions and a server that publishes them. **Discovery** is a
collector that watches file activity and remembers it. Both are read-only today.

```
  management                                   discovery
  ──────────                                   ─────────
  lineage.server  ── GET /v1/policy ──►        lineage.collect run   (hourly, SYSTEM / root)
   var/policy.json                 │             │  taps:  etw.py      (Windows: Kernel-File + Kernel-Process)
   var/state/<host>/<item>/...     │             │         observe.py  (Linux: /proc fd sampling)
        ▲                          ▼             │  D4:    paths.py    (path -> storage identity)
        └── POST /v1/report ── lineage.agent     │  D5:    graph.py    (aggregate into graph.db)
                                 relevance.py    │  D1/D2: jobs.py + daily snapshot (seasons.py reads it)
                                 inspectors.py   ▼
                                               <home>/graph.db, snapshots/, etw/, collect.log
                                                 │
                                    tools/pve.py pull  (sqlite backup on the guest, copy, merge)
                                                 ▼
                                               var/fleet/central.db ── lineage.collect report
                                                                     └─ lineage.web  (read-only console, :8771)
```

## The parts

| Part | Owns | Process |
|---|---|---|
| `relevance.py` | the expression language: parse, whitelist, evaluate, budget | library |
| `inspectors.py` | the native facts an expression can ask for (registry, files, services, CIM, packages) | library |
| `agent.py` | fetch policy, evaluate, report; offline fallback to cached policy | `python -m lineage.agent` |
| `server.py` | publish `var/policy.json`, accept reports, write opsview-shaped state | `python -m lineage.server` |
| `update.py` | self-update decision logic (no plumbing yet; see SELF-UPDATE.md) | library |
| `jobs.py` | D1: every scheduled job and its declared max gap | library |
| `seasons.py` | D2: same-phase snapshot diff, ranked by job exposure | library |
| `etw.py` | D3 Windows tap: trace session, decoder, correlator | library + `tools/etw_capture.py` |
| `observe.py` | D3 record shape, path patterns, noise filter, `/proc` tap | library |
| `paths.py` | D4: mountinfo, exports, NT device paths, UNC, storage identity | library |
| `graph.py` | D5: the store, links across time, host aliases, scream test | library |
| `collect.py` | the unattended cycle wiring D1–D5, plus `merge` and `report` | `python -m lineage.collect` |
| `web.py` + `web.html` | read-only console over `var/fleet`: coverage, actors, scream test, files, jobs, links | `python -m lineage.web` |
| `tools/pve.py` | deploy to and pull from Windows guests through the Proxmox guest agent | workstation tool |
| `tools/conformance.py` | scores `jobs.py` against croniter | CI |
| `tools/demo.py` | builds a made-up two-host fleet for the console and its screenshots | by hand, tested in `test_web.py` |

## Data flow in one discovery cycle

1. **Windows:** stop the running ETW session, rename its file, start the next
   session immediately, snapshot pid→image. Only then decode the finished file,
   so the unwatched gap is the stop→start, not the decode. See `collect.py`.
2. Decode with `Get-WinEvent` (in-box, no TraceEvent), translate NT device
   paths to DOS paths, correlate Create/Write/NameCreate into observations, and
   name actors from the snapshot plus ProcessStart events (`etw.py`).
3. Drop the collector's own home directory, then resolve each path to a storage
   identity: `smb://server/share` for UNC and mapped drives, `dev://host/c:` for
   local volumes, `nfs://`/`dev://` from mountinfo on Linux (`paths.py`).
4. Aggregate into one counted row per `(host, container, actor, volume, path
   pattern, direction)` and record the window actually watched, measured by
   uptime so a wall-clock jump cannot inflate it (`graph.py`).
5. Write the day's snapshot: every scheduled job with its declared frequency,
   and every mount, in agent-report shape so `seasons.py` reads it unchanged.

## Why the pieces are where they are

- **The tap runs on the host, not in containers.** Audit netlink and eBPF are
  unavailable inside unprivileged LXC; a host tap attributes back by cgroup.
- **Every store is per-host and cumulative; the central store replaces by host.**
  A pull can be repeated any number of times without double counting
  (`graph.merge_store`).
- **The collector is a scheduled task, not a service.** A task restarts at boot,
  has an execution limit and `IgnoreNew` for overlap, and needs no service
  wrapper or signed binary. The agent's service mode is still roadmap step 1.

Design reasoning that is not about structure is in [design.md](design.md); the
wire protocol is [api.md](api.md); the store is [data-model.md](data-model.md).
