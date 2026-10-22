"""Self-update decision logic. The crash-loop test is the one that matters.

Runs under pytest, or standalone: `python tests/test_update.py`
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lineage.update import (  # noqa: E402
    MAX_STAGING_ATTEMPTS, PROBATION_SECONDS, REQUIRED_HEALTHY_CYCLES,
    activate, activation_jitter, begin_staging, clear_refusal, decide, is_refused,
    load_state, new_state, promote, prunable_versions, record_crash, record_cycle,
    rollback, save_state, staging_failed, status, update_ring,
)

T0 = 1_800_000_000.0
OFFER = {"version": "1.4.3", "published_at": T0}
AGENT = "3f2c-agent-id"


def fresh(current="1.4.2", **kw):
    s = new_state(current, golden="1.0.0")
    s.update(kw)
    return s


def past_jitter(now=T0):
    return now + activation_jitter(AGENT) + 1


def run(state, offer=OFFER, now=None):
    return decide(state, offer, now=now if now else past_jitter(), agent_id=AGENT)


# --------------------------------------------------------------------------
def test_happy_path():
    s = fresh(reporting_ok=True)
    assert run(s).action == "stage"

    begin_staging(s, "1.4.3")
    activate(s, "1.4.3", now=T0)
    assert decide(s, OFFER, now=T0, agent_id=AGENT).action == "wait"

    for _ in range(REQUIRED_HEALTHY_CYCLES):
        record_cycle(s, ok=True, reported=True)
    d = decide(s, OFFER, now=T0 + 60, agent_id=AGENT)
    assert d.action == "promote", d

    promote(s, "1.4.3")
    assert s["current"] == "1.4.3"
    assert s["probation"] is None
    assert decide(s, OFFER, now=T0 + 99, agent_id=AGENT).action == "wait"  # already there


def test_crash_during_probation_rolls_back_and_refuses():
    s = fresh()
    activate(s, "1.4.3", now=T0)
    record_crash(s)

    d = decide(s, OFFER, now=T0 + 10, agent_id=AGENT)
    assert d.action == "rollback" and "crashed" in d.reason

    rollback(s, d.version, d.reason)
    assert s["current"] == "1.4.2"
    assert is_refused(s, "1.4.3")


def test_the_crash_loop():
    """The failure this module exists for.

    Roll back correctly, then re-apply the same build because the server is
    still publishing it, forever, on every endpoint. Rolling back is the easy
    half; refusing to re-apply is the half that saves the fleet.
    """
    s = fresh()
    activate(s, "1.4.3", now=T0)
    record_crash(s)
    d = decide(s, OFFER, now=T0 + 10, agent_id=AGENT)
    rollback(s, d.version, d.reason)

    # The server has not changed its mind. Ask ten more times.
    for i in range(10):
        d = decide(s, OFFER, now=past_jitter() + i * 3600, agent_id=AGENT)
        assert d.action == "wait", f"attempt {i}: re-applied a refused version"
        assert "refused" in d.reason

    # A genuinely newer build is still allowed through -- refusal is per-version,
    # not a latch that strands the machine forever.
    assert run(s, {"version": "1.4.4", "published_at": T0}).action == "stage"


def test_refusal_survives_a_reboot():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "update-state.json"
        s = fresh()
        activate(s, "1.4.3", now=T0)
        record_crash(s)
        rollback(s, "1.4.3", "crashed")
        save_state(path, s)

        # ...power cycle...
        reloaded = load_state(path)
        assert is_refused(reloaded, "1.4.3")
        assert reloaded["current"] == "1.4.2"
        assert decide(reloaded, OFFER, now=past_jitter(), agent_id=AGENT).action == "wait"


def test_unreadable_state_does_not_forget_refusals_silently():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "update-state.json"
        path.write_text("{ this is not json")
        s = load_state(path, current="1.4.2")
        # It cannot recover refusals it cannot read -- but it must come back to a
        # sane, usable state rather than crashing the agent on boot.
        assert s["current"] == "1.4.2" and s["refused"] == {}


def test_deadline_with_too_few_cycles_rolls_back():
    s = fresh()
    activate(s, "1.4.3", now=T0)
    record_cycle(s, ok=True, reported=True)          # 1 of 3
    d = decide(s, OFFER, now=T0 + PROBATION_SECONDS + 1, agent_id=AGENT)
    assert d.action == "rollback" and "1/3" in d.reason


def test_healthy_but_deaf_rolls_back_only_if_it_used_to_talk():
    # Previous version was reporting -> the new one must too.
    s = fresh(reporting_ok=True)
    activate(s, "1.4.3", now=T0)
    for _ in range(REQUIRED_HEALTHY_CYCLES):
        record_cycle(s, ok=True, reported=False)
    assert decide(s, OFFER, now=T0 + 60, agent_id=AGENT).action == "wait"
    d = decide(s, OFFER, now=T0 + PROBATION_SECONDS + 1, agent_id=AGENT)
    assert d.action == "rollback" and "never reported" in d.reason

    # A machine that was already offline is not a failing machine.
    s2 = fresh(reporting_ok=False)
    activate(s2, "1.4.3", now=T0)
    for _ in range(REQUIRED_HEALTHY_CYCLES):
        record_cycle(s2, ok=True, reported=False)
    assert decide(s2, OFFER, now=T0 + 60, agent_id=AGENT).action == "promote"


def test_staging_failures_retry_and_never_refuse():
    """A full disk must not permanently reject a good version."""
    s = fresh()
    now = past_jitter()
    for attempt in range(1, MAX_STAGING_ATTEMPTS + 1):
        assert decide(s, OFFER, now=now, agent_id=AGENT).action == "stage"
        begin_staging(s, "1.4.3")
        staging_failed(s, "1.4.3", "disk full", now=now)
        assert not is_refused(s, "1.4.3"), "staging failure must never refuse"

        blocked = decide(s, OFFER, now=now, agent_id=AGENT)
        assert blocked.action == "wait"
        if attempt < MAX_STAGING_ATTEMPTS:
            assert blocked.wait_seconds > 0, f"attempt {attempt} should back off"
            now += blocked.wait_seconds + 1
        else:
            # Attempts exhausted: stop trying until a new version is offered.
            assert "staging failed" in blocked.reason

    assert not is_refused(s, "1.4.3")
    # A new offer is still allowed to try -- exhaustion is per-offer, not a refusal.
    assert decide(s, {"version": "1.4.4", "published_at": T0},
                  now=past_jitter(), agent_id=AGENT).action == "stage"


def test_backoff_actually_grows():
    s = fresh()
    waits = []
    now = past_jitter()
    for _ in range(3):
        begin_staging(s, "1.4.3")
        staging_failed(s, "1.4.3", "timeout", now=now)
        waits.append(decide(s, OFFER, now=now, agent_id=AGENT).wait_seconds)
    assert waits[0] < waits[1] < waits[2], waits


def test_no_downgrade_from_the_server():
    s = fresh(current="1.4.2")
    for older in ("1.4.1", "1.4.2", "0.9"):
        d = decide(s, {"version": older, "published_at": T0}, now=past_jitter(), agent_id=AGENT)
        assert d.action == "wait" and "not newer" in d.reason


def test_probation_outranks_a_new_offer():
    s = fresh()
    activate(s, "1.4.3", now=T0)
    record_crash(s)
    d = decide(s, {"version": "1.4.9", "published_at": T0}, now=T0 + 5, agent_id=AGENT)
    assert d.action == "rollback" and d.version == "1.4.3"


def test_jitter_and_ring_are_stable_and_spread():
    assert update_ring(AGENT) == update_ring(AGENT)
    assert activation_jitter(AGENT) == activation_jitter(AGENT)
    rings = [update_ring(f"agent-{i}") for i in range(500)]
    assert all(0 <= r < 100 for r in rings)
    assert len(set(rings)) > 60, "ring assignment is not spreading the fleet"
    assert 5 < sum(1 for r in rings if r < 5) / 5 < 50   # ~1% ring is small but non-empty


def test_jitter_delays_the_stampede():
    s = fresh()
    d = decide(s, OFFER, now=T0, agent_id=AGENT)
    assert d.action == "wait" and d.wait_seconds > 0 and "jitter" in d.reason


def test_pruning_never_eats_the_floor():
    s = fresh(current="1.4.2")
    activate(s, "1.4.3", now=T0)          # probation: previous=1.4.2
    installed = ["1.0.0", "1.2.0", "1.4.0", "1.4.1", "1.4.2", "1.4.3"]
    prunable = prunable_versions(s, installed)
    for must_keep in ("1.0.0", "1.4.2", "1.4.3"):
        assert must_keep not in prunable, f"pruned {must_keep}"
    assert "1.2.0" in prunable


def test_operator_can_clear_a_refusal():
    s = fresh()
    activate(s, "1.4.3", now=T0)
    record_crash(s)
    rollback(s, "1.4.3", "crashed")
    assert status(s)["refused_count"] == 1
    clear_refusal(s, "1.4.3")
    assert run(s).action == "stage"


# --------------------------------------------------------------------------
def _main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    _main()
