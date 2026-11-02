"""Record verifiers: preserves and excludes (issue #62).

Split out of verify.py. Both read the Label: value pairs earlier
steps' stored records carry: preserves checks every harvested value
survived into the deliverable, excludes checks none of the guarded
values leaked into it.
"""
from __future__ import annotations

import re as regular_expression

from .verify_registry import _FILE, _QPATH, _QTEXT, CheckResult, VerifyContext, verifier
from .verify_text import (_field_matches, _harvest, _norm, _parse_fields, _pick,
                          _record_text, _refs_in, _resolve)

# --- preserves (issue #37): field-level preservation across steps ---
#
# A read-then-compile step's real contract is that the fields the
# source records carry survive into the deliverable. The planner
# cannot state that with contains (it plans before reading, so it
# cannot quote the values) and covers only tracks file names. The
# preserves clause names FIELDS instead; the harness harvests
# Label: value pairs from the named steps' stored records (the
# records embed what those steps actually read) and checks that
# every harvested value for a named field appears in the deliverable.


def _source_refs(refs_text: str, verify_context: VerifyContext) -> list[int]:
    """Step numbers a clause draws on, minus the current step."""
    return [step_id for step_id in _refs_in(refs_text) if step_id != verify_context.step_id]


def _values_for_field(field: str, refs: list[int],
                      records: dict) -> list[tuple[int, str]]:
    """Distinct (step, value) pairs the source records carry for one
    field: every harvested Label: value pair whose label matches the
    field, deduped per step."""
    values: list[tuple[int, str]] = []
    for referenced_step_id in refs:
        for label, value in _harvest(records.get(referenced_step_id) or ""):
            pair = (referenced_step_id, value)
            if _field_matches(field, label) and pair not in values:
                values.append(pair)
    return values


def _preserves_empty_detail(fields: list[str], refs: list[int]) -> str:
    """Not-checked detail for a preserves clause naming nothing."""
    if not fields and not refs:
        return "the clause names no fields and no source steps: preservation not checked"
    if not fields:
        return "the clause names no fields: preservation not checked"
    return "the clause names no source steps: preservation not checked"


def _preserves_tally(fields: list[str], refs: list[int], records: dict,
                     content: str) -> tuple[int, list[tuple[str, int, str]], list[str]]:
    """(values checked, missing (field, step, value), unchecked fields)."""
    total = 0
    missing: list[tuple[str, int, str]] = []
    unchecked: list[str] = []
    for field in fields:
        values = _values_for_field(field, refs, records)
        if not values:
            unchecked.append(field)
            continue
        total += len(values)
        missing.extend((field, step_id, value) for step_id, value in values
                       if _norm(value) not in content)
    return total, missing, unchecked


def _shown_missing_values(missing: list[tuple[str, int, str]]) -> str:
    shown_values = [f"{field_name} {value!r} (step {step_id})" for field_name, step_id, value in missing[:8]]
    shown = "; ".join(shown_values)
    if len(missing) > 8:
        shown += f"; and {len(missing) - 8} more"
    return shown


def _preserves_outcome(relative_path: str, fields: list[str], ref_list: str,
                       tally: tuple[int, list, list]) -> tuple[bool, str]:
    """(ok, detail) for a preserves clause from its tally."""
    total, missing, unchecked = tally
    note = ""
    if unchecked:
        note = f"; field(s) {', '.join(unchecked)} never appear in the source records — not checked"
    if total == 0:
        return True, (f"no labeled values for {', '.join(fields)} found in the "
                      f"records of step(s) {ref_list} — preservation not checked")
    if missing:
        return False, (f"{relative_path!r} is missing values the source steps "
                       f"read: {_shown_missing_values(missing)}{note}")
    return True, (f"{relative_path!r} preserves all {total} values of "
                  f"{', '.join(fields)} read in step(s) {ref_list}{note}")


@verifier("preserves",
          rf"""^{_FILE}{_QPATH}\s+preserves?\s+(.+?)\s+from\s+(.+?)\s*[.!]?\s*$""")
def _v_preserves(match: regular_expression.Match, verify_context: VerifyContext) -> CheckResult:
    """Field preservation: every value the named source steps read
    for each named field must appear in the deliverable. Strict, like
    indexes: a compilation that drops one customer's status has not
    preserved the records. A field whose label never appears in the
    source records cannot be checked and is reported as such, the
    same honesty rule as covers with no measurable source list."""
    relative_path = _pick(*match.groups()[0:4])
    fields = _parse_fields(match.group(5))
    refs = _source_refs(match.group(6), verify_context)
    then = match.string.strip()
    if not fields or not refs:
        return CheckResult(then, True, "preserves", _preserves_empty_detail(fields, refs))
    resolved_path, error = _resolve(verify_context, relative_path)
    if error:
        return CheckResult(then, False, "preserves", error)
    ref_list = ", ".join(str(step_id) for step_id in refs)
    if not resolved_path.is_file():
        detail = f"{relative_path!r} is not a file — expected it to preserve {', '.join(fields)} from step(s) {ref_list}"
        return CheckResult(then, False, "preserves", detail)
    content = _norm(resolved_path.read_text(errors="replace"))
    records = {step_id: _record_text(verify_context, step_id) for step_id in refs}
    tally = _preserves_tally(fields, refs, records, content)
    ok, detail = _preserves_outcome(relative_path, fields, ref_list, tally)
    return CheckResult(then, ok, "preserves", detail)


# --- excludes: the negative contract, counterpart to preserves ---
#
# Some deliverables must NOT carry what the earlier steps read:
# prices or internal notes leaking into a customer facing summary.
# No positive clause can state that. excludes names the absence in
# three forms: fields harvested from source steps' records (checked
# in reverse, the preserves machinery), the contents of a named
# file (its labeled values and distinctive lines), and a quoted
# literal. The honesty rules mirror the rest of the registry: when
# nothing can be harvested to check against, the clause passes as
# not checked and says so; a missing deliverable fails, because the
# step promised a scrubbed file that is not there.

def _guarded_source_content(source: str) -> list[str]:
    """A source file's harvested labeled values plus its distinctive
    lines (12 characters or more)."""
    guarded = [value for _label, value in _harvest(source)]
    for line in source.splitlines():
        line = regular_expression.sub(r"\s+", " ", line.strip())
        if len(line) >= 12 and line not in guarded:
            guarded.append(line)
    return guarded


def _excludes_contents_leaks(verify_context: VerifyContext,
                             contents_relative_path: str,
                             content: str) -> tuple[list[str], str, str | None]:
    """Leaks for the contents-of form: (leaks, basis, not-checked).

    The guarded set is the source file's harvested labeled values
    plus its distinctive lines (12 characters or more). A missing
    source file, or a source holding nothing checkable, is not a
    failure: the clause passes as not checked with the reason."""
    resolved_path, error = _resolve(verify_context, contents_relative_path)
    if error or resolved_path is None or not resolved_path.is_file():
        return [], "", f"source file {contents_relative_path!r} not found — exclusion not checked"
    guarded = _guarded_source_content(resolved_path.read_text(errors="replace"))
    if not guarded:
        return [], "", f"{contents_relative_path!r} holds nothing checkable — exclusion not checked"
    leaks = [value for value in guarded if _norm(value) in content]
    return leaks, f"content from {contents_relative_path!r}", None


def _guarded_field_values(verify_context: VerifyContext, fields: list[str],
                          refs: list[int]) -> list[str]:
    """Every distinct labeled value the source steps read for a named field."""
    guarded: list[str] = []
    for referenced_step_id in refs:
        record = _record_text(verify_context, referenced_step_id) or ""
        for label, value in _harvest(record):
            named = any(_field_matches(field, label) for field in fields)
            if named and value not in guarded:
                guarded.append(value)
    return guarded


def _excludes_fields_leaks(verify_context: VerifyContext, fields_text: str,
                           refs_text: str,
                           content: str) -> tuple[list[str], str, str | None]:
    """Leaks for the fields-from-steps form: (leaks, basis, not-checked).

    The guarded set is every labeled value the named source steps
    read for a named field. A clause naming no fields or no source
    steps, or one whose fields yield no values, passes as not
    checked with the reason."""
    fields = _parse_fields(fields_text)
    refs = _source_refs(refs_text, verify_context)
    if not fields or not refs:
        return [], "", "the clause names no fields or no source steps — exclusion not checked"
    ref_list = ", ".join(str(step_id) for step_id in refs)
    guarded = _guarded_field_values(verify_context, fields, refs)
    if not guarded:
        return [], "", (f"no labeled values for {', '.join(fields)} found in the "
                        f"records of step(s) {ref_list} — exclusion not checked")
    leaks = [value for value in guarded if _norm(value) in content]
    return leaks, f"{', '.join(fields)} from step(s) {ref_list}", None


def _excludes_literal_leaks(literal: str, content: str) -> tuple[list[str], str, str | None]:
    leaks = [literal] if _norm(literal) in content else []
    return leaks, f"the text {literal!r}", None


def _excludes_leaks(match: regular_expression.Match, verify_context: VerifyContext,
                    content: str) -> tuple[list[str], str, str | None]:
    """Dispatch to the excludes form the clause was written in."""
    groups = match.groups()
    contents_relative_path = _pick(*groups[4:8])
    if contents_relative_path is not None:
        return _excludes_contents_leaks(verify_context, contents_relative_path, content)
    literal = _pick(*groups[8:11])
    if literal is not None:
        return _excludes_literal_leaks(literal, content)
    return _excludes_fields_leaks(verify_context, groups[11] or "", groups[12] or "", content)


def _excludes_outcome(relative_path: str, leaks: list[str], basis: str) -> tuple[bool, str]:
    if not leaks:
        return True, f"{relative_path!r} excludes {basis}"
    shown = "; ".join(repr(value) for value in leaks[:8])
    if len(leaks) > 8:
        shown += f"; and {len(leaks) - 8} more"
    return False, f"{relative_path!r} must exclude {basis}, but it contains: {shown}"


@verifier("excludes",
          rf"""^{_FILE}{_QPATH}\s+excludes?\s+"""
          rf"""(?:the\s+contents\s+of\s+{_QPATH}|{_QTEXT}|"""
          rf"""(.+?)\s+from\s+(.+?))\s*[.!]?\s*$""")
def _v_excludes(match: regular_expression.Match, verify_context: VerifyContext) -> CheckResult:
    relative_path = _pick(*match.groups()[0:4])
    then = match.string.strip()
    resolved_path, error = _resolve(verify_context, relative_path)
    if error:
        return CheckResult(then, False, "excludes", error)
    if not resolved_path.is_file():
        detail = f"{relative_path!r} is not a file — expected a deliverable that excludes the named content"
        return CheckResult(then, False, "excludes", detail)
    content = _norm(resolved_path.read_text(errors="replace"))
    leaks, basis, not_checked = _excludes_leaks(match, verify_context, content)
    if not_checked is not None:
        return CheckResult(then, True, "excludes", not_checked)
    ok, detail = _excludes_outcome(relative_path, leaks, basis)
    return CheckResult(then, ok, "excludes", detail)
