# What lineage inherits, and what it deliberately does not

We already built a lot of this. Being precise about *which* parts saves
rebuilding them and, more importantly, stops us assuming a part exists that
does not.

## opsview — the closest relative

[opsview](https://github.com/sp00nznet/opsview) is fleet observability for
vCenter/Proxmox estates and standalone Linux hosts.

**Reused directly:**

- **The probe result contract.** `{probe, status, summary, data, issues}` plus a
  `_meta` block. lineage's report results map onto it 1:1 — see
  `server.py:Store.write_report`.
- **The state layout.** `state/<host>/<probe>/latest.json` + timestamped
  history capped at 50, in the same wrapper shape as
  `orchestrator/state.py`. lineage writes this exact tree, so **opsview's Flask
  console can render a lineage fleet with no new UI.** That is why lineage ships
  no web UI of its own.
- **The upgrade path for the server.** `lineage/server.py` is stdlib
  `http.server` because a scaffold does not need Flask. When it needs auth,
  users, groups and per-tab permissions, `opsview/web/api.py` is the template
  and it is already written.
- **Deployment shape.** `roles/opsview/` is the model for however lineage's
  server eventually gets installed.

**Not reused, and this is the important half:**

- **opsview has no agent.** It is *agentless push*: `orchestrator/runner.py`
  base64-encodes a probe script, SSHes to the target, and pipes it to
  `python3 -`. Nothing resides on the endpoint. So there is no agent to have
  tested at scale — there was never one to test.
- **It is Linux-only, by construction.** `runner.py:_probe_file` looks only in
  `probes/linux/`, and `_run_one` returns `status: skipped` for any host that
  is not Linux, with the literal note *"Windows probes deferred to Phase 7"*.
  The web API accepts `windows` as a host `os` value; nothing implements it.
- **Push is the opposite of lineage's model.** opsview's server decides what runs
  where and reaches out to do it. lineage publishes questions and lets endpoints
  self-select. That is the endpoint-suite insight (docs/design.md) and it is the entire reason lineage is
  a separate project rather than "opsview phase 7".

## whatdoesthisboxdo — the Windows knowledge

[whatdoesthisboxdo](https://github.com/sp00nznet/whatdoesthisboxdo) is a system
analysis tool: point it at a box, learn what it does, emit Terraform/Ansible/
Packer to recreate it. **It is the only sibling project that knows anything
about Windows**, and `analyzers/windows_analyzer.py` is 763 lines of it.

**Reused as content, not architecture.** It already answers "what do you collect
from a Windows box, and what is the PowerShell that gets it":

| Class | Collects |
|---|---|
| `WindowsProcessAnalyzer` | processes, services, connections, listening ports, resource usage |
| `WindowsFileAnalyzer` | installed software, Windows features, important paths, recently-modified files, scheduled tasks |
| `WindowsHistoryAnalyzer` | per-user shell history |
| `WindowsSecretsAnalyzer` | credential-shaped material |

That list is a ready-made specification for lineage's **deep inventory** — see
below — and the queries are already written and debugged.

`connectors/local_executor.py` is the closest thing among the siblings to on-box
execution: a `LocalExecutor` that runs commands on the current system, already
branching on `platform.system() == 'Windows'`. It is a useful starting point for
the agent's action executor in roadmap step 3.

**Not an agent, and not reusable as one.** `connectors/winrm_executor.py` is the
transport: the server opens a WinRM session and runs PowerShell remotely. It is
the same push, one-shot, shell-out-and-parse model as opsview, aimed at a
different OS. Nothing resides on the endpoint.

**And the collection method is exactly what lineage's inspectors must not do.**
Every collector here spawns PowerShell and parses `ConvertTo-Json`. For a
one-shot "tell me everything about this box" run, that is completely correct and
costs a few seconds. For a relevance evaluation on every endpoint every cycle,
it is the "the agent pegged the CPU" failure. Hence `service_state` being ctypes
into advapi32 rather than `Get-Service`, and hence `cim_value` being cached and
documented as a last resort.

**The right split, then:**

- **Relevance inspectors** (this repo) — cheap, native, every cycle, answer one
  question. Registry reads, file version, service state.
- **Deep inventory** (lift from here) — expensive, PowerShell, once a day or on
  demand, dump everything. Scheduled tasks, full software list, listening ports.

Those are different jobs with different cost budgets, and conflating them is how
the agent becomes the thing people uninstall.

## fleetview — parsers, not plumbing

[fleetview](https://github.com/sp00nznet/fleetview) collects cloud/hypervisor
inventory (AWS, GCP, Proxmox, VMware) and reproduces it as Terraform/Ansible.

- `fleetview/inspect/probes.py` is a clean set of **pure parsers** — raw command
  stdout in, typed models out, deliberately no I/O so they unit-test against
  fixtures. That separation is the right pattern and lineage's inspectors follow
  it (inspectors are injected as a table, never imported by the evaluator).
- The parsers themselves are `dpkg`, `rpm`, `systemctl`, `ss`, `docker ps` —
  all Linux. Nothing lifts over.
- `fleetview/models/` is worth reading before designing lineage's fleet-wide
  reporting schema; it has already thought about software inventory shape.

## The honest summary

Three projects, one architecture. opsview pushes Python over SSH to Linux.
whatdoesthisboxdo pushes PowerShell over WinRM to Windows. fleetview pulls from
cloud APIs and parses command output. **All of them are remote, push-based,
one-shot, and shell-out-and-parse.**

What exists and lifts:

- a probe result contract and a state format (opsview)
- a console that already reads that state (opsview)
- a deployment role (opsview)
- **what to collect from Windows, and the PowerShell that gets it**
  (whatdoesthisboxdo) — the single most valuable inheritance
- a local executor, and the pure-parser pattern (whatdoesthisboxdo, fleetview)

**What none of the siblings has: a resident agent, a continuously
self-evaluating endpoint, or a relevance engine.** Nothing has ever run *on* a
Windows machine and stayed there. That is why "we never tested our agents at
scale or for Windows" is true in a stricter sense than it sounds — there was no
agent to test, and the scale question has never been asked because push-based
tools fail visibly at the console instead of quietly on ten thousand endpoints.

Those are lineage's actual work.
