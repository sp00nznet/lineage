# Agent self-update

**The highest-risk operation in the product.** Everything else the agent does
affects one machine. This affects every machine, at once, with no way back in if
it goes wrong — the agent is the remote hands.

Design it before adding features, because retrofitting a safe update path onto a
deployed fleet means visiting the machines.

---

## The failure we are designing against

Not "the update fails". A failed update that leaves the old agent running is a
non-event. The one that matters:

> A new version is published. It starts, crashes, gets restarted, crashes again.
> Ten thousand endpoints are now running a crash loop, reporting nothing, and
> the only tool that could push a fix is the thing that is broken.

And its sibling, which is worse because it looks like it is working:

> The bad version rolls back correctly. The server still says "update to 1.4.3".
> The agent updates, crashes, rolls back, updates again — forever.

**The rollback is the easy half. Not re-applying the version you just rejected
is the half that saves the fleet.** See [Refusal](#refusal-the-part-that-matters).

---

## Shape: supervisor + worker

The Service Control Manager starts a **supervisor**. The supervisor runs the
**worker** as a child process. The worker does all the actual work — policy
evaluation, reporting, and later, actions.

```
  SCM
   └── lineage-svc.exe          supervisor    stable, tiny, boring
        └── versions\1.4.2\lineage-agent.exe   worker    everything else
```

This one structure pays for three separate requirements, which is why it is
worth the extra process:

| Requirement | How the supervisor covers it |
|---|---|
| **Self-update** | A running EXE is locked on Windows and cannot replace itself. The supervisor is not the file being replaced, so the problem disappears. |
| **Watchdog** | The supervisor already owns the child handle. Dead child → restart, with backoff. |
| **CPU / memory ceiling** | Job object on the child. A runaway evaluation gets killed, not tolerated. |

Rules that keep this safe:

- **The supervisor never self-updates.** It is replaced only by the installer
  (MSI), as a deliberate, ceremonious act through the normal package channel.
  Keep it small enough that it never needs to change: start a child, watch it,
  read a pointer, flip a pointer. No HTTP client, no parser, no policy logic.
- **The supervisor is the only thing that can activate or roll back a version.**
  The worker asks; the supervisor decides and acts.
- **Versions are side-by-side, never in place.** Unpacking never touches the
  running version's files.
- **Activation is one atomic pointer write** (a registry value). Everything
  before it is reversible by deleting a directory; nothing after it is needed.

## Layout

```
C:\Program Files\lineage\
  lineage-svc.exe                    supervisor  (installer-managed only)
  versions\
    1.0.0\                          golden — shipped by the installer, never pruned
    1.4.1\                          previous — kept for rollback
    1.4.2\                          current
  staging\<version>\                unpacked here, verified, then activated
C:\ProgramData\lineage\
  update-state.json                 current / golden / probation / refused
  agent-id                          survives everything
```

State lives outside `versions\` so pruning a version never takes data with it.

`golden` is the floor: if the supervisor cannot start *any* version, it starts
golden. There is always something that ran once on this machine.

## The update sequence

Each step is reversible until the last one, and the last one is a single write.

1. **Offer.** Policy names a version, a URL, a size, a SHA-256, and a signature.
2. **Gate.** Refused? Not newer? Not in our ring yet? Stop. Cheap, local, no
   download.
3. **Jittered wait.** A delay derived from `agent_id`, not from a random number
   — so the delay is stable across restarts and the fleet does not stampede the
   server the second a version is published.
4. **Download** to `staging\`, size-capped, resumable, with backoff.
5. **Verify.** SHA-256 against the manifest, signature against a **pinned public
   key compiled into the supervisor**, and Authenticode on the binaries.
   Verification failures are hard stops that never activate anything.
6. **Unpack** into `staging\<version>\`, then move into `versions\<version>\`.
   Still not running. Still reversible by deleting a directory.
7. **Activate**: write the pointer, restart the child. **This is the atomic
   moment.** Power loss before it changes nothing; after it, the new version
   runs and probation begins.
8. **Probation.** See below.
9. **Promote or roll back.**

## Probation — proving the new version works

Activation is not success. A version is on probation until it demonstrates it
can do the job:

- **It stays alive.** The supervisor counts child restarts. Any crash during
  probation is an immediate rollback, not a strike.
- **It completes whole cycles.** Three successful policy evaluations.
- **It can still talk.** *If the previous version was reporting successfully*,
  the new one must report at least once. A build that evaluates perfectly and
  broke its TLS stack is a bricked agent that looks healthy from the inside.
  This condition is skipped for machines that were already offline, because
  "fail safe, not fail closed" means an offline machine is not a failing one.
- **Deadline: 30 minutes.** Not enough evidence by then → roll back.

Rolling back is: write the pointer to the previous version, restart the child.
Same atomic operation, in reverse.

## Refusal — the part that matters

A version that failed probation is written to `refused` in
`update-state.json`, on disk, **before** the rollback completes. It is never
offered to that machine again.

Two failure kinds, deliberately treated differently:

| Failure | Meaning | Response |
|---|---|---|
| **Staging** — download, hash, signature, unpack, disk full | The machine or the network had a bad day | Retry with backoff. **Do not refuse.** A good version must not be permanently rejected because a disk was full once. |
| **Probation** — it ran and misbehaved | This build does not work *on this machine* | **Refuse permanently.** It will do the same thing next time. |

Refusal is cleared only by an operator, from the console, deliberately.

**Refusals are reported.** A console showing *"1,847 endpoints refused 1.4.3"*
is the difference between a caught bad build and a silent fleet-wide outage. The
refusal count is the single most important number in the update pipeline, and it
is the signal the server uses to stop publishing.

## Staged rollout, using the engine we already have

Ring assignment is a relevance expression — no new mechanism:

```json
{ "id": "update-1.4.3", "kind": "applicability", "relevance": "update_ring() < 5" }
```

`update_ring()` is a stable 0–99 bucket derived from `agent_id`. Stable matters:
a machine must land in the same ring every time, or "canary" means nothing.

Suggested progression, gated on the refusal count at each step: **1% → 5% → 25%
→ 100%**, with the server halting automatically past a refusal threshold.

## What is deliberately not built

| Not doing | Why |
|---|---|
| **Supervisor self-update** | The one component that must never break. Installer-only, forever. |
| **Delta / binary patching** | The agent is small. Bandwidth is not the problem; correctness is. Delivery Optimization exists if it ever becomes one. |
| **Downgrade on server command** | Rollback is a *local* decision made from local evidence. A server-driven downgrade is an attack shape, and the pinned-signature check plus the monotonic version rule closes it. |
| **Auto-retry of a probation-failed version** | It crashed here once. It will crash here again. |
| **Updating while an action is in flight** | Once actions exist (step 3), finish or abandon them first. Not a concern at step 1, which changes nothing. |

## Status

- [x] **Decision logic** — `lineage/update.py`. The state machine, refusal
      bookkeeping, probation accounting and gating. Pure functions over a JSON
      state file, no processes, no I/O beyond load/save. This is the part where
      being wrong bricks a fleet, so it is the part that exists first and has
      tests.
- [x] `update_ring()` inspector for staged rollout
- [ ] Supervisor process (`lineage-svc.exe`): child lifecycle, job object, pointer read
- [ ] Download + SHA-256 + signature verification against a pinned key
- [ ] Unpack and prune
- [ ] Windows service registration and installer
- [ ] **The test that matters: kill the worker mid-activation, at every step,
      and confirm the machine still comes up managed.**
