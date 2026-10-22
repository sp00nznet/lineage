# Data model

## `graph.db` (sqlite, `lineage/graph.py`)

One per host at `<home>/graph.db`, cumulative; `var/fleet/central.db` holds
several hosts after `tools/pve.py pull`. Raw observations are never stored: 48k
in thirty seconds on one node is why.

### `edges`: one counted row per who-touched-what-which-way

| Column | Meaning |
|---|---|
| `host` | the observing host, lower-case hostname |
| `container` | `lxc:<id>`, `qemu:<id>`, `docker:<id>`, or `''` for the host itself |
| `actor` | image path of the process (`c:/windows/system32/svchost.exe`), or `pid:<n>` when nothing named it |
| `volume` | storage identity: `smb://server/share`, `nfs://server/export`, `dev://host/c:`, `dev://host/<maj:min>`, `vol://<guid>` |
| `rel` | path within that volume, collapsed to a pattern (`/out/x_<date>.csv`) |
| `direction` | `read`, `write`, or `readwrite`. On ETW, `read` means *opened and not written* |
| `count` | observations folded into this row |
| `first_seen`, `last_seen` | epoch seconds |
| `examples` | JSON list of up to three concrete paths behind the pattern |

Primary key `(host, container, actor, volume, rel, direction)`; index on
`(volume, rel)` for the link join. A link is a writer row and a reader row on
the same `(volume, rel)` with different actors. They need never share a window.

### `windows`: when we were actually watching

| Column | Meaning |
|---|---|
| `host`, `tap` | which host, and `etw` or `proc-sample` |
| `started`, `ended` | epoch seconds; `started` is the later of the session start (by uptime) and the first event still held |

Without this table a quiet artifact and an unwatched one look identical.

### `host_aliases`: one machine, many names

`alias` (hostname, FQDN, every IP) → `canonical`. Volumes are rewritten through
it so `nfs://192.0.2.23/beta` and `nfs://pve-a/beta` meet.

## Daily snapshot (`<home>/snapshots/YYYY-MM-DD.json`)

Agent-report shape, so `seasons.snapshot_from_reports` reads it unchanged.
Overwritten through the day, so the last cycle's view wins.

```json
{"hostname": "win-a", "sent_at": 1792050000.0,
 "results": [{"id": "job:\\EOM close", "value": "CalendarTrigger"},
             {"id": "mount://fs01/data", "value": "\\device\\mup\\fs01\\data"}],
 "jobs": [{"id": "\\EOM close", "kind": "scheduled-task", "schedule": "CalendarTrigger",
           "max_gap_days": 31, "frequency": "monthly", "enabled": true, "host": "win-a"}]}
```

`job:<id>` values are trigger summaries; `mount:<target>` maps a mount (drive
letter or `//server/share` on Windows, mountpoint on Linux) to its source.

## ETW state (`<home>/etw/`)

`current.etl` is the running session's circular file. `current.json` holds
`started` (wall clock), `mono` (uptime) and `images` (pid → image) from when it
started. A `done-<epoch>.etl` exists only while a cycle decodes it.

## Agent and server state

The server writes opsview's tree under `var/state/<host>/<item>/<epoch>.json`
and `latest.json`; the agent caches under `var/agent-state/`. Shapes are in
[api.md](api.md).
