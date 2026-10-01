"""v0.7: per-step Then verification.

After a plan step's sub-run reports DONE, the harness evaluates each Then
clause against the workspace before advancing. Verifiers live in a registry:
each one declares the Then phrasings it handles (a regex) plus a check
function, and receives the full step context (jail, step id, task). Adding a
verifier never touches the engine.

A Then with no matching verifier falls back to model attestation (pre-v0.7
behavior) — fail open, never block on what we can't parse. Verification
itself costs zero model calls; only a failed check triggers a step retry.

Registry discipline (the expandability contract):
- one verifier = one phrasing family, independently unit-testable;
- order matters: more specific phrasings (not_exists, contains_exactly)
  register before their general siblings, because lazy groups backtrack
  and a general pattern will otherwise swallow the specific one;
- check functions get the whole context, so new *kinds* of checks don't
  need engine changes;
- a buggy verifier is worse than none: test each against the robust
  solution, the gaming solution, and the broken one before it goes live.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

from .tools import Jail, Refusal


@dataclass(frozen=True)
class VerifyContext:
    jail: Jail       # path resolution; Refusal on workspace escape
    step_id: int
    task: str


@dataclass
class CheckResult:
    then: str
    ok: bool
    verifier: str    # verifier name, or "attest" for the fallback
    detail: str      # human-readable: what was checked / why it failed


@dataclass
class StepVerify:
    ok: bool
    checks: list[CheckResult] = field(default_factory=list)


@dataclass
class Verifier:
    name: str
    pattern: re.Pattern
    check: Callable[[re.Match, VerifyContext], CheckResult]


VERIFIERS: list[Verifier] = []


def verifier(name: str, pattern: str):
    """Register a verifier. The pattern's groups feed the check function."""
    def deco(fn: Callable[[re.Match, VerifyContext], CheckResult]) -> Verifier:
        v = Verifier(name, re.compile(pattern, re.IGNORECASE), fn)
        VERIFIERS.append(v)
        return v
    return deco


def _pick(*groups: str | None) -> str | None:
    """First non-None regex group (quoted alternatives are mutually exclusive)."""
    return next((g for g in groups if g is not None), None)


def _resolve(ctx: VerifyContext, rel: str | None) -> tuple[object | None, str]:
    """Resolve rel inside the jail. Returns (path, '') or (None, error)."""
    try:
        return ctx.jail.resolve((rel or "").strip()), ""
    except Refusal as e:
        return None, str(e)


# A quoted string with matching quotes: "text", 'text', `text` (3 groups).
_QTEXT = r'''(?:"([^"]*)"|'([^']*)'|`([^`]*)`)'''
# A path: quoted anything, or a bare token without whitespace (4 groups).
_QPATH = r'''(?:"([^"]*)"|'([^']*)'|`([^`]*)`|([\w./\\-]+))'''
_FILE = r"""(?:the\s+file\s+)?"""
_SHOULD = r"""(?:should\s+)?"""


@verifier("contains_exactly",
          rf"""^{_FILE}{_QPATH}\s+{_SHOULD}contains?\s+"""
          rf"""(?:exactly|the\s+exact\s+content)\s+{_QTEXT}\s*$""")
def _v_contains_exactly(m: re.Match, ctx: VerifyContext) -> CheckResult:
    rel = _pick(*m.groups()[0:4])
    text = _pick(*m.groups()[4:7])
    then = m.string.strip()
    p, err = _resolve(ctx, rel)
    if err:
        return CheckResult(then, False, "contains_exactly", err)
    if not p.is_file():
        return CheckResult(then, False, "contains_exactly",
                           f"{rel!r} is not a file — expected exactly {text!r}")
    got = p.read_text().strip()
    if got == text:
        return CheckResult(then, True, "contains_exactly",
                           f"{rel!r} contains exactly {text!r}")
    return CheckResult(then, False, "contains_exactly",
                       f"{rel!r}: got {got!r}, expected exactly {text!r}")


@verifier("has_lines",
          rf"""^{_FILE}{_QPATH}\s+{_SHOULD}(?:has|contains?)\s+"""
          rf"""(\d+)\s+lines?\s*[.!]?\s*$""")
def _v_has_lines(m: re.Match, ctx: VerifyContext) -> CheckResult:
    rel = _pick(*m.groups()[0:4])
    n = int(m.group(5))
    then = m.string.strip()
    p, err = _resolve(ctx, rel)
    if err:
        return CheckResult(then, False, "has_lines", err)
    if not p.is_file():
        return CheckResult(then, False, "has_lines",
                           f"{rel!r} is not a file — expected {n} lines")
    got = len(p.read_text().splitlines())
    if got == n:
        return CheckResult(then, True, "has_lines", f"{rel!r} has {n} lines")
    return CheckResult(then, False, "has_lines",
                       f"{rel!r} has {got} lines, expected {n}")


@verifier("contains",
          rf"""^{_FILE}{_QPATH}\s+{_SHOULD}contains?\s+"""
          rf"""(?!exactly\b|the\s+exact\s+content\b){_QTEXT}\s*$""")
def _v_contains(m: re.Match, ctx: VerifyContext) -> CheckResult:
    rel = _pick(*m.groups()[0:4])
    text = _pick(*m.groups()[4:7])
    then = m.string.strip()
    p, err = _resolve(ctx, rel)
    if err:
        return CheckResult(then, False, "contains", err)
    if not p.is_file():
        return CheckResult(then, False, "contains",
                           f"{rel!r} is not a file — expected it to contain {text!r}")
    if text in p.read_text():
        return CheckResult(then, True, "contains", f"{rel!r} contains {text!r}")
    return CheckResult(then, False, "contains",
                       f"{rel!r} does not contain {text!r}")


@verifier("not_exists",
          rf"""^{_FILE}{_QPATH}\s+{_SHOULD}(?:does\s+)?not\s+exist\s*[.!]?\s*$""")
def _v_not_exists(m: re.Match, ctx: VerifyContext) -> CheckResult:
    rel = _pick(*m.groups()[0:4])
    then = m.string.strip()
    p, err = _resolve(ctx, rel)
    if err:
        return CheckResult(then, False, "not_exists", err)
    if not p.exists():
        return CheckResult(then, True, "not_exists", f"{rel!r} does not exist")
    return CheckResult(then, False, "not_exists",
                       f"{rel!r} exists — expected it not to")


@verifier("exists",
          rf"""^{_FILE}{_QPATH}\s+{_SHOULD}exists?\s*[.!]?\s*$""")
def _v_exists(m: re.Match, ctx: VerifyContext) -> CheckResult:
    rel = _pick(*m.groups()[0:4])
    then = m.string.strip()
    p, err = _resolve(ctx, rel)
    if err:
        return CheckResult(then, False, "exists", err)
    if p.exists():
        return CheckResult(then, True, "exists", f"{rel!r} exists")
    return CheckResult(then, False, "exists",
                       f"{rel!r} MISSING — expected it to exist")


def match_verifier(then: str) -> str | None:
    """Name of the verifier handling this Then, or None (attestation fallback).
    Used for the approval badge; matching never touches the workspace."""
    p = parse_then(then)
    return p[0] if p else None


def parse_then(then: str) -> tuple[str, str | None, str | None] | None:
    """Parse a Then into (verifier_name, path, expected_value).

    Returns None when no verifier handles the phrasing (attestation fallback).
    `path` is the file the check targets; `expected_value` is the asserted
    text for contains/contains_exactly, else None. Pure phrasing — never
    touches the workspace. Used by the plan lint and the approval badges.
    """
    then = (then or "").strip()
    for v in VERIFIERS:
        m = v.pattern.match(then)
        if m:
            g = m.groups()
            path = _pick(*g[0:4])
            value = _pick(*g[4:7]) if v.name in ("contains", "contains_exactly") else None
            return v.name, path, value
    return None


def verify_step(thens: list[str], ctx: VerifyContext) -> StepVerify:
    """Evaluate each Then clause. Unmatched phrasings fall back to attestation
    (ok=True) — the harness only fails what it can actually check."""
    checks: list[CheckResult] = []
    for then in thens:
        then = (then or "").strip()
        if not then:
            continue
        result = None
        for v in VERIFIERS:
            m = v.pattern.match(then)
            if m:
                result = v.check(m, ctx)
                break
        if result is None:
            result = CheckResult(then, True, "attest",
                                 "no verifier matched — taking the model's word")
        checks.append(result)
    return StepVerify(ok=all(c.ok for c in checks), checks=checks)
