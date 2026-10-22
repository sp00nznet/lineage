# lineage wire protocol — v1

**Freeze this before writing code on either side of it.** It is the only real
coupling between the agent and the server, it is the seam the two-person split
runs along, and it is far cheaper to get right on day one than on day ninety.

Changes to this document are changes to `protocol`. Adding an optional field is
not a version bump. Changing the meaning of an existing field, or making a new
field required, is.

---

## Principles

1. **The endpoint decides.** The server publishes questions; it does not model
   what is installed where. It learns that only from reports.
2. **The agent never trusts the server with code.** Policy carries *expressions*
   in a non-Turing-complete language, never scripts. See `lineage/relevance.py`.
3. **Everything is versioned and idempotent.** A report may be re-sent; the
   server overwrites by `(host, item)`.
4. **No server is a survivable state.** The agent evaluates its cached policy
   and caches its reports. It does not stop.

---

## `GET /v1/policy`

The complete set of questions the fleet should be answering.

```json
{
  "version": 7,
  "items": [
    {
      "id": "chrome-needs-update",
      "kind": "applicability",
      "relevance": "installed(\"Google Chrome\") and package_version(\"Google Chrome\") < \"139.0\""
    },
    { "id": "os-build", "kind": "inventory", "relevance": "os_build()" }
  ]
}
```

| Field | Type | Notes |
|---|---|---|
| `version` | int | Monotonic. The agent replaces its cache when this differs. |
| `items[].id` | string | Stable, unique. Becomes a directory name — `[A-Za-z0-9._-]`. |
| `items[].kind` | string | `applicability` (result coerced to bool) or `inventory` (value kept). |
| `items[].relevance` | string | One expression. Max 4096 chars. |
| `items[].comment` | string | Optional, ignored by the agent. |

**Not yet built, and named so it does not get forgotten:** policy is
unauthenticated and unsigned. Whoever serves this owns every endpoint the
moment actions exist. Detached signature over the canonical JSON, verified by
the agent against a pinned key, must land before roadmap step 2.

## `POST /v1/report`

```json
{
  "protocol": 1,
  "agent_id": "3f2c…",
  "hostname": "WS-0412",
  "os": "Windows-11-10.0.26200",
  "sent_at": 1786265697,
  "policy_version": 7,
  "results": [
    { "id": "chrome-needs-update", "kind": "applicability",
      "value": false, "error": null, "duration_ms": 12 },
    { "id": "bitlocker-off", "kind": "applicability",
      "value": null, "error": "cim_value failed…: Access denied", "duration_ms": 415 }
  ]
}
```

| Field | Type | Notes |
|---|---|---|
| `protocol` | int | Must equal 1. The server rejects anything else. |
| `agent_id` | string | UUID generated once, stored on the endpoint. **Not the hostname** — hostnames get renamed and reused, and both silently corrupt fleet history. |
| `results[].value` | any | `bool` for applicability. String/number/bool/null for inventory; non-scalars (e.g. `Version`) are stringified by the agent. |
| `results[].error` | string \| null | Non-null means **the question could not be answered**. |

### The `error` field is load-bearing

`value: false` and `error: "…"` are different facts and must never be
collapsed. A check that could not run is not a clean bill of health — an item
that silently reports `false` on failure is how a fleet quietly stops patching
while every dashboard stays green.

This is not hypothetical: the first version of `cim_value` returned `""` when a
query failed, so `cim_value(…) != "1"` reported every machine in the estate as
BitLocker-unencrypted. Inspectors now raise; the agent records the raise. Any
future inspector that returns a sentinel on failure is a bug.

Max body 4 MiB. Larger is rejected with 413.

## `GET /v1/inventory`

`{host: {item: {value, kind, error, captured_at}}}` across the fleet. A
convenience view; the state tree below is the source of truth.

---

## State layout — deliberately opsview's

```
<root>/state/<host_key>/<item_id>/latest.json
<root>/state/<host_key>/<item_id>/<unix_ts>.json     (history, capped at 50)
```

`latest.json` matches the wrapper `opsview/orchestrator/state.py` writes:

```json
{
  "host":   { "name": "WS-0412", "ip": null, "vcenter": null, "agent_id": "3f2c…" },
  "probe":  "chrome-needs-update",
  "captured_at": 1786265697,
  "result": { "probe": "chrome-needs-update", "status": "ok",
              "summary": "applicability = False", "data": {…},
              "issues": [], "_meta": { "duration_ms": 12 } }
}
```

**This compatibility is a feature, not an accident.** opsview's Flask API
already reads this tree, which is why lineage ships no web UI: the console
exists. Do not let the shape drift without a decision to give that up.

---

## Deliberately absent from v1

| Missing | Why, and when it lands |
|---|---|
| **Authentication, TLS, content signing** | v1 takes no actions, so exposure is inventory poisoning and disclosure rather than RCE. That stops being true at roadmap step 2. **Blocks actions.** |
| **Actions** | Step 1 is "the agent changes nothing". Actions get their own endpoint, their own signing, and their own audit trail — never smuggled into policy. |
| **Push / long-poll** | Agents poll. Add a wake channel when someone actually needs sub-interval response. |
| **Delta policy** | Full policy every fetch. A policy big enough to need deltas is a policy that needs splitting. |
| **Content distribution** | Delivery Optimization and BranchCache are in the box. Do not reimplement distribution points. |

---

## Inspectors

What a relevance expression can call today (`lineage/inspectors.py`):

`file_exists` `dir_exists` `file_version` `reg_exists` `reg_value`
`service_state` `hostname` `os_caption` `os_build` `os_arch` `domain`
`installed` `package_version` `cim_value` `version` `update_ring`

An inspector that cannot answer raises; the agent records `value: null` and the
error. Never write content against `os_caption()` to tell Windows 10 from 11 —
use `os_build() >= "10.0.22000"` (docs/findings.md).
