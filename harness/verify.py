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

# Issue #62: the module is split by responsibility. The registry and
# record types live in verify_registry; the verifier families register
# on import, single-file checks first (see verifiers_files for why that
# order is the registry contract); clause phrasing reads live in
# verify_clauses; the attestation floor and run-level audits live in
# verify_deliverables. Every name callers used from here is re-exported.
from . import verifiers_files, verifiers_completeness, verifiers_records
from .tools import Jail, Refusal
from .verifiers_completeness import (
    _empty_list_detail, _missing_items_detail, _v_accounts_for, _v_indexes)
from .verifiers_files import (
    _v_contains, _v_contains_exactly, _v_exists, _v_has_lines, _v_not_exists)
from .verifiers_records import (
    _excludes_contents_leaks, _excludes_fields_leaks, _preserves_empty_detail,
    _v_excludes, _v_preserves, _values_for_field)
from .verify_clauses import (
    _ABSENCE_INTENT, _ABSENCE_PREDICATE, _COVERS_CLAUSE, _LEADING_QUOTED,
    _LEADING_SUBJECT, _PRESERVATION_PREDICATE, _QUOTED, _excluded_files_for,
    _excluded_from_step, _legacy_covers_target, _named_file, absence_intent,
    preservation_intent, then_subject_file)
from .verify_deliverables import (
    _accumulate_promised, _attest_or_floor, _check_verifier,
    _floor_missing_file, _trusted_deliverables, missing_deliverables,
    promised_deliverables, trust_only_steps)
from .verify_registry import (
    _FILE, _QPATH, _QTEXT, _SHOULD, VERIFIERS, CheckResult, StepVerify,
    Verifier, VerifyContext, _matched_check, match_verifier, parse_then,
    verifier)
from .verify_text import (
    _deferral_in, _field_matches, _harvest, _norm, _parse_fields, _pick,
    _record_text, _refs_in, _resolve, collect_step_items)


def verify_step(thens: list[str], verify_context: VerifyContext) -> StepVerify:
    """Evaluate each Then clause. Unmatched phrasings fall back to attestation
    (ok=True) — the harness only fails what it can actually check."""
    checks: list[CheckResult] = []
    for then in thens:
        then = (then or "").strip()
        if not then:
            continue
        result = _matched_check(then, verify_context)
        if result is None:
            result = _attest_or_floor(then, verify_context)
        checks.append(result)
    return StepVerify(ok=all(check_result.ok for check_result in checks), checks=checks)
