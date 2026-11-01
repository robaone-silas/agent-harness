"""Verifier registry for Then verification (issue #62).

Split out of verify.py: the record types, the registry and its
decorator, the shared phrasing fragments, and the pure phrasing
lookups (parse_then, match_verifier). The verifier families
register themselves by importing this module; verify.py imports
them in registry order and re-exports these names, so existing
imports keep working.
"""
from __future__ import annotations

import re as regular_expression
from dataclasses import dataclass, field
from typing import Callable

from .tools import Jail
from .verify_text import _pick


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
    # Issue #47 option 1: files excluded for each deliverable by
    # contents-of excludes clauses on the current or earlier steps.
    # _v_indexes and _v_accounts_for subtract the set for the
    # clause's target from the expected set. Defaults to empty, so
    # contexts built without it behave exactly as before.
    excluded_files: dict = field(default_factory=dict)


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
    pattern: regular_expression.Pattern
    check: Callable[[regular_expression.Match, VerifyContext], CheckResult]


VERIFIERS: list[Verifier] = []


def verifier(name: str, pattern: str):
    """Register a verifier. The pattern's groups feed the check function."""
    def decorator(verify_function: Callable[[regular_expression.Match, VerifyContext], CheckResult]) -> Verifier:
        compiled_pattern = regular_expression.compile(pattern, regular_expression.IGNORECASE)
        registered_verifier = Verifier(name, compiled_pattern, verify_function)
        VERIFIERS.append(registered_verifier)
        return registered_verifier
    return decorator


# A quoted string with matching quotes: "text", 'text', `text` (3 groups).
_QTEXT = r'''(?:"([^"]*)"|'([^']*)'|`([^`]*)`)'''
# A path: quoted anything, or a bare token without whitespace (4 groups).
_QPATH = r'''(?:"([^"]*)"|'([^']*)'|`([^`]*)`|([\w./\\-]+))'''
_FILE = r"""(?:the\s+file\s+)?"""
_SHOULD = r"""(?:should\s+)?"""

_VALUE_BEARING_VERIFIERS = ("contains", "contains_exactly")


def _first_match(then: str) -> tuple[Verifier, regular_expression.Match] | None:
    """The first registered verifier whose pattern matches the Then,
    with its match, or None (the attestation fallback)."""
    for registered_verifier in VERIFIERS:
        match = registered_verifier.pattern.match(then)
        if match:
            return registered_verifier, match
    return None


def match_verifier(then: str) -> str | None:
    """Name of the verifier handling this Then, or None (attestation fallback).
    Used for the approval badge; matching never touches the workspace."""
    parsed = parse_then(then)
    return parsed[0] if parsed else None


def parse_then(then: str) -> tuple[str, str | None, str | None] | None:
    """Parse a Then into (verifier_name, path, expected_value).

    Returns None when no verifier handles the phrasing (attestation fallback).
    `path` is the file the check targets; `expected_value` is the asserted
    text for contains/contains_exactly, else None. Pure phrasing — never
    touches the workspace. Used by the plan lint and the approval badges.
    """
    found = _first_match((then or "").strip())
    if found is None:
        return None
    registered_verifier, match = found
    groups = match.groups()
    path = _pick(*groups[0:4])
    value = None
    if registered_verifier.name in _VALUE_BEARING_VERIFIERS:
        value = _pick(*groups[4:7])
    return registered_verifier.name, path, value


def _matched_check(then: str, verify_context: VerifyContext) -> CheckResult | None:
    """The first verifier's result for a Then, or None when no
    verifier handles the phrasing (the attestation fallback)."""
    found = _first_match(then)
    if found is None:
        return None
    registered_verifier, match = found
    return registered_verifier.check(match, verify_context)
