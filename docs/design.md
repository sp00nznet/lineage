# Design

Why lineage is shaped the way it is. The README says what it does today; this
file holds the reasoning that outlives any one release. Moved here from the
README on 2026-10-15, unchanged apart from links.

## What it's for

Windows fleet management is a per-seat racket at every price point. The
endpoint management suites are enterprise-priced; the smaller patching tools
went to ~$1,500/yr; the cloud services are per-user and need a tenant. Meanwhile the free on-prem path
is *shrinking* — WSUS was deprecated in 2024, and its replacements all require a
tenant.

The gap, in one sentence:

> A free, on-prem fleet manager for Windows that needs no cloud tenant, no SQL
> Server, no Linux control node, and no per-seat licence.

## The goal

**One relevance engine, four products.**

The idea worth taking from the big endpoint suites is not their feature list — it is that every piece of
content carries a declarative expression evaluated **on the endpoint**,
continuously, by the agent. The server never polls and never holds a model of
what is installed where. It publishes *"here is a thing, here is when it
applies"* and endpoints self-select.

That single decision means these are not four subsystems that sit near each
other. They are one file:

| Question | Result | Product |
|---|---|---|
| Does this patch apply? | boolean | patch deployment |
| Is this machine compliant? | boolean | compliance baselines |
| Should this policy target it? | boolean | targeting |
| May this user install this? | boolean | self-service portal |
| What version is installed? | value | inventory |

That is [`lineage/relevance.py`](../lineage/relevance.py) — about 300 lines. Nothing
else in the design has that leverage ratio, and it is the whole reason this
project is worth its size.

### The loop: what it discovers, it manages

The two halves are not adjacent features, they feed each other. Drop the agent
into an estate nobody documented, let it observe, and **the findings become
policy in the same language they were discovered in.**

```
  observe  →  "srv-04 runs the month-end close, 0 2 28-31 * *"
                  ↓
  targeting →  is_eom_critical()          ← a relevance expression
                  ↓
  policy    →  never patch this host in the last week of the month
               never reboot it without an approval
               alert if its mounts drift from the last EOM baseline
```

Same engine, closed loop, no integration. That is the answer to *"this is the
tool that installs Notepad++ and Java for your users, and it is also the tool
that tells you the SQL Server nobody owns is a ticking time bomb because
accounting's annual bonus reports land there."*

It is also why the discovery half runs first: **an MSP taking over an unfamiliar
estate cannot safely manage what it has not yet mapped.** Discovery is the
onboarding step, and management is what it earns.

The design consequence, so it does not get lost: **discovered facts must be
expressible as relevance inspectors.** `is_eom_critical()`, `depends_on(path)`,
`observed_since()` are the shape. If a discovery output cannot be phrased as
something policy can target, it is a report — useful, but outside the loop.

## The design rules that aren't negotiable

- **The agent takes no actions until the protocol is signed.** Step 1 answers
  questions and nothing else.
- **Fail safe, not fail closed.** No server contact means keep evaluating the
  cached policy. It never means stop.
- **A check that could not run is an error, not a finding.** `value: false` and
  `error: "…"` are different facts. Collapsing them is how a fleet quietly stops
  patching while every dashboard stays green. (This is not theoretical — see
  *Findings* below.) The same rule applies to discovery: never say "unused",
  only "no activity in the N days observed", and never invent a period for a
  schedule that does not declare one.
- **The relevance language is deliberately not Turing complete.** No loops, no
  assignment, no attribute access, no indexing; bounded by node count, inspector
  calls and wall clock. That is what makes it safe to run every 60 seconds on
  every endpoint and what limits the blast radius of a compromised publisher.
- **Catalog is payroll; policy is code.** Maintaining detect+install logic for
  thousands of apps is a permanent staffed job and it is most of what the paid
  products actually sell. Delegate the catalog to winget/Chocolatey. Build only
  the policy: rings, deferrals, maintenance windows, reporting.
- **Do not reimplement distribution points.** BranchCache and Delivery
  Optimization are in the box.

## What's reused, and what was never built

We have more of this than it looks, and less than it feels.
[`docs/HERITAGE.md`](HERITAGE.md) is precise about both.

- **opsview** contributes the probe result contract, the state layout (lineage
  writes opsview's exact tree, so **opsview's console renders a lineage fleet
  with no new UI**), and the Flask upgrade path for the server.
- **whatdoesthisboxdo** contributes the Windows knowledge — 763 lines of it. Its
  `windows_analyzer.py` already answers *what do you collect from a Windows box
  and what is the PowerShell that gets it*: processes, services, connections,
  installed software, features, scheduled tasks, recently-modified paths. That
  is a ready-made spec for **deep inventory**, and its `LocalExecutor` is the
  starting point for the action executor in step 3.
- **fleetview** contributes the pure-parser pattern and a software inventory
  schema worth reading. Its parsers are `dpkg`/`rpm`/`systemctl` — Linux.

**Three projects, one architecture: remote, push-based, one-shot,
shell-out-and-parse.** opsview pushes Python over SSH to Linux;
whatdoesthisboxdo pushes PowerShell over WinRM to Windows; fleetview pulls from
cloud APIs. None of them leaves anything on the endpoint. So there was no agent
to have tested at scale — and the scale question has never been asked, because
push-based tools fail visibly at the console rather than quietly across ten
thousand endpoints.

**Two collection budgets, kept apart.** Relevance inspectors are cheap, native
and run every cycle — that is why `service_state` is ctypes into advapi32 and
not `Get-Service`. Deep inventory is expensive, PowerShell, and runs once a day
— that is where whatdoesthisboxdo's collectors lift in wholesale. Conflating
them is how the agent becomes the thing people uninstall.

Net new work: the resident agent, the relevance engine, and native inspectors.

**And residency is not just convenience.** The estate question nobody can answer
— *what does that twenty-year-old script actually do, and what breaks if I turn
its box off* — needs `(process, file, direction, time)` observed as it happens.
You cannot poll for "who wrote this file at 03:00"; by the time you ask, the
process has exited. That capability is structurally out of reach for every
push-based tool, and it is the strongest argument for the agent
existing at all. It ships as its own product on this agent; see
[DISCOVERY.md](DISCOVERY.md).

## Two people, one seam

The split is unusually clean, which is most of why this is worth doing as a
pair rather than alone:

- **Agent** — service lifecycle, evaluation loop, self-update, inspectors.
- **Server + console** — publishing, targeting, reporting, signing, web UI.

They meet at [`docs/api.md`](api.md). **Freeze that first.** It is
the only real coupling and it is far cheaper to get right on day one than on day
ninety.

## Deliberate omissions

`ponytail:` comments in the source mark shortcuts with their ceiling and upgrade
path. The load-bearing ones:

- **Relevance syntax is a whitelisted subset of Python expressions**, parsed by
  `ast.parse`, so there is no hand-written lexer or parser to get wrong. We walk
  the tree with our own evaluator — the whitelist is a filter, the evaluator is
  the boundary. Write a real parser only if the syntax needs to diverge.
- **`server.py` is stdlib `http.server`**, no Flask, no database. Swap in
  opsview's `web/api.py` when it needs auth and a console.
- **No authentication anywhere yet.** Binds to loopback. See docs/api.md; this
  blocks roadmap step 3.
- **`service_state` is ctypes into advapi32, not `sc query`.** A process spawn
  per service check per cycle per endpoint is exactly the "the agent pegged the
  CPU" failure this project cannot afford.

## Prior art, honestly

The open-source configuration managers are free and good, and they are
push-based from a control node, usually Linux — no OS deployment, no
self-service, no continuous endpoint self-evaluation. The open-source RMMs are
monitoring and remote access first. The cloud endpoint services are the real
competition, and they are per-user and need a tenant. **If the pitch ever stops being "one signed .exe, on-prem, no
tenant, no per-seat cost", this project has lost its reason to exist** — and the
right response is to say so, not to build a worse config manager.

