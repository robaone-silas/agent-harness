"""Attestation floor and deliverable audits (issue #62).

Split out of verify.py: what the harness can still check when no
verifier matched a clause (the issue #22 existence floor), and the
run-level audits over a whole plan's Thens. promised_deliverables
and missing_deliverables back the issue #22 deliverable audit;
trust_only_steps backs the issue #37 done-on-trust status. verify.py
re-exports these names, so existing imports keep working.
"""
from __future__ import annotations

from .tools import Jail, Refusal
from .verify_clauses import _clauses, _is_absence_clause, _named_file
from .verify_registry import CheckResult, VerifyContext, parse_then
from .verify_text import _resolve


def _floor_missing_file(then: str, verify_context: VerifyContext) -> CheckResult | None:
    """The issue #22 existence floor for one clause: a failure when
    the clause names a quoted file that does not exist, else None.

    Absence-intent clauses are exempt (there the missing file is the
    success state), but a recognized retired covers clause is never
    absence intent: it promises its target exists, even when the
    filename itself contains an absence word."""
    named_file = _named_file(then)
    if named_file is None or _is_absence_clause(then):
        return None
    path, error = _resolve(verify_context, named_file)
    if error or path is None or path.exists():
        return None
    return CheckResult(
        then, False, "attest",
        f"named file {named_file!r} does not exist — a clause "
        f"about a file cannot pass on trust while the file "
        f"is missing; create {named_file!r} with the promised "
        f"content")


def _attest_or_floor(then: str, verify_context: VerifyContext) -> CheckResult:
    """The attestation fallback, with the issue #22 existence floor.

    Field failure (Daniel Okafor, 2026-10-04): a prose contains
    clause attested ok while the file it named had never been
    created, and the run reported done with no deliverable. The
    asserted *content* may be prose no verifier can match, but the
    named file's *existence* is always machine-checkable, so a
    clause naming a quoted file cannot pass on trust while that
    file is missing. The failure feeds the step's normal retry with
    the exact missing path. Absence-intent clauses are exempt:
    there the missing file is the success state. A recognized
    retired covers clause is never absence intent: it promises
    its target exists, even when the filename itself contains an
    absence word."""
    floor = _floor_missing_file(then, verify_context)
    if floor is not None:
        return floor
    return CheckResult(then, True, "attest", "no verifier matched — taking the model's word")


def _clause_promise(clause: str) -> tuple[str, bool] | None:
    """(file, is_absence) for the file a clause promises or rules
    out, or None when it names no file.

    A parsed clause's subject is its verifier's target, ruled out
    only by not_exists; a prose clause's subject is a quoted file it
    names, ruled out by absence-intent wording. A retired covers
    clause counts as a positive promise, never absence intent."""
    parsed = parse_then(clause)
    if parsed:
        verb, path, _value = parsed
        return (path, verb == "not_exists") if path is not None else None
    named_file = _named_file(clause)
    if named_file is None:
        return None
    return named_file, _is_absence_clause(clause)


def _accumulate_promised(clause: str, promised: list[str], excluded: set[str]) -> None:
    """Fold one Then clause into the promised/excluded sets.

    not_exists (or absence-intent prose) excludes; anything else
    promises, unless already promised."""
    promise = _clause_promise(clause.strip()) if clause.strip() else None
    if promise is None:
        return
    path, is_absence = promise
    if is_absence:
        excluded.add(path)
    elif path not in promised:
        promised.append(path)


def promised_deliverables(steps) -> list[str]:
    """The files a plan promises will exist when the run ends: the
    subject file of every exists / covers / indexes / contains-family
    Then, plus any quoted file a prose Then names, minus anything a
    not_exists (or absence-intent) Then names. A retired covers
    clause counts as a positive promise, never absence intent.
    Derived from the Thens alone, sorted for determinism. Issue
    #22: this set is the run's contract with the user, audited at
    the end of the run."""
    promised: list[str] = []
    excluded: set[str] = set()
    for step in steps:
        for clause in (step.done_when or "").split("\n"):
            _accumulate_promised(clause, promised, excluded)
    return sorted(relative_path for relative_path in promised if relative_path not in excluded)


def missing_deliverables(steps, jail: Jail) -> list[str]:
    """Promised deliverables that do not exist in the workspace.
    Paths escaping the jail cannot be judged and are skipped."""
    missing: list[str] = []
    for relative_path in promised_deliverables(steps):
        try:
            resolved_path = jail.resolve(relative_path)
        except Refusal:
            continue
        if not resolved_path.exists():
            missing.append(relative_path)
    return missing


def _check_verifier(check) -> str | None:
    """The verifier name recorded for a check, dict or CheckResult."""
    if isinstance(check, dict):
        return check.get("verifier")
    return getattr(check, "verifier", None)


def _finished_on_attestation_alone(step) -> bool:
    """A step that finished done, unwaived, with every recorded check
    the attestation fallback."""
    if getattr(step, "status", None) != "done":
        return False
    if getattr(step, "verify_waived", False):
        return False
    checks = getattr(step, "verify", None) or []
    return bool(checks) and all(_check_verifier(check) == "attest" for check in checks)


def _clause_named_file(clause: str) -> str | None:
    parsed = parse_then(clause)
    if parsed and parsed[1]:
        return parsed[1]
    return _named_file(clause)


def _trusted_deliverables(step, promised: set[str]) -> list[str]:
    """Promised deliverables one finished step rests entirely on
    trust for, else [].

    A step counts when it finished done, its recorded checks are
    all the attestation fallback, and at least one file its Thens
    name is a promised deliverable. A prose Then on a read step
    names no promised file and does not taint the run. A waived
    step keeps its own separate flag and never counts here, and a
    failed step cannot taint a run that never completed."""
    if not _finished_on_attestation_alone(step):
        return []
    named = {_clause_named_file(clause) for clause in _clauses(step)}
    return sorted(deliverable for deliverable in named if deliverable in promised)


def trust_only_steps(steps) -> list[dict]:
    """Steps whose deliverable rests entirely on trust (issue #37).

    A step counts when it finished done, its recorded checks are all
    the attestation fallback, and at least one file its Thens name is
    a promised deliverable. A prose Then on a read step names no
    promised file and does not taint the run. A waived step keeps its
    own separate flag and never counts here, and a failed step cannot
    taint a run that never completed. Returns plan-ordered
    [{"step": id, "deliverables": [names]}]. Pure phrasing over the
    recorded checks; never touches the workspace."""
    promised = set(promised_deliverables(steps))
    out: list[dict] = []
    for step in steps:
        deliverables = _trusted_deliverables(step, promised)
        if deliverables:
            out.append({"step": step.id, "deliverables": deliverables})
    return out
