"""lineage.relevance — the one primitive.

An expression, evaluated *on the endpoint*, that answers a question about that
endpoint. A boolean result answers "does this patch apply / is this machine
compliant / should this policy target it / may this user install this". A
non-boolean result is inventory. Those are not four subsystems that sit near
each other; they are this file.

Deliberately NOT Turing complete: no loops, no assignment, no user-defined
functions, no attribute access, no indexing, no comprehensions. Evaluation is
bounded by node count, inspector-call count and wall clock. That is what makes
it safe to run every 60 seconds on every endpoint, and it is what limits the
blast radius if content publishing is ever compromised.

ponytail: syntax is a whitelisted subset of Python expressions, parsed by
ast.parse, so there is no hand-written lexer or parser to get wrong. Trade-off:
the syntax is Python's, not ours. Write a real Pratt parser only if the
language needs to diverge -- English-like `exists file "x"` phrasing would be
the trigger.

We walk the tree with our own evaluator rather than handing it to eval(). The
whitelist is a filter; this evaluator is the actual security boundary. Do not
"simplify" it back into eval().
"""
from __future__ import annotations

import ast
import re
import time

MAX_SOURCE = 4096       # chars; also keeps ast.parse away from its recursion limit
MAX_NODES = 500
MAX_INSPECTOR_CALLS = 64
MAX_SECONDS = 2.0

_CONSTANTS = {
    "true": True, "false": False, "none": None, "null": None,
    "True": True, "False": False, "None": None,
}


class RelevanceError(Exception):
    """Bad expression, or an expression that exceeded its budget."""


# --------------------------------------------------------------------------
# Version — because "138.0.7204.97" >= "139.0" must not be a string compare
# --------------------------------------------------------------------------
class Version:
    __slots__ = ("raw", "parts")

    def __init__(self, raw):
        self.raw = "" if raw is None else str(raw)
        self.parts = tuple(int(n) for n in re.findall(r"\d+", self.raw))

    def _pair(self, other):
        o = other if isinstance(other, Version) else Version(other)
        n = max(len(self.parts), len(o.parts))
        return (self.parts + (0,) * n)[:n], (o.parts + (0,) * n)[:n]

    def __eq__(self, other):
        a, b = self._pair(other)
        return a == b

    def __lt__(self, other):
        a, b = self._pair(other)
        return a < b

    def __le__(self, other):
        a, b = self._pair(other)
        return a <= b

    def __gt__(self, other):
        a, b = self._pair(other)
        return a > b

    def __ge__(self, other):
        a, b = self._pair(other)
        return a >= b

    def __hash__(self):
        return hash(self.parts)

    def __bool__(self):
        return bool(self.parts)

    def __repr__(self):
        return f"Version({self.raw!r})"

    def __str__(self):
        return self.raw


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------
_ALLOWED = (
    ast.Expression, ast.BoolOp, ast.UnaryOp, ast.Compare, ast.Call,
    ast.Name, ast.Constant, ast.Load,
    ast.And, ast.Or, ast.Not,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn,
)


def _validate(node):
    """Reject anything outside the whitelist, with a useful message."""
    count = 0
    for n in ast.walk(node):
        count += 1
        if count > MAX_NODES:
            raise RelevanceError(f"expression too complex (>{MAX_NODES} nodes)")
        if not isinstance(n, _ALLOWED):
            raise RelevanceError(f"{type(n).__name__} is not allowed in a relevance expression")
        if isinstance(n, ast.Call):
            if not isinstance(n.func, ast.Name):
                raise RelevanceError("only direct inspector calls are allowed")
            if n.keywords:
                raise RelevanceError("keyword arguments are not allowed")
        if isinstance(n, ast.Constant) and not isinstance(n.value, (str, int, float, bool, type(None))):
            raise RelevanceError(f"unsupported constant: {type(n.value).__name__}")
    return count


class Relevance:
    """A parsed, validated expression. Compile once, evaluate many times."""

    __slots__ = ("source", "_tree", "nodes")

    def __init__(self, source):
        if not isinstance(source, str):
            raise RelevanceError("relevance source must be a string")
        if len(source) > MAX_SOURCE:
            raise RelevanceError(f"expression too long (>{MAX_SOURCE} chars)")
        if not source.strip():
            raise RelevanceError("empty expression")
        try:
            tree = ast.parse(source, mode="eval")
        except SyntaxError as e:
            raise RelevanceError(f"syntax error: {e.msg}") from e
        self.nodes = _validate(tree)
        self.source = source
        self._tree = tree

    def evaluate(self, inspectors, *, deadline=None):
        """Evaluate against a {name: callable} inspector table.

        Raises RelevanceError on an unknown inspector, a budget overrun, or an
        inspector that itself raised. Callers decide what a failure means --
        see agent.py, which reports it rather than swallowing it.
        """
        ctx = _Ctx(inspectors, deadline if deadline is not None else time.monotonic() + MAX_SECONDS)
        return _eval(self._tree.body, ctx)

    def evaluate_bool(self, inspectors, *, deadline=None):
        return bool(self.evaluate(inspectors, deadline=deadline))

    def __repr__(self):
        return f"Relevance({self.source!r})"


class _Ctx:
    __slots__ = ("inspectors", "deadline", "calls")

    def __init__(self, inspectors, deadline):
        self.inspectors = inspectors
        self.deadline = deadline
        self.calls = 0

    def check(self):
        if time.monotonic() > self.deadline:
            raise RelevanceError(f"evaluation exceeded {MAX_SECONDS}s")


# --------------------------------------------------------------------------
# Evaluation
# --------------------------------------------------------------------------
def _compare(op, left, right):
    # A Version on either side wins: coerce so "1.10" > "1.9" is true.
    if isinstance(left, Version) or isinstance(right, Version):
        if not isinstance(left, Version):
            left = Version(left)
        if not isinstance(right, Version):
            right = Version(right)

    if isinstance(op, ast.Eq):
        return left == right
    if isinstance(op, ast.NotEq):
        return left != right
    if isinstance(op, (ast.In, ast.NotIn)):
        if not isinstance(right, (str, list, tuple, set, dict)):
            raise RelevanceError("'in' needs a string or a list on the right")
        hit = str(left) in right if isinstance(right, str) else left in right
        return hit if isinstance(op, ast.In) else not hit

    # Ordered comparisons on mismatched types raise TypeError in Python; that
    # is a content bug, so surface it rather than silently returning False.
    try:
        if isinstance(op, ast.Lt):
            return left < right
        if isinstance(op, ast.LtE):
            return left <= right
        if isinstance(op, ast.Gt):
            return left > right
        if isinstance(op, ast.GtE):
            return left >= right
    except TypeError as e:
        raise RelevanceError(f"cannot compare {type(left).__name__} with {type(right).__name__}") from e
    raise RelevanceError(f"unsupported comparison: {type(op).__name__}")


def _eval(node, ctx):
    ctx.check()

    if isinstance(node, ast.Constant):
        return node.value

    if isinstance(node, ast.Name):
        if node.id in _CONSTANTS:
            return _CONSTANTS[node.id]
        raise RelevanceError(f"unknown name: {node.id} (inspectors must be called, e.g. {node.id}())")

    if isinstance(node, ast.BoolOp):
        # Short-circuit, so `file_exists(p) and file_version(p) >= "2"` never
        # asks a missing file for its version.
        if isinstance(node.op, ast.And):
            result = True
            for v in node.values:
                result = _eval(v, ctx)
                if not result:
                    return result
            return result
        result = False
        for v in node.values:
            result = _eval(v, ctx)
            if result:
                return result
        return result

    if isinstance(node, ast.UnaryOp):
        if isinstance(node.op, ast.Not):
            return not _eval(node.operand, ctx)
        raise RelevanceError("unsupported unary operator")

    if isinstance(node, ast.Compare):
        left = _eval(node.left, ctx)
        for op, comparator in zip(node.ops, node.comparators):
            right = _eval(comparator, ctx)
            if not _compare(op, left, right):
                return False
            left = right
        return True

    if isinstance(node, ast.Call):
        name = node.func.id
        fn = ctx.inspectors.get(name)
        if fn is None:
            raise RelevanceError(f"unknown inspector: {name}")
        ctx.calls += 1
        if ctx.calls > MAX_INSPECTOR_CALLS:
            raise RelevanceError(f"too many inspector calls (>{MAX_INSPECTOR_CALLS})")
        args = [_eval(a, ctx) for a in node.args]
        try:
            return fn(*args)
        except RelevanceError:
            raise
        except TypeError as e:
            raise RelevanceError(f"{name}: bad arguments: {e}") from e
        except Exception as e:
            raise RelevanceError(f"{name} failed: {type(e).__name__}: {e}") from e

    raise RelevanceError(f"{type(node).__name__} is not evaluable")


def compile_relevance(source):
    """Convenience wrapper; the constructor is the real API."""
    return Relevance(source)
