"""The one runnable check: the relevance engine is the security boundary.

Runs under pytest, or standalone: `python tests/test_relevance.py`
No fixtures, no framework. Inspectors are plain dicts of fakes, which is the
whole reason the evaluator takes an inspector table instead of importing one.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lineage.relevance import (  # noqa: E402
    MAX_INSPECTOR_CALLS, Relevance, RelevanceError, Version,
)

FAKE = {
    "file_exists": lambda p: p == r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    "file_version": lambda p: Version("138.0.7204.97"),
    "os_build": lambda: Version("10.0.26100.2314"),
    "os_arch": lambda: "x64",
    "domain": lambda: "corp.example.com",
    "installed": lambda n: n.lower() in ("google chrome", "7-zip"),
    "package_version": lambda n: Version("24.09" if n == "7-zip" else ""),
    "service_state": lambda n: "running" if n == "wuauserv" else "",
    "boom": lambda: 1 / 0,
    "slow": lambda: time.sleep(0.05) or True,
}


def ev(src, inspectors=FAKE):
    return Relevance(src).evaluate(inspectors)


def rejects(src, needle=None):
    try:
        Relevance(src).evaluate(FAKE)
    except RelevanceError as e:
        assert needle is None or needle in str(e), f"{src!r}: wrong error {e!r}"
        return
    raise AssertionError(f"{src!r} should have been rejected")


# --------------------------------------------------------------------------
def test_version_is_numeric_not_lexical():
    # The bug this whole class exists to prevent.
    assert Version("1.10") > Version("1.9")
    assert Version("138.0.7204.97") < Version("139.0")
    assert Version("10.0.26100") >= "10.0.22000"
    assert Version("2.0") == "2.0.0.0"          # zero-padding
    assert Version("") < "0.0.0.1"              # missing file sorts below everything
    assert not Version("")


def test_applicability():
    assert ev('file_exists(r"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe")') is True
    assert ev('installed("Google Chrome") and file_version("x") < "139.0"') is True
    assert ev('installed("Google Chrome") and file_version("x") >= "139.0"') is False
    assert ev('os_build() >= "10.0.22000" and os_arch() == "x64"') is True
    assert ev('service_state("wuauserv") == "running"') is True
    assert ev('service_state("nosuchsvc") == "running"') is False


def test_inventory_returns_values_not_just_booleans():
    # Same engine, same expressions -- only the caller's intent differs.
    assert ev("os_arch()") == "x64"
    assert str(ev("os_build()")) == "10.0.26100.2314"
    assert ev("domain()") == "corp.example.com"


def test_operators():
    assert ev('"example" in domain()') is True
    assert ev('"contoso" in domain()') is False
    assert ev("not installed(\"Notepad++\")") is True
    assert ev('true or boom()') is True          # or short-circuits past the exploding inspector
    assert ev('false and boom()') is False       # and does too
    assert ev('"10.0.1" < os_build() < "11.0"') is True   # chained comparison


def test_short_circuit_protects_dependent_calls():
    calls = []
    inspectors = {
        "file_exists": lambda p: calls.append("exists") or False,
        "file_version": lambda p: calls.append("version") or Version("1.0"),
    }
    assert Relevance('file_exists("x") and file_version("x") >= "2.0"').evaluate(inspectors) is False
    assert calls == ["exists"], "file_version must not be called for a missing file"


# --------------------------------------------------------------------------
# The security boundary. These are the tests that matter.
# --------------------------------------------------------------------------
def test_rejects_code_execution_paths():
    rejects('__import__("os").system("calc")', "only direct inspector calls")
    rejects('().__class__', "not allowed")                      # attribute access
    rejects('os_arch().upper()', "only direct inspector calls")
    rejects('[x for x in range(10)]', "not allowed")            # comprehension
    rejects('(lambda: 1)()', "only direct inspector calls")
    rejects('open("f")', "unknown inspector")
    rejects('os_arch()[0]', "not allowed")                      # subscript
    rejects('f"{os_arch()}"', "not allowed")                    # f-string
    rejects('x := 1', "syntax error")


def test_rejects_malformed_and_unknown():
    rejects("", "empty")
    rejects("os_arch(", "syntax error")
    rejects("nonexistent_inspector()", "unknown inspector")
    rejects("some_bare_name", "unknown name")
    rejects('file_exists(path="x")', "keyword arguments")
    rejects("x" * 5000, "too long")


def test_budgets_are_enforced():
    rejects(" or ".join(["false"] * 300), "too complex")
    # `or` keeps evaluating while each result is falsey, so all of these run.
    rejects(" or ".join(["installed('nope')"] * (MAX_INSPECTOR_CALLS + 5)), "too many inspector calls")

    expr = Relevance(" and ".join(["slow()"] * 20))
    try:
        expr.evaluate(FAKE, deadline=time.monotonic() + 0.1)
        raise AssertionError("should have hit the deadline")
    except RelevanceError as e:
        assert "exceeded" in str(e)


def test_inspector_failure_surfaces_not_silently_false():
    # A silently-false applicability check is how a fleet quietly stops patching.
    rejects("boom()", "boom failed")
    rejects("os_arch() > 5", "cannot compare")
    # But a Version compared against a bare number is coerced, not an error --
    # `os_build() >= 10` is sloppy content, not a broken machine.
    assert ev("os_build() >= 10") is True


# --------------------------------------------------------------------------
def _main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    _main()
