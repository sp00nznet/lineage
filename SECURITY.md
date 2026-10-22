# Security

lineage reads a lot about the machines it runs on: every file opened or written
(ETW / `/proc`), every scheduled job and its command line, installed software,
mounts, and registry values a policy asks for. It runs elevated to do that. This
file says what it does with that, and what is not protected yet.

## What is true today

- **Read-only.** No code path changes anything on an endpoint. The agent answers
  relevance expressions; `lineage.collect` observes. There is no action executor.
- **The relevance language is not Turing complete.** No loops, assignment,
  attribute access or indexing; bounded by node count, inspector calls and wall
  clock (`lineage/relevance.py`). A malicious policy can ask questions, not run code.
- **No authentication between agent and server.** `lineage.server` binds to
  loopback and warns loudly if told otherwise. Do not expose it to a network.
  Signed policy and mutual auth are roadmap step 2 and block every action.
- **Collected data stays on the host** in `C:\ProgramData\lineage` (Windows,
  SYSTEM-owned) or `$LINEAGE_HOME`. It contains file paths, process image paths,
  scheduled-task command lines and host aliases (names and IPs). Treat a pulled
  `graph.db` and its snapshots as sensitive inventory.
- **`tools/pve.py` uses a Proxmox root password** from `PVE_PASSWORD`. It is
  never written to disk by lineage. TLS verification is off by default because
  Proxmox nodes ship self-signed certificates; set `PVE_CA` to pin one.
- **The guest Python runtime is pinned by SHA-256** (`tools/pve.py`, `PY_SHA256`);
  a mismatched download aborts the install.

## Never committed

Pulled fleet data (`var/fleet/`), agent state (`var/agent-state/`, `var/state/`),
credentials, and internal hostnames or address ranges. Test fixtures use
placeholder names (`pve-a`, `pve-b`) and the RFC 5737 range `192.0.2.0/24`.

## Reporting

This repository is private. Report anything security-relevant to the maintainer
directly rather than in an issue.
