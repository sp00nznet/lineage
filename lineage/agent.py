"""lineage.agent — evaluate this machine's policy, report the answers.

Step 1 of the roadmap, and deliberately the whole of it: **the agent takes no
actions.** It answers questions about itself and reports. That alone is the
inventory product, and it is the cheapest possible proof that the relevance
model works before anything is allowed to change a machine.

Design rules that are not up for negotiation (see docs/api.md):

- **Fail safe, not fail closed.** No server contact means keep evaluating the
  last known policy and keep caching reports. It does not mean stop.
- **Bounded evaluation.** Every expression gets a node, call and wall-clock
  budget. One bad expression degrades one result, never the cycle.
- **An expression that fails is a reported result, not a swallowed one.** A
  silently-false applicability check is how a fleet quietly stops patching.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import socket
import time
import urllib.error
import urllib.request
from pathlib import Path

from .inspectors import build_inspectors
from .relevance import MAX_SECONDS, Relevance, RelevanceError

PROTOCOL_VERSION = 1
DEFAULT_INTERVAL = 300          # seconds between cycles
DEFAULT_STATE = Path(os.environ.get("LINEAGE_AGENT_STATE", Path.home() / ".lineage-agent"))


# --------------------------------------------------------------------------
# Evaluation
# --------------------------------------------------------------------------
def evaluate_policy(policy, inspectors=None, machine_id=""):
    """Evaluate every item. Returns the `results` array of a report.

    One inspector table for the whole cycle, so the expensive caches (installed
    software, CIM) are paid for once no matter how many items ask.
    """
    inspectors = inspectors if inspectors is not None else build_inspectors(machine_id)
    results = []

    for item in policy.get("items", []):
        item_id = item.get("id") or "(unnamed)"
        kind = item.get("kind", "applicability")
        started = time.monotonic()
        value, error = None, None

        try:
            expr = Relevance(item["relevance"])
            value = expr.evaluate(inspectors, deadline=started + MAX_SECONDS)
            if kind == "applicability":
                value = bool(value)
            elif not isinstance(value, (str, int, float, bool, type(None))):
                value = str(value)      # Version and friends go over the wire as text
        except KeyError:
            error = "item has no 'relevance' expression"
        except RelevanceError as e:
            error = str(e)
        except Exception as e:            # an inspector bug must not kill the cycle
            error = f"internal: {type(e).__name__}: {e}"

        results.append({
            "id": item_id,
            "kind": kind,
            "value": value,
            "error": error,
            "duration_ms": int((time.monotonic() - started) * 1000),
        })

    return results


def build_report(policy, results, agent_id):
    return {
        "protocol": PROTOCOL_VERSION,
        "agent_id": agent_id,
        "hostname": platform.node(),
        "os": platform.platform(),
        "sent_at": int(time.time()),
        "policy_version": policy.get("version", 0),
        "results": results,
    }


# --------------------------------------------------------------------------
# Local state -- the reason "no server" is survivable
# --------------------------------------------------------------------------
def _state_dir(root=None):
    d = Path(root or DEFAULT_STATE)
    d.mkdir(parents=True, exist_ok=True)
    return d


def agent_id(root=None):
    """Stable per-machine id, generated once. Not the hostname: hostnames get
    reused and renamed, and both quietly corrupt fleet history."""
    f = _state_dir(root) / "agent-id"
    if f.exists():
        existing = f.read_text().strip()
        if existing:
            return existing
    import uuid
    new = str(uuid.uuid4())
    f.write_text(new)
    return new


def load_cached_policy(root=None):
    f = _state_dir(root) / "policy.json"
    if not f.exists():
        return {"version": 0, "items": []}
    try:
        return json.loads(f.read_text())
    except json.JSONDecodeError:
        return {"version": 0, "items": []}


def save_cached_policy(policy, root=None):
    (_state_dir(root) / "policy.json").write_text(json.dumps(policy, indent=2))


def fetch_policy(server, timeout=30):
    with urllib.request.urlopen(f"{server.rstrip('/')}/v1/policy", timeout=timeout) as r:
        return json.loads(r.read().decode())


def send_report(server, report, timeout=30):
    data = json.dumps(report).encode()
    req = urllib.request.Request(
        f"{server.rstrip('/')}/v1/report", data=data,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status


# --------------------------------------------------------------------------
# One cycle
# --------------------------------------------------------------------------
def run_cycle(server, root=None, *, offline=False):
    """Fetch, evaluate, report. Returns the report regardless of server health."""
    policy = load_cached_policy(root)

    if not offline:
        try:
            fresh = fetch_policy(server)
            if fresh.get("version", 0) != policy.get("version", -1):
                save_cached_policy(fresh, root)
            policy = fresh
        except (urllib.error.URLError, socket.timeout, json.JSONDecodeError, OSError) as e:
            # Fail safe: the cached policy is still the right thing to evaluate.
            print(f"[lineage] policy fetch failed ({e}); using cached v{policy.get('version', 0)}")

    machine_id = agent_id(root)
    results = evaluate_policy(policy, machine_id=machine_id)
    report = build_report(policy, results, machine_id)

    if not offline:
        try:
            send_report(server, report)
        except (urllib.error.URLError, socket.timeout, OSError) as e:
            print(f"[lineage] report send failed ({e}); cached locally")

    (_state_dir(root) / "last-report.json").write_text(json.dumps(report, indent=2))
    return report


def main(argv=None):
    ap = argparse.ArgumentParser(prog="lineage-agent", description="Evaluate and report this machine.")
    ap.add_argument("--server", default=os.environ.get("LINEAGE_SERVER", "http://127.0.0.1:8770"))
    ap.add_argument("--state", default=None, help="agent state directory")
    ap.add_argument("--once", action="store_true", help="one cycle, then exit")
    ap.add_argument("--offline", action="store_true", help="evaluate cached policy, talk to nobody")
    ap.add_argument("--interval", type=int, default=DEFAULT_INTERVAL)
    args = ap.parse_args(argv)

    while True:
        report = run_cycle(args.server, args.state, offline=args.offline)
        ok = sum(1 for r in report["results"] if not r["error"])
        bad = len(report["results"]) - ok
        print(f"[lineage] {ok} evaluated, {bad} failed, policy v{report['policy_version']}")
        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
