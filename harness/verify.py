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
    # v0.8.1: items produced by earlier steps (step id -> names extracted
    # from that step's listing-shaped tool results). Lets a verifier judge
    # a deliverable against the actual source data, not just the plan.
    prior_items: dict = field(default_factory=dict)
    # Issue #17 part 2: the covers step's Intent line. An exhaustive cue
    # here makes covers strict just as one in the task does.
    intent: str = ""


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


# Deferral language: a document that promises its own content instead of
# containing it. Scanned by the covers verifier (v0.8.1 field failure: a
# deliverable whose whole substance was "(Content to be filled in...)").
_DEFERRALS = [re.compile(p, re.IGNORECASE) for p in (
    r"to be filled in", r"to be completed", r"to be written",
    r"placeholder", r"lorem ipsum", r"content goes here",
    r"will be added", r"coming soon", r"\bTODO\b", r"\bTBD\b",
)]


def _deferral_in(text: str) -> str | None:
    for rx in _DEFERRALS:
        m = rx.search(text or "")
        if m:
            return m.group(0)
    return None


# Exhaustive cues for strict covers (issue #17, part 2): language that
# makes completeness, not sampling, the stated contract. Deliberately
# narrow, the phrases named in the issue: every file, all files, do not
# omit, each entry/file, no omissions (plus their closest variants).
# Summaries and strategy documents carry none of these and keep the
# at-least-half covers floor.
_EXHAUSTIVE_CUES = [re.compile(p, re.IGNORECASE) for p in (
    r"\bevery\s+(?:file|entry|document|paper|record)s?\b",
    r"\ball\s+(?:the\s+)?(?:file|entry|document|paper|record)s\b",
    r"\bdo\s+not\s+omit\b", r"\bdon['’]t\s+omit\b",
    r"\bwithout\s+omitting\b", r"\bomit(?:s|ting)?\s+none\b",
    r"\bno\s+omissions?\b", r"\bnothing\s+(?:is\s+)?omitted\b",
    r"\beach\s+(?:entry|file|document|paper|record)\b",
)]


def _strict_covers(ctx: VerifyContext) -> bool:
    """True when the task or the covers step's Intent states a
    completeness contract, so covers must require every source item."""
    text = f"{ctx.task or ''}\n{ctx.intent or ''}"
    return any(rx.search(text) for rx in _EXHAUSTIVE_CUES)


def collect_step_items(sub_steps) -> list[str]:
    """Extract the item names a step produced, from listing-shaped tool
    results: list_dir and grep_files outputs (one workspace-relative name
    per line) and `ls`/`find` exec output. Other tools' results are prose
    or content, not name lists, and are never mined. Deterministic, deduped,
    order preserved."""
    items: list[str] = []
    for s in sub_steps:
        if not s.tool or s.result is None:
            continue
        lines = None
        if s.tool in ("list_dir", "grep_files"):
            lines = s.result.splitlines()
        elif s.tool == "exec":
            cmd = str((s.args or {}).get("command", ""))
            if re.match(r"\s*(ls|find)\b", cmd):
                lines = [ln for ln in s.result.splitlines()
                         if not ln.startswith("exit=")]
        if lines is None:
            continue
        for ln in lines:
            name = ln.strip().rstrip("/")
            if not name or name in ("(empty)", "(no matches)"):
                continue
            if name.startswith("...[") or name.startswith("exit="):
                continue
            if name not in items:
                items.append(name)
    return items


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


@verifier("covers",
          # Phrasing tolerance (field, 2026-10-03): the planner produced
          # 'the contents of "organization.md" cover the files from step 1'
          # live: an optional contents-of prefix and cover/covers both
          # match. The anchors stay strict: a real path, "the files", and
          # a step reference, whole-clause.
          rf"""^(?:the\s+contents?\s+of\s+|contents?\s+of\s+)?{_FILE}{_QPATH}\s+covers?\s+the\s+files\s+"""
          rf"""(?:from|listed\s+(?:in|by))\s+step\s+(\d+)\s*[.!]?\s*$""")
def _v_covers(m: re.Match, ctx: VerifyContext) -> CheckResult:
    rel = _pick(*m.groups()[0:4])
    step_no = int(m.group(5))
    then = m.string.strip()
    p, err = _resolve(ctx, rel)
    if err:
        return CheckResult(then, False, "covers", err)
    items = list(ctx.prior_items.get(step_no) or [])
    if not items:
        # Nothing recorded to measure against: the harness only fails what
        # it can actually check, and says plainly that it did not check.
        return CheckResult(then, True, "covers",
                           f"step {step_no} produced no file list — "
                           f"coverage not checked")
    if not p.is_file():
        return CheckResult(then, False, "covers",
                           f"{rel!r} is not a file — expected it to cover "
                           f"the {len(items)} files from step {step_no}")
    content = p.read_text(errors="replace")
    bad = _deferral_in(content)
    if bad:
        return CheckResult(then, False, "covers",
                           f"{rel!r} contains a deferral ({bad!r}) — the "
                           f"document must be finished, not promised")
    covered = [i for i in items if i in content]
    strict = _strict_covers(ctx)
    # Issue #17 part 2: an exhaustive task or Intent makes completeness
    # the contract, so every source item must be mentioned. Otherwise
    # the floor stays at least half, for summaries and strategy docs.
    need = len(items) if strict else max(1, -(-len(items) // 2))
    if len(covered) >= need:
        return CheckResult(then, True, "covers",
                           f"{rel!r} mentions {len(covered)} of {len(items)} "
                           f"files from step {step_no}")
    missing = [i for i in items if i not in content]
    need_text = (f"exhaustive task: needs all {need}" if strict
                 else f"needs at least {need}")
    detail = (f"{rel!r} mentions {len(covered)} of {len(items)} files from "
              f"step {step_no} ({need_text}); missing include: "
              + ", ".join(missing[:12]))
    if len(missing) > 12:
        detail += (f" and {len(missing) - 12} more "
                   f"(see step {step_no}'s stored output)")
    return CheckResult(then, False, "covers", detail)


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
