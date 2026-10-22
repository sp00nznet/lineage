"""lineage.update — the decision half of agent self-update.

Everything here is a pure function over a JSON state dict. No downloads, no
subprocesses, no file replacement: that is the supervisor's job (see
docs/SELF-UPDATE.md) and it is plumbing. *This* is the part where being wrong
bricks a fleet, so it exists first and it is the part with tests.

The whole module is built around one asymmetry:

    Rolling back a bad version is the easy half.
    Not re-applying the version you just rejected is the half that saves you.

An agent that rolls back correctly and then re-applies the same bad build --
because the server is still publishing it -- is in an infinite crash loop across
every endpoint, and it looks like the update system is working.
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from .relevance import Version

REQUIRED_HEALTHY_CYCLES = 3
PROBATION_SECONDS = 30 * 60
MAX_STAGING_ATTEMPTS = 5          # transient failures: retry, never refuse
STAGING_BACKOFF_BASE = 60         # seconds, doubled per attempt
MAX_JITTER_SECONDS = 30 * 60
KEEP_VERSIONS = 2                 # besides golden


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------
def new_state(current, golden=None):
    return {
        "current": str(current),
        "golden": str(golden or current),
        "probation": None,
        "refused": {},
        "staging": None,
        "reporting_ok": False,     # did *this* version manage to reach the server
        "history": [],
    }


def load_state(path, current="0.0.0"):
    p = Path(path)
    if not p.exists():
        return new_state(current)
    try:
        state = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        # Unreadable state must not mean "no refusals". Fail closed here --
        # this is the one place where forgetting is worse than not updating.
        return new_state(current)
    for key, default in new_state(current).items():
        state.setdefault(key, default)
    return state


def save_state(path, state):
    """Write via a temp file + replace, so a power cut cannot truncate it.

    Refusals in particular must survive anything; a half-written state file that
    loses them is exactly the crash loop this module exists to prevent.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(p)          # atomic on NTFS and POSIX
    return p


# --------------------------------------------------------------------------
# Decisions
# --------------------------------------------------------------------------
class Decision:
    __slots__ = ("action", "version", "reason", "wait_seconds")

    def __init__(self, action, version=None, reason="", wait_seconds=0):
        self.action = action          # wait | stage | activate | promote | rollback
        self.version = version
        self.reason = reason
        self.wait_seconds = wait_seconds

    def __eq__(self, other):
        return isinstance(other, Decision) and (
            self.action, self.version, self.reason) == (other.action, other.version, other.reason)

    def __repr__(self):
        v = f" {self.version}" if self.version else ""
        return f"<{self.action}{v}: {self.reason}>"


def update_ring(agent_id):
    """Stable 0-99 bucket. Stable is the whole point: a machine must land in the
    same ring on every boot, or "canary" is just a random 5% each time."""
    digest = hashlib.sha256(str(agent_id).encode()).digest()
    return int.from_bytes(digest[:4], "big") % 100


def activation_jitter(agent_id, window=MAX_JITTER_SECONDS):
    """Derived from agent_id, not random -- so it survives a restart and the
    fleet does not stampede the server the instant a version is published."""
    digest = hashlib.sha256(f"jitter:{agent_id}".encode()).digest()
    return int.from_bytes(digest[:4], "big") % max(1, int(window))


def is_refused(state, version):
    return str(version) in state.get("refused", {})


def decide(state, offer=None, now=None, agent_id="", jitter_window=MAX_JITTER_SECONDS):
    """What should happen right now?

    Called at two moments: when policy offers a version, and at the end of every
    cycle. Probation is resolved first -- an in-flight update always outranks a
    new offer, because promoting or rolling back is how we get back to a known
    state.
    """
    now = time.time() if now is None else now
    prob = state.get("probation")

    # 1. Resolve any update already in flight.
    if prob:
        version = prob.get("version")
        if prob.get("crashed"):
            return Decision("rollback", version, "worker crashed during probation")
        if prob.get("cycles_ok", 0) >= REQUIRED_HEALTHY_CYCLES:
            # A build that evaluates fine but can no longer reach the server is
            # a bricked agent that looks healthy from the inside. Only hold it
            # to that standard if the previous version was actually reporting.
            if prob.get("require_reporting") and not prob.get("reported_ok"):
                if now >= prob.get("deadline", 0):
                    return Decision("rollback", version, "healthy but never reported in")
                return Decision("wait", version, "healthy; waiting for a successful report")
            return Decision("promote", version, "probation passed")
        if now >= prob.get("deadline", 0):
            return Decision(
                "rollback", version,
                f"only {prob.get('cycles_ok', 0)}/{REQUIRED_HEALTHY_CYCLES} healthy cycles before deadline")
        return Decision("wait", version, "on probation")

    if not offer:
        return Decision("wait", None, "nothing offered")

    # 2. Gate the offer. All local, all cheap -- no download to find out.
    version = str(offer.get("version", ""))
    if not version:
        return Decision("wait", None, "offer has no version")

    if is_refused(state, version):
        entry = state["refused"][version]
        return Decision("wait", version, f"refused: {entry.get('reason', 'previously failed probation')}")

    if Version(version) <= Version(state["current"]):
        # Rollback is a local decision from local evidence, never a server
        # instruction. This closes the downgrade-attack shape.
        return Decision("wait", version, f"not newer than {state['current']}")

    staging = state.get("staging")
    if staging and staging.get("version") == version:
        attempts = staging.get("attempts", 0)
        if attempts >= MAX_STAGING_ATTEMPTS:
            return Decision("wait", version, f"staging failed {attempts}x; waiting for a new offer")
        ready_at = staging.get("retry_at", 0)
        if now < ready_at:
            return Decision("wait", version, "backing off after a staging failure",
                            wait_seconds=int(ready_at - now))
        return Decision("stage", version, f"retry {attempts + 1}/{MAX_STAGING_ATTEMPTS}")

    ready_at = offer.get("published_at", 0) + activation_jitter(agent_id, jitter_window)
    if now < ready_at:
        return Decision("wait", version, "jittered wait", wait_seconds=int(ready_at - now))

    return Decision("stage", version, "newer, not refused, ring reached")


# --------------------------------------------------------------------------
# Transitions -- each returns the mutated state, caller persists it
# --------------------------------------------------------------------------
def begin_staging(state, version, now=None):
    now = time.time() if now is None else now
    staging = state.get("staging") or {}
    if staging.get("version") != version:
        staging = {"version": version, "attempts": 0}
    staging["attempts"] = staging.get("attempts", 0) + 1
    staging["started_at"] = now
    state["staging"] = staging
    return state


def staging_failed(state, version, reason, now=None):
    """Transient by definition: download, hash, signature, disk. Back off, retry.

    Deliberately does NOT refuse. A good version must not be rejected forever
    because a disk was full once -- that is how a fleet strands itself on an old
    build with no obvious cause.
    """
    now = time.time() if now is None else now
    staging = state.get("staging") or {"version": version, "attempts": 1}
    attempts = staging.get("attempts", 1)
    staging["retry_at"] = now + STAGING_BACKOFF_BASE * (2 ** (attempts - 1))
    staging["last_error"] = reason
    state["staging"] = staging
    return state


def activate(state, version, now=None, require_reporting=None):
    """Pointer flipped, worker restarted. Probation starts."""
    now = time.time() if now is None else now
    if require_reporting is None:
        require_reporting = bool(state.get("reporting_ok"))
    state["probation"] = {
        "version": str(version),
        "previous": state["current"],
        "started_at": now,
        "deadline": now + PROBATION_SECONDS,
        "cycles_ok": 0,
        "reported_ok": False,
        "require_reporting": require_reporting,
        "crashed": False,
    }
    state["staging"] = None
    return state


def record_cycle(state, *, ok=True, reported=False):
    """One completed evaluation cycle. Feeds probation and tracks reachability."""
    if reported:
        state["reporting_ok"] = True
    prob = state.get("probation")
    if not prob:
        return state
    if ok:
        prob["cycles_ok"] = prob.get("cycles_ok", 0) + 1
    if reported:
        prob["reported_ok"] = True
    return state


def record_crash(state):
    """The supervisor saw the worker die. During probation that is fatal, not a
    strike -- a version that crashes once will crash again, and each restart is
    another window where the machine is unmanaged."""
    prob = state.get("probation")
    if prob:
        prob["crashed"] = True
    return state


def promote(state, version, now=None):
    now = time.time() if now is None else now
    state["current"] = str(version)
    state["probation"] = None
    state["history"].append({"at": now, "event": "promote", "version": str(version)})
    return state


def rollback(state, version, reason, now=None):
    """Roll back AND refuse, in that order in the code and on disk.

    Refusal is written before the rollback is considered complete. If the
    machine loses power between the two, it comes up on the old version with the
    refusal already recorded -- which is the safe direction. The reverse order
    would come up ready to re-apply the build that just failed.
    """
    now = time.time() if now is None else now
    prob = state.get("probation") or {}
    version = str(version or prob.get("version", ""))
    previous = prob.get("previous") or state["current"]

    entry = state["refused"].get(version, {"attempts": 0})
    entry["attempts"] = entry.get("attempts", 0) + 1
    entry["at"] = now
    entry["reason"] = reason
    state["refused"][version] = entry

    state["current"] = str(previous)
    state["probation"] = None
    state["staging"] = None
    state["history"].append({"at": now, "event": "rollback", "version": version, "reason": reason})
    return state


def clear_refusal(state, version):
    """Operator action, from the console, deliberately. Never automatic."""
    state.get("refused", {}).pop(str(version), None)
    return state


def prunable_versions(state, installed):
    """Which version directories may be deleted. Never current, never golden,
    never the rollback target, and always keep KEEP_VERSIONS recent ones."""
    keep = {str(state["current"]), str(state["golden"])}
    prob = state.get("probation")
    if prob:
        keep.add(str(prob.get("version", "")))
        keep.add(str(prob.get("previous", "")))
    ordered = sorted((str(v) for v in installed), key=Version, reverse=True)
    for v in ordered[:KEEP_VERSIONS]:
        keep.add(v)
    return [v for v in ordered if v not in keep]


def status(state):
    """What the console needs. The refusal count is the number that matters --
    "1,847 endpoints refused 1.4.3" is a caught bad build; not having it is an
    outage nobody noticed."""
    prob = state.get("probation")
    return {
        "current": state["current"],
        "golden": state["golden"],
        "on_probation": prob.get("version") if prob else None,
        "probation_cycles": prob.get("cycles_ok", 0) if prob else 0,
        "refused": sorted(state.get("refused", {}).keys()),
        "refused_count": len(state.get("refused", {})),
    }
