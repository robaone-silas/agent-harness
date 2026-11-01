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

import re as regular_expression
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
        verifier = Verifier(name, regular_expression.compile(pattern, regular_expression.IGNORECASE), verify_function)
        VERIFIERS.append(verifier)
        return verifier
    return decorator


def _pick(*groups: str | None) -> str | None:
    """First non-None regex group (quoted alternatives are mutually exclusive)."""
    return next((g for g in groups if g is not None), None)


def _resolve(verify_context: VerifyContext, relative_path: str | None) -> tuple[object | None, str]:
    """Resolve a relative path inside the jail. Returns (path, '') or (None, error)."""
    try:
        return verify_context.jail.resolve((relative_path or "").strip()), ""
    except Refusal as e:
        return None, str(e)


# Deferral language: a document that promises its own content instead of
# containing it. Scanned by the covers verifier (v0.8.1 field failure: a
# deliverable whose whole substance was "(Content to be filled in...)").
_DEFERRALS = [regular_expression.compile(pattern, regular_expression.IGNORECASE) for pattern in (
    r"to be filled in", r"to be completed", r"to be written",
    r"placeholder", r"lorem ipsum", r"content goes here",
    r"will be added", r"coming soon", r"\bTODO\b", r"\bTBD\b",
)]


def _deferral_in(text: str) -> str | None:
    for deferral_pattern in _DEFERRALS:
        match = deferral_pattern.search(text or "")
        if match:
            return match.group(0)
    return None


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
            if regular_expression.match(r"\s*(ls|find)\b", cmd):
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
def _v_contains_exactly(match: regular_expression.Match, verify_context: VerifyContext) -> CheckResult:
    relative_path = _pick(*match.groups()[0:4])
    text = _pick(*match.groups()[4:7])
    then = match.string.strip()
    resolved_path, error = _resolve(verify_context, relative_path)
    if error:
        return CheckResult(then, False, "contains_exactly", error)
    if not resolved_path.is_file():
        return CheckResult(then, False, "contains_exactly",
                           f"{relative_path!r} is not a file — expected exactly {text!r}")
    got = resolved_path.read_text().strip()
    if got == text:
        return CheckResult(then, True, "contains_exactly",
                           f"{relative_path!r} contains exactly {text!r}")
    return CheckResult(then, False, "contains_exactly",
                       f"{relative_path!r}: got {got!r}, expected exactly {text!r}")


@verifier("has_lines",
          rf"""^{_FILE}{_QPATH}\s+{_SHOULD}(?:has|contains?)\s+"""
          rf"""(\d+)\s+lines?\s*[.!]?\s*$""")
def _v_has_lines(match: regular_expression.Match, verify_context: VerifyContext) -> CheckResult:
    relative_path = _pick(*match.groups()[0:4])
    expected_line_count = int(match.group(5))
    then = match.string.strip()
    resolved_path, error = _resolve(verify_context, relative_path)
    if error:
        return CheckResult(then, False, "has_lines", error)
    if not resolved_path.is_file():
        return CheckResult(then, False, "has_lines",
                           f"{relative_path!r} is not a file — expected {expected_line_count} lines")
    got = len(resolved_path.read_text().splitlines())
    if got == expected_line_count:
        return CheckResult(then, True, "has_lines", f"{relative_path!r} has {expected_line_count} lines")
    return CheckResult(then, False, "has_lines",
                       f"{relative_path!r} has {got} lines, expected {expected_line_count}")


@verifier("indexes",
          rf"""^{_FILE}{_QPATH}\s+indexes\s+the\s+files\s+"""
          rf"""(?:from|listed\s+(?:in|by))\s+step\s+(\d+)\s*[.!]?\s*$""")
def _v_indexes(match: regular_expression.Match, verify_context: VerifyContext) -> CheckResult:
    """The productive counterpart of covers: the step declares that
    its target is an index of step N's files, built by the harness
    indexing primitive (or, on the fallback path, by the executor).
    Strict by definition: an index that omits a source file is not an
    index of it, so every recorded item must appear, with no
    at-least-half floor and no exhaustive-cue condition."""
    relative_path = _pick(*match.groups()[0:4])
    step_number = int(match.group(5))
    then = match.string.strip()
    resolved_path, error = _resolve(verify_context, relative_path)
    if error:
        return CheckResult(then, False, "indexes", error)
    items = list(verify_context.prior_items.get(step_number) or [])
    excluded = verify_context.excluded_files.get(relative_path) or set()
    had_items = bool(items)
    if excluded:
        # Issue #47 option 1: a completeness check honors the plan's
        # contents-of excludes clauses for this deliverable.
        # Subtraction runs before the empty-set check, so a fully
        # excluded listing still reports ok, but with its own detail:
        # it had a list, and every item in it was excluded.
        items = [item for item in items if item not in excluded]
    if not items:
        if had_items:
            detail = (f"all files from step {step_number} excluded: "
                      f"coverage not checked")
        else:
            detail = (f"step {step_number} produced no file list: "
                      f"coverage not checked")
        return CheckResult(then, True, "indexes", detail)
    if not resolved_path.is_file():
        return CheckResult(then, False, "indexes",
                           f"{relative_path!r} is not a file — expected it to "
                           f"index the {len(items)} files from step "
                           f"{step_number}")
    content = resolved_path.read_text(errors="replace")
    bad = _deferral_in(content)
    if bad:
        return CheckResult(then, False, "indexes",
                           f"{relative_path!r} contains a deferral ({bad!r}) — "
                           f"the index must be finished, not promised")
    covered = [i for i in items if i in content]
    if len(covered) == len(items):
        return CheckResult(then, True, "indexes",
                           f"{relative_path!r} indexes all {len(items)} files "
                           f"from step {step_number}")
    missing = [i for i in items if i not in content]
    detail = (f"{relative_path!r} indexes {len(covered)} of {len(items)} files "
              f"from step {step_number}; an index must name every file; "
              f"missing include: " + ", ".join(missing[:12]))
    if len(missing) > 12:
        detail += (f" and {len(missing) - 12} more "
                   f"(see step {step_number}'s stored output)")
    return CheckResult(then, False, "indexes", detail)


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

# A step-reference phrase inside prose: singular, plural lists, and
# ranges. Same grammar as planner._step_refs; verify.py cannot import
# planner (planner imports verify), so the segment parser is local,
# like graphcheck's and planlint's own reference patterns. A plural
# head may carry a single number ("steps 2"), and separators may
# repeat between numbers, so an Oxford comma list ("steps 1, 2, and
# 3") parses whole instead of dropping its last member. The whitespace
# before a separator run sits outside the repeated group: with a \s*
# on both sides of each separator, adjacent repetitions can split the
# same spaces between them and backtrack exponentially when the tail
# never reaches a number.
_PRES_SEG = regular_expression.compile(
    r"\bsteps?\s+(\d+(?:\s*(?:(?:,|and|through|to|[-–—])\s*)+\d+)*)",
    regular_expression.IGNORECASE)
_PRES_TOKEN = regular_expression.compile(r"\d+|through|to|and|,|[-–—]", regular_expression.IGNORECASE)
_PRES_RANGE_SEPS = {"through", "to", "-", "–", "—"}


def _refs_in(text: str) -> list[int]:
    refs = [int(step_number_string) for step_number_string in regular_expression.findall(r"\bstep\s+(\d+)", text or "",
                                       regular_expression.IGNORECASE)]
    for match in _PRES_SEG.finditer(text or ""):
        nums: list[int] = []
        gaps: list[str] = []
        pending: list[str] = []
        for tok in _PRES_TOKEN.findall(match.group(1)):
            if tok.isdigit():
                if nums:
                    # One connector per numeric gap: the last separator
                    # spoken before the next number governs that gap,
                    # so repeated separators (", and") cannot shift the
                    # pairing between numbers and connectors. The token
                    # walk in planner._step_refs pairs them the same way.
                    gaps.append(pending[-1].strip().lower()
                                if pending else "")
                nums.append(int(tok))
                pending = []
            else:
                pending.append(tok)
        for i, num in enumerate(nums):
            if (i + 1 < len(nums) and gaps[i] in _PRES_RANGE_SEPS):
                a, b = num, nums[i + 1]
                if 0 < abs(b - a) <= 64:
                    refs.extend(range(min(a, b), max(a, b) + 1))
                    continue
            refs.append(num)
    seen: set[int] = set()
    out: list[int] = []
    for referenced_step_id in refs:
        if referenced_step_id not in seen:
            seen.add(referenced_step_id)
            out.append(referenced_step_id)
    return out


def _field_key(phrase: str) -> str:
    """Comparison key for a field phrase: the last word singularized
    (a trailing s, unless the word ends in ss, us, or is, which keeps
    'status' intact), so a clause naming 'prices' guards records
    labeled 'Price'."""
    words = (phrase or "").split()
    if words:
        w = words[-1]
        if (len(w) > 3 and w.endswith("s")
                and not w.endswith(("ss", "us", "is"))):
            words[-1] = w[:-1]
    return " ".join(words)


def _field_matches(field: str, label: str) -> bool:
    field_name, l = _field_key(field), _field_key(label)
    return l == field_name or l.startswith(field_name + " ")


def _parse_fields(text: str) -> list[str]:
    fields: list[str] = []
    for chunk in regular_expression.split(r",|\band\b", text or "", flags=regular_expression.IGNORECASE):
        field = regular_expression.sub(r"\s+", " ", chunk.strip().lower())
        field = regular_expression.sub(r"^the\s+", "", field)
        if field and regular_expression.fullmatch(r"[a-z][a-z0-9 \-]*", field) and field not in fields:
            fields.append(field)
    return fields


# Label: value pairs in record-shaped text ("Customer: Elena Vasquez.
# Item: ... Status: ready for pickup."). A value ends at a period, a
# semicolon, or the line's end, the way the source notes punctuate.
# Label words join on spaces only, never across lines: a bare label
# on its own line ("Result:") must not swallow the next line's label.
_PAIR = regular_expression.compile(
    r"([A-Z][A-Za-z]+(?:[ \t]+[A-Z][A-Za-z]+)*)[ \t]*:[ \t]*([^\n.;]+)")


def _harvest(text: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for match in _PAIR.finditer(text or ""):
        label = regular_expression.sub(r"\s+", " ", match.group(1).strip().lower())
        value = regular_expression.sub(r"\s+", " ", match.group(2).strip())
        if value and (label, value) not in pairs:
            pairs.append((label, value))
    return pairs


def _record_text(verify_context: VerifyContext, step_no: int) -> str | None:
    """The stored record of an earlier step in the current run, or
    None when it cannot be read (no run store, an ad-hoc context)."""
    try:
        latest = verify_context.jail.resolve(".harness/latest.txt")
        if not latest.is_file():
            return None
        lines = latest.read_text(errors="replace").strip().splitlines()
        if not lines or not lines[0].strip():
            return None
        resolved_path = verify_context.jail.resolve(
            f".harness/runs/{lines[0].strip()}/step-{step_no:02d}.md")
        if not resolved_path.is_file():
            return None
        return resolved_path.read_text(errors="replace")
    except (Refusal, OSError):
        return None


def _norm(text: str) -> str:
    return regular_expression.sub(r"\s+", " ", text or "").casefold()


@verifier("accounts_for",
          # The listing-only completeness contract (Daniel Okafor's
          # shape): the step writes from a listing's names alone, the
          # contents unread (often unreadable by the task's own rule).
          # Strict, unlike covers: when names are the only material,
          # there is no legitimate sampling, so every listed file
          # must appear. Phrasing tolerance mirrors covers (optional
          # contents-of prefix).
          rf"""^(?:the\s+contents?\s+of\s+|contents?\s+of\s+)?{_FILE}{_QPATH}\s+accounts?\s+for\s+the\s+files\s+"""
          rf"""(?:from|listed\s+(?:in|by))\s+step\s+(\d+)\s*[.!]?\s*$""")
def _v_accounts_for(match: regular_expression.Match, verify_context: VerifyContext) -> CheckResult:
    relative_path = _pick(*match.groups()[0:4])
    step_number = int(match.group(5))
    then = match.string.strip()
    resolved_path, error = _resolve(verify_context, relative_path)
    if error:
        return CheckResult(then, False, "accounts_for", error)
    items = list(verify_context.prior_items.get(step_number) or [])
    excluded = verify_context.excluded_files.get(relative_path) or set()
    had_items = bool(items)
    if excluded:
        # Issue #47 option 1: see _v_indexes. Same subtraction and
        # same all-excluded distinction, before the empty-set check.
        items = [item for item in items if item not in excluded]
    if not items:
        if had_items:
            detail = (f"all files from step {step_number} excluded: "
                      f"accounting not checked")
        else:
            detail = (f"step {step_number} produced no file list: "
                      f"accounting not checked")
        return CheckResult(then, True, "accounts_for", detail)
    if not resolved_path.is_file():
        return CheckResult(then, False, "accounts_for",
                           f"{relative_path!r} is not a file — expected it to "
                           f"account for the {len(items)} files from "
                           f"step {step_number}")
    content = resolved_path.read_text(errors="replace")
    bad = _deferral_in(content)
    if bad:
        return CheckResult(then, False, "accounts_for",
                           f"{relative_path!r} contains a deferral ({bad!r}) — "
                           f"the document must be finished, not promised")
    missing = [i for i in items if i not in content]
    if not missing:
        return CheckResult(then, True, "accounts_for",
                           f"{relative_path!r} accounts for all {len(items)} "
                           f"files from step {step_number}")
    detail = (f"{relative_path!r} accounts for {len(items) - len(missing)} of "
              f"{len(items)} files from step {step_number}; every listed "
              f"file must be accounted for; missing include: "
              + ", ".join(missing[:12]))
    if len(missing) > 12:
        detail += (f" and {len(missing) - 12} more "
                   f"(see step {step_number}'s stored output)")
    return CheckResult(then, False, "accounts_for", detail)


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
    refs = [referenced_step_id for referenced_step_id in _refs_in(match.group(6)) if referenced_step_id != verify_context.step_id]
    then = match.string.strip()
    if not fields or not refs:
        if not fields and not refs:
            detail = ("the clause names no fields and no source "
                      "steps: preservation not checked")
        elif not fields:
            detail = ("the clause names no fields: "
                      "preservation not checked")
        else:
            detail = ("the clause names no source steps: "
                      "preservation not checked")
        return CheckResult(then, True, "preserves", detail)
    resolved_path, error = _resolve(verify_context, relative_path)
    if error:
        return CheckResult(then, False, "preserves", error)
    ref_list = ", ".join(str(referenced_step_id) for referenced_step_id in refs)
    if not resolved_path.is_file():
        return CheckResult(then, False, "preserves",
                           f"{relative_path!r} is not a file — expected it to "
                           f"preserve {', '.join(fields)} from "
                           f"step(s) {ref_list}")
    content = _norm(resolved_path.read_text(errors="replace"))
    records = {referenced_step_id: _record_text(verify_context, referenced_step_id) for referenced_step_id in refs}
    total = 0
    missing: list[tuple[str, int, str]] = []
    unchecked: list[str] = []
    for f in fields:
        vals: list[tuple[int, str]] = []
        for referenced_step_id in refs:
            for label, value in _harvest(records.get(referenced_step_id) or ""):
                if (_field_matches(f, label)
                        and (referenced_step_id, value) not in vals):
                    vals.append((referenced_step_id, value))
        if not vals:
            unchecked.append(f)
            continue
        total += len(vals)
        for referenced_step_id, value in vals:
            if _norm(value) not in content:
                missing.append((f, referenced_step_id, value))
    note = (f"; field(s) {', '.join(unchecked)} never appear in the "
            f"source records — not checked" if unchecked else "")
    if total == 0:
        return CheckResult(then, True, "preserves",
                           f"no labeled values for {', '.join(fields)} "
                           f"found in the records of step(s) {ref_list} "
                           f"— preservation not checked")
    if missing:
        shown = "; ".join(f"{f} {value!r} (step {referenced_step_id})"
                          for f, referenced_step_id, value in missing[:8])
        if len(missing) > 8:
            shown += f"; and {len(missing) - 8} more"
        return CheckResult(then, False, "preserves",
                           f"{relative_path!r} is missing values the source "
                           f"steps read: {shown}{note}")
    return CheckResult(then, True, "preserves",
                       f"{relative_path!r} preserves all {total} values of "
                       f"{', '.join(fields)} read in step(s) "
                       f"{ref_list}{note}")


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

@verifier("excludes",
          rf"""^{_FILE}{_QPATH}\s+excludes?\s+"""
          rf"""(?:the\s+contents\s+of\s+{_QPATH}|{_QTEXT}|"""
          rf"""(.+?)\s+from\s+(.+?))\s*[.!]?\s*$""")
def _v_excludes(match: regular_expression.Match, verify_context: VerifyContext) -> CheckResult:
    groups = match.groups()
    relative_path = _pick(*groups[0:4])
    contents_relative_path = _pick(*groups[4:8])
    literal = _pick(*groups[8:11])
    then = match.string.strip()
    resolved_path, error = _resolve(verify_context, relative_path)
    if error:
        return CheckResult(then, False, "excludes", error)
    if not resolved_path.is_file():
        return CheckResult(then, False, "excludes",
                           f"{relative_path!r} is not a file — expected a "
                           f"deliverable that excludes the named "
                           f"content")
    content = _norm(resolved_path.read_text(errors="replace"))
    leaks: list[str] = []
    basis = ""
    if contents_relative_path is not None:
        sp, serr = _resolve(verify_context, contents_relative_path)
        if serr or sp is None or not sp.is_file():
            return CheckResult(then, True, "excludes",
                               f"source file {contents_relative_path!r} not "
                               f"found — exclusion not checked")
        src = sp.read_text(errors="replace")
        guarded = [value for _label, value in _harvest(src)]
        for line in src.splitlines():
            line = regular_expression.sub(r"\s+", " ", line.strip())
            if len(line) >= 12 and line not in guarded:
                guarded.append(line)
        leaks = [value for value in guarded if _norm(value) in content]
        basis = f"content from {contents_relative_path!r}"
        if not guarded:
            return CheckResult(then, True, "excludes",
                               f"{contents_relative_path!r} holds nothing "
                               f"checkable — exclusion not checked")
    elif literal is not None:
        if _norm(literal) in content:
            leaks = [literal]
        basis = f"the text {literal!r}"
    else:
        fields = _parse_fields(groups[11] or "")
        refs = [referenced_step_id for referenced_step_id in _refs_in(groups[12] or "") if referenced_step_id != verify_context.step_id]
        if not fields or not refs:
            return CheckResult(then, True, "excludes",
                               "the clause names no fields or no "
                               "source steps — exclusion not checked")
        guarded = []
        for referenced_step_id in refs:
            for label, value in _harvest(_record_text(verify_context, referenced_step_id) or ""):
                if any(_field_matches(f, label) for f in fields) \
                        and value not in guarded:
                    guarded.append(value)
        if not guarded:
            return CheckResult(then, True, "excludes",
                               f"no labeled values for "
                               f"{', '.join(fields)} found in the "
                               f"records of step(s) "
                               f"{', '.join(str(referenced_step_id) for referenced_step_id in refs)} "
                               f"— exclusion not checked")
        leaks = [value for value in guarded if _norm(value) in content]
        basis = (f"{', '.join(fields)} from step(s) "
                 f"{', '.join(str(referenced_step_id) for referenced_step_id in refs)}")
    if leaks:
        shown = "; ".join(repr(value) for value in leaks[:8])
        if len(leaks) > 8:
            shown += f"; and {len(leaks) - 8} more"
        return CheckResult(then, False, "excludes",
                           f"{relative_path!r} must exclude {basis}, but it "
                           f"contains: {shown}")
    return CheckResult(then, True, "excludes",
                       f"{relative_path!r} excludes {basis}")


@verifier("contains",
          rf"""^{_FILE}{_QPATH}\s+{_SHOULD}contains?\s+"""
          rf"""(?!exactly\b|the\s+exact\s+content\b){_QTEXT}\s*$""")
def _v_contains(match: regular_expression.Match, verify_context: VerifyContext) -> CheckResult:
    relative_path = _pick(*match.groups()[0:4])
    text = _pick(*match.groups()[4:7])
    then = match.string.strip()
    resolved_path, error = _resolve(verify_context, relative_path)
    if error:
        return CheckResult(then, False, "contains", error)
    if not resolved_path.is_file():
        return CheckResult(then, False, "contains",
                           f"{relative_path!r} is not a file — expected it to contain {text!r}")
    if text in resolved_path.read_text():
        return CheckResult(then, True, "contains", f"{relative_path!r} contains {text!r}")
    return CheckResult(then, False, "contains",
                       f"{relative_path!r} does not contain {text!r}")


@verifier("not_exists",
          rf"""^{_FILE}{_QPATH}\s+{_SHOULD}(?:does\s+)?not\s+exist\s*[.!]?\s*$""")
def _v_not_exists(match: regular_expression.Match, verify_context: VerifyContext) -> CheckResult:
    relative_path = _pick(*match.groups()[0:4])
    then = match.string.strip()
    resolved_path, error = _resolve(verify_context, relative_path)
    if error:
        return CheckResult(then, False, "not_exists", error)
    if not resolved_path.exists():
        return CheckResult(then, True, "not_exists", f"{relative_path!r} does not exist")
    return CheckResult(then, False, "not_exists",
                       f"{relative_path!r} exists — expected it not to")


@verifier("exists",
          rf"""^{_FILE}{_QPATH}\s+{_SHOULD}exists?\s*[.!]?\s*$""")
def _v_exists(match: regular_expression.Match, verify_context: VerifyContext) -> CheckResult:
    relative_path = _pick(*match.groups()[0:4])
    then = match.string.strip()
    resolved_path, error = _resolve(verify_context, relative_path)
    if error:
        return CheckResult(then, False, "exists", error)
    if resolved_path.exists():
        return CheckResult(then, True, "exists", f"{relative_path!r} exists")
    return CheckResult(then, False, "exists",
                       f"{relative_path!r} MISSING — expected it to exist")


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
    then = (then or "").strip()
    for verifier in VERIFIERS:
        match = verifier.pattern.match(then)
        if match:
            groups = match.groups()
            path = _pick(*groups[0:4])
            value = _pick(*groups[4:7]) if verifier.name in ("contains", "contains_exactly") else None
            return verifier.name, path, value
    return None


def _excluded_files_for(steps, current_step_id) -> dict[str, set[str]]:
    """Per-deliverable excluded filenames from contents-of excludes clauses.

    Scans Then clauses on steps with id <= current_step_id: same
    deliverable only, current or earlier steps only. Only the
    contents-of form yields a filename to subtract (its source,
    quoted or bare path-shaped). Literal and fields forms yield no
    filename, and a clause that does not parse (prose target or
    prose contents source) yields none and attests as before.
    Steps are duck-typed (any object with .id and .done_when, the
    newline-separated Then bodies), so this stays importable
    without planner, which imports verify.
    """
    excluded: dict[str, set[str]] = {}
    excludes = next((verifier for verifier in VERIFIERS if verifier.name == "excludes"), None)
    if excludes is None:
        return excluded
    for step in steps or []:
        if step.id > current_step_id:
            continue
        for clause in (step.done_when or "").split("\n"):
            clause = clause.strip()
            if not clause:
                continue
            parsed = parse_then(clause)
            if parsed is None or parsed[0] != "excludes":
                continue
            match = excludes.pattern.match(clause)
            if match is None:
                continue
            contents_relative_path = _pick(*match.groups()[4:8])
            if parsed[1] is None or contents_relative_path is None:
                continue
            excluded.setdefault(parsed[1], set()).add(contents_relative_path)
    return excluded


def verify_step(thens: list[str], verify_context: VerifyContext) -> StepVerify:
    """Evaluate each Then clause. Unmatched phrasings fall back to attestation
    (ok=True) — the harness only fails what it can actually check."""
    checks: list[CheckResult] = []
    for then in thens:
        then = (then or "").strip()
        if not then:
            continue
        result = None
        for verifier in VERIFIERS:
            match = verifier.pattern.match(then)
            if match:
                result = verifier.check(match, verify_context)
                break
        if result is None:
            result = _attest_or_floor(then, verify_context)
        checks.append(result)
    return StepVerify(ok=all(c.ok for c in checks), checks=checks)


_QUOTED = regular_expression.compile(r'''"([^"]*)"|'([^']*)'|`([^`]*)`''')
# A saved plan's covers clause: the verb is retired, so the clause
# is never checked as covers, but it still names its deliverable.
# This is the retired verifier's target grammar, kept so the
# existence floor and the deliverable audit can read that name in
# every written form, including the bare and contents-of forms that
# contain no quoted span. (graphcheck keeps its own copy for the
# approval graph.)
_COVERS_CLAUSE = regular_expression.compile(
    r"^\s*(?:the\s+contents?\s+of\s+|contents?\s+of\s+)?"
    r"(?:the\s+file\s+)?"
    r"(?:\"([^\"]*)\"|'([^']*)'|`([^`]*)`|([\w./\\-]+))"
    r"\s+covers?\s+the\s+files\s+"
    r"(?:from|listed\s+(?:in|by))\s+step\s+\d+\s*[.!]?\s*$",
    regular_expression.IGNORECASE)
# Absence intent: the clause wants the named file NOT there, so a
# missing file is the success state, never a floor failure.
_ABSENCE_INTENT = regular_expression.compile(
    r"not\s+exist|should\s+not|does\s+not|n't\s+exist|no\s+longer|"
    r"\babsent\b|\bremoved\b|\bdeleted\b", regular_expression.IGNORECASE)


def _legacy_covers_target(then: str) -> str | None:
    """The target of a retired covers clause, or None when the
    clause is not one. A covers clause is a positive existence
    promise by construction: its target is the deliverable,
    whatever words the filename itself contains."""
    covers_match = _COVERS_CLAUSE.match(then or "")
    if not covers_match:
        return None
    name = _pick(*covers_match.groups())
    return name.strip() if name else None


def _named_file(then: str) -> str | None:
    """The workspace file a Then clause names, when it names one in
    quotes and the quoted text is path-shaped (no whitespace, and
    either a slash or a dotted extension). Quoted prose ("done",
    a quoted phrase) is not a file. A retired covers clause also
    names its target, in the grammar above, quoted or bare. Pure
    phrasing; never touches the workspace."""
    covers_target = _legacy_covers_target(then)
    if covers_target is not None:
        return covers_target
    quoted_match = _QUOTED.search(then or "")
    if not quoted_match:
        return None
    name = next(alternative for alternative in quoted_match.groups()
                if alternative is not None).strip()
    if not name or any(c.isspace() for c in name):
        return None
    if "/" in name or regular_expression.search(r"\.[\w-]+$", name):
        return name
    return None


_LEADING_QUOTED = regular_expression.compile(r"""^\s*(?:the\s+file\s+)?["'`]""",
                             regular_expression.IGNORECASE)


_ABSENCE_PREDICATE = regular_expression.compile(
    r"""(?:does|do)\s+not\s+exist\b"""
    r"""|doesn't\s+exist\b"""
    r"""|(?:should|shall)\s+not\s+exist\b"""
    r"""|(?:is|was)\s+absent\b"""
    r"""|no\s+longer\s+(?:exists|in)\b"""
    r"""|(?:is|was)\s+no\s+longer\s+(?:there|present|available)\b"""
    r"""|(?:has|have|had)\s+been\s+(?:removed|deleted)\b"""
    r"""|(?:was|were|is|are)\s+(?:removed|deleted)\b""",
    regular_expression.IGNORECASE)


def absence_intent(clause: str) -> bool:
    """True when the clause wants its named file NOT there, so a
    missing file is the success state, never a gap. This is the
    exemption the existence floor and the deliverable audit apply;
    lint applies it too, so the layers agree on absence clauses.

    A parsed clause's verb decides: only not_exists expresses
    absence, so a contains value that happens to be an absence word
    ("result.txt" contains "removed") is not absence intent. For
    unparsed clauses the predicate must sit directly after the
    subject's quoted name and be about the subject itself, exactly
    as for preservation: a removal word inside the filename
    ("deleted-records.txt") or inside the described contents
    ("contains a list of removed records") is not a claim the file
    is gone. A recognized retired covers clause is never absence
    intent, as at the floor: it promises its target exists."""
    parsed = parse_then(clause or "")
    if parsed is not None:
        return parsed[0] == "not_exists"
    if _legacy_covers_target(clause or "") is not None:
        return False
    subject_match = _LEADING_SUBJECT.match(clause or "")
    if subject_match is None:
        return False
    remainder = (clause or "")[subject_match.end():].strip()
    return bool(_ABSENCE_PREDICATE.match(remainder))


_LEADING_SUBJECT = regular_expression.compile(
    r"""^\s*(?:the\s+file\s+)?""" + _QTEXT,
    regular_expression.IGNORECASE)


_PRESERVATION_PREDICATE = regular_expression.compile(
    r"(?:remains?|stays?)\s+(?:unchanged|unmodified|untouched|"
    r"as\s+it\s+was|the\s+same)\b"
    r"|(?:is|was)\s+(?:unchanged|unmodified|untouched)\b"
    r"|(?:is|was)\s+not\s+(?:modified|changed)\b"
    r"|(?:is|was)\s+left\s+(?:unchanged|unmodified|untouched|"
    r"as\s+it\s+was)\b"
    r"|(?:has|have)\s+not\s+been\s+(?:modified|changed)\b"
    r"|(?:does|do)\s+not\s+change\b",
    regular_expression.IGNORECASE)


def preservation_intent(clause: str) -> bool:
    """True when an unparsed clause asserts its named file is left
    as it was ("source.txt" remains unchanged): the file is a
    constraint on the step, never its output. The predicate must
    sit directly after the subject's quoted name and be about the
    subject itself: a preservation word inside the filename
    ("unchanged-jobs.txt") or inside the described contents
    ("contains a list of unchanged records") is not a claim about
    the file, and neither is a clause asserting the file changed
    ("source.txt" is modified). The subject's closing delimiter
    is the one that opened it, so an apostrophe inside the name
    ("owner's-notes.txt") does not cut the match short. Parsed
    clauses are governed by their verb; a preserves clause's
    subject is a deliverable the step writes, not a file it must
    leave alone."""
    if parse_then(clause or "") is not None:
        return False
    subject_match = _LEADING_SUBJECT.match(clause or "")
    if subject_match is None:
        return False
    remainder = (clause or "")[subject_match.end():].strip()
    return bool(_PRESERVATION_PREDICATE.match(remainder))


def then_subject_file(clause: str) -> str | None:
    """The file a Then clause is about: its subject.

    A parsed clause's subject is its verifier's target. A prose
    clause's subject is a quoted, path-shaped name in subject
    position, at the clause's start (`"urgent-gear.txt" contains a
    list of ...`); a file merely mentioned later in the clause (a
    listing Then enumerating the files it found) is not the
    subject, and neither is quoted prose. Pure phrasing; never
    touches the workspace. Issue #48: lint and the executor both
    keyed only on parsed clauses, so a deliverable named only in
    a prose Then was invisible to both."""
    clause = (clause or "").strip()
    parsed = parse_then(clause)
    if parsed is not None:
        return parsed[1]
    if not _LEADING_QUOTED.match(clause):
        return None
    return _named_file(clause)


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
    named_file = _named_file(then)
    if named_file and (_legacy_covers_target(then) is not None
                       or not _ABSENCE_INTENT.search(then)):
        path, error = _resolve(verify_context, named_file)
        if not error and path is not None and not path.exists():
            return CheckResult(
                then, False, "attest",
                f"named file {named_file!r} does not exist — a clause "
                f"about a file cannot pass on trust while the file "
                f"is missing; create {named_file!r} with the promised "
                f"content")
    return CheckResult(then, True, "attest",
                       "no verifier matched — taking the model's word")


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
            clause = clause.strip()
            if not clause:
                continue
            parsed = parse_then(clause)
            if parsed:
                verb, path, _value = parsed
                if path is None:
                    continue
                if verb == "not_exists":
                    excluded.add(path)
                elif path not in promised:
                    promised.append(path)
                continue
            named_file = _named_file(clause)
            if named_file is None:
                continue
            if (_legacy_covers_target(clause) is None
                    and _ABSENCE_INTENT.search(clause)):
                excluded.add(named_file)
            elif named_file not in promised:
                promised.append(named_file)
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
        if getattr(step, "status", None) != "done":
            continue
        if getattr(step, "verify_waived", False):
            continue
        checks = getattr(step, "verify", None) or []
        if not checks or not all(_check_verifier(c) == "attest"
                                 for c in checks):
            continue
        named: set[str] = set()
        for clause in (step.done_when or "").split("\n"):
            clause = clause.strip()
            if not clause:
                continue
            parsed = parse_then(clause)
            if parsed and parsed[1]:
                named.add(parsed[1])
                continue
            named_file = _named_file(clause)
            if named_file:
                named.add(named_file)
        deliverables = sorted(f for f in named if f in promised)
        if deliverables:
            out.append({"step": step.id, "deliverables": deliverables})
    return out
