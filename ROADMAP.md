# Roadmap

Sequenced so every step is a shippable product and stopping early is survivable.
The trap is building the console first, because the console demos well.

**Read-only first.** Everything through D5 needs no signed content, no mutual
auth and no action executor, so the discovery track ships while the security
prerequisite for actions is still being built, rather than behind it. See
[`docs/DISCOVERY.md`](docs/DISCOVERY.md).

## Now: accumulate history

D2 and D5 are worth exactly as much as the history behind them: a month minimum
for an end-of-month comparison, a year for an annual window. Every week not
collecting is a week of answer that cannot be recovered later. Every other piece
of work costs the same whenever it is done.

- [x] **Actor names** from `Microsoft-Windows-Kernel-Process`, plus a working
      process snapshot (the old one never parsed; see docs/findings.md).
- [x] **`lineage.collect`**: D1–D5 in one read-only cycle, an hourly SYSTEM task,
      a rolling ETW session so the gaps are seconds, not 55 minutes an hour.
- [x] **Running unattended on a Windows guest** since 2026-10-15.
- [ ] **A second host, and a cross-host link.** A Proxmox node (the `/proc` tap,
      cron schedule) plus a Windows machine mapping one of its exports. A Windows
      write and a Linux read of the same bytes, through two taps, unified by the
      host-alias table. If that link forms unaided, D3→D4→D5 is proven end to end.
- [ ] **Re-add the GPU to the first test VM** after its host reboots (its
      passthrough card is in D3cold; see docs/running.md).
- [ ] **Decode throughput** at an hourly cycle on a busy machine. ~1,500 events/s
      through `Get-WinEvent` is fine for a quiet guest; a workstation may need a
      native ETL reader.

## Steps

| # | Step | Ships as | State |
|---|---|---|---|
| 1 | Agent + relevance evaluation + report-in. **No actions.** | asset inventory | 🟡 in progress |
| 1b | Deep inventory: daily sweep, lifted from whatdoesthisboxdo | fills out step 1's asset data | ⬜ not started |
| 1c | Linux inspector table (fleetview's parsers lift directly) | the biggest server population | ⬜ not started |
| D1 | Job inventory: cron, Task Scheduler, with **declared frequency** | "every scheduled job in the estate" | 🟡 [`jobs.py`](lineage/jobs.py), scored by the conformance harness; systemd timers pending |
| D2 | Periodic snapshots + **seasonal baseline diff** | catches the removed-mount incident with one month of history | 🟡 [`seasons.py`](lineage/seasons.py); daily snapshots written by `lineage.collect` |
| D3 | Observation collector: ETW, `/proc` sampling, then fanotify | feeds the graph | 🟡 [`etw.py`](lineage/etw.py) running unattended with process names; [`observe.py`](lineage/observe.py) verified on a Proxmox node; fanotify pending |
| D4 | Path canonicalisation + mount join | the make-or-break piece | 🟡 [`paths.py`](lineage/paths.py), resolver + export bridge against real fixtures |
| D5 | Dependency graph + **scream test** | dependency mapping | 🟡 [`graph.py`](lineage/graph.py), persisted store, idempotent central merge |
| 2 | Signed policy + mutual auth | prerequisite, blocks every *action* below | ⬜ not started |
| 3 | Actions: one package to one targeted group, audited | software deployment | ⬜ not started |
| 4 | Patch policy over winget/Chocolatey: rings, deferrals, windows | patch management | ⬜ not started |
| 5 | Self-service portal | user-requested installs | ⬜ not started |
| 6 | Compliance baselines + remediation | same engine, a report and an action | ⬜ not started |
| 7 | OS deployment / task sequences (PXE, WinPE, DISM) | OS deployment | ⬜ not started |

**The falsifiable test:** if steps 1 and 3 (an agent that self-evaluates, and
one package deployed to a targeted group) do not work end to end within about a
month of evenings, the scope is wrong and it needs cutting, not more time.

**Why discovery jumped the queue.** It is read-only, so it skips the entire
signing/auth prerequisite that gates actions, which means putting it first *cuts*
scope rather than adding it. It is also the sharper product: a free patch tool
competes with every patch tool and config manager, while "what breaks if I turn this off,
across Windows and Linux" competes with a whiteboard. And D2 alone addresses a
real recurring incident (a mount removed in a quiet month, discovered at
end-of-month when 20 GB of CSVs arrive) with one month of history and no graph.

### Step 1 detail

- [x] Relevance engine: parse, validate, evaluate, budget
- [x] `Version` comparison that is numeric, not lexical
- [x] Windows inspector set (registry, files, services, identity, packages, CIM)
- [x] Agent cycle: fetch policy → evaluate → report, with offline fallback
- [x] Server: publish policy, accept reports, write opsview-shaped state
- [x] End-to-end on real hardware
- [ ] Run as a Windows service (the agent is a foreground loop; `lineage.collect`
      already runs as a scheduled task)
- [ ] **Transactional self-update with rollback**: the highest-risk operation in
      the product; an agent that can brick itself bricks the fleet
- [ ] Watchdog + hard CPU/memory ceiling on evaluation
- [ ] Packaging: single signed binary. Today the guest gets the embeddable
      CPython (pinned by hash) and the source tree.
- [ ] Scale test, which has never been done here (see `docs/HERITAGE.md`)

## Deferred, with the reason

- **Storage headroom as inventory.** A thin pool at 99.77% silently dropped a
  guest's writes for ten days (docs/findings.md). Pool fill and overcommit ratio
  belong in the host inventory, once there is a Linux inspector table (1c).
- **`observed_days` counts the span from first to last window**, not the sum of
  windows, so a host that was off for a week reads as watched for it. Harmless
  while collection is continuous; needs fixing before anyone reads a scream test
  from a host with gaps. `report` already prints both numbers.
- **Automatic fleet pulls.** `tools/pve.py pull` is run by hand. Collection,
  the part with the deadline, is unattended; reading it is not urgent.
- **More conformance corpora.** Task Scheduler XML against `schtasks /query`'s own
  next-run times, and mountinfo against `findmnt`, are the obvious next oracles.

## Out of scope

- A kernel driver. ETW and fanotify cover it from user mode; see docs/design.md.
- Re-implementing distribution points or a software catalog (winget/Chocolatey
  own the catalog; BranchCache and Delivery Optimization own distribution).
