# Changelog

All notable changes. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versions: [SemVer](https://semver.org/). Entries before 2026-10-15 were
reconstructed from git history when this file was created; from here on they are
written as the work lands.

## [Unreleased]

### Added
- `tools/demo.py`: a made-up two-host fleet (Windows app server, Linux report box,
  a shared file share) for the console. README screenshots come from it, never
  from real pulled data. Seeded and on fixed dates, so they regenerate the same.
- README screenshots of every console tab (`docs/screenshots/`).
- `CONTRIBUTING.md`.
- The repository is public. Security reports go through GitHub's private
  vulnerability reporting (SECURITY.md).
- `lineage.web`: a read-only console over `var/fleet` (coverage timeline with
  gaps, actors, scream test, file search, jobs, links). Stdlib, loopback only.
- `lineage.collect`: D1–D5 as one read-only cycle (`run`), an idempotent central
  merge (`merge`), and a coverage + scream-test `report`. Windows keeps one ETW
  session rolling between hourly cycles so the unwatched gap is seconds.
- Actor names from `Microsoft-Windows-Kernel-Process` ProcessStart events, added
  to the same trace session with `logman update`.
- `tools/pve.py`: deploy to and pull from Windows guests through the Proxmox
  guest agent, stdlib only. Ships the embeddable CPython pinned by SHA-256.
- Daily D1/D2 snapshot (jobs + mounts) in agent-report shape.
- Cron conformance harness against croniter, with a baseline that CI enforces
  (`tools/conformance.py`, 58/58).
- `graph.merge_store`, `etw.correlate`, `etw.decode`, `etw.start_session` moved
  into the library from `tools/etw_capture.py`.
- CI on Linux and Windows, Python 3.9 and 3.12: pyflakes, pytest, conformance.
- `Setup.cmd` / `setup.sh` quick start and `tests/run.py` (no test framework needed).
- LICENSE (MIT), SECURITY.md, ROADMAP.md, docs/architecture.md, docs/data-model.md,
  docs/running.md, docs/findings.md, docs/design.md, docs/proxmox-guest-agent.md.

### Changed
- Docs and comments describe competing products by kind, not by brand name, and
  no longer link to private idea notes.
- README restructured; design reasoning moved to docs/design.md and findings to
  docs/findings.md. `docs/PROTOCOL.md` is now `docs/api.md`.
- Observation windows are measured by uptime, so a wall-clock jump cannot
  inflate coverage.
- Test fixtures and docs use placeholder hostnames and RFC 5737 addresses.

### Fixed
- The ETW process snapshot never parsed (a PowerShell single-quoted `` `t ``),
  so every actor was `pid:NNNN`. Protected processes now fall back to `Name`.
- `cron_max_gap_days` ignored day rules for schedules that run several times a
  day: `0 8-18/2 * * 1-5` reported 14 hours instead of 2.6 days. Found by the
  conformance harness.
- The collector no longer reports its own file activity, including opens of its
  home directory itself, and opens of `c:/`
  and `c:/windows` themselves count as noise.

## [0.1.0] - 2026-10-02 (untagged)

### Added
- Relevance engine, Windows inspectors, agent and server (2026-08-09).
- Self-update decision logic.
- Discovery track D1–D5: declared job frequency, seasonal diff, `/proc` and ETW
  taps, storage-identity resolver, persisted dependency graph and scream test.

### Fixed
- `os_caption` trusts the build number, not ProductName, for Windows 11 (2026-10-02).
- ETW NT device paths and process attribution, found by a real capture.
