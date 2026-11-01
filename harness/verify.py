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


@verifier("indexes",
          rf"""^{_FILE}{_QPATH}\s+indexes\s+the\s+files\s+"""
          rf"""(?:from|listed\s+(?:in|by))\s+step\s+(\d+)\s*[.!]?\s*$""")
def _v_indexes(m: re.Match, ctx: VerifyContext) -> CheckResult:
    """The productive counterpart of covers: the step declares that
    its target is an index of step N's files, built by the harness
    indexing primitive (or, on the fallback path, by the executor).
    Strict by definition: an index that omits a source file is not an
    index of it, so every recorded item must appear, with no
    at-least-half floor and no exhaustive-cue condition."""
    rel = _pick(*m.groups()[0:4])
    step_no = int(m.group(5))
    then = m.string.strip()
    p, err = _resolve(ctx, rel)
    if err:
        return CheckResult(then, False, "indexes", err)
    items = list(ctx.prior_items.get(step_no) or [])
    excluded = ctx.excluded_files.get(rel) or set()
    if excluded:
        # Issue #47 option 1: a completeness check honors the plan's
        # contents-of excludes clauses for this deliverable.
        # Subtraction runs before the empty-set check, so a fully
        # excluded listing reports "coverage not checked" exactly
        # like an empty listing.
        items = [i for i in items if i not in excluded]
    if not items:
        return CheckResult(then, True, "indexes",
                           f"step {step_no} produced no file list — "
                           f"coverage not checked")
    if not p.is_file():
        return CheckResult(then, False, "indexes",
                           f"{rel!r} is not a file — expected it to "
                           f"index the {len(items)} files from step "
                           f"{step_no}")
    content = p.read_text(errors="replace")
    bad = _deferral_in(content)
    if bad:
        return CheckResult(then, False, "indexes",
                           f"{rel!r} contains a deferral ({bad!r}) — "
                           f"the index must be finished, not promised")
    covered = [i for i in items if i in content]
    if len(covered) == len(items):
        return CheckResult(then, True, "indexes",
                           f"{rel!r} indexes all {len(items)} files "
                           f"from step {step_no}")
    missing = [i for i in items if i not in content]
    detail = (f"{rel!r} indexes {len(covered)} of {len(items)} files "
              f"from step {step_no}; an index must name every file; "
              f"missing include: " + ", ".join(missing[:12]))
    if len(missing) > 12:
        detail += (f" and {len(missing) - 12} more "
                   f"(see step {step_no}'s stored output)")
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
_PRES_SEG = re.compile(
    r"\bsteps?\s+(\d+(?:\s*(?:(?:,|and|through|to|[-–—])\s*)+\d+)*)",
    re.IGNORECASE)
_PRES_TOKEN = re.compile(r"\d+|through|to|and|,|[-–—]", re.IGNORECASE)
_PRES_RANGE_SEPS = {"through", "to", "-", "–", "—"}


def _refs_in(text: str) -> list[int]:
    refs = [int(n) for n in re.findall(r"\bstep\s+(\d+)", text or "",
                                       re.IGNORECASE)]
    for m in _PRES_SEG.finditer(text or ""):
        nums: list[int] = []
        gaps: list[str] = []
        pending: list[str] = []
        for tok in _PRES_TOKEN.findall(m.group(1)):
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
    for r in refs:
        if r not in seen:
            seen.add(r)
            out.append(r)
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
    f, l = _field_key(field), _field_key(label)
    return l == f or l.startswith(f + " ")


def _parse_fields(text: str) -> list[str]:
    fields: list[str] = []
    for chunk in re.split(r",|\band\b", text or "", flags=re.IGNORECASE):
        f = re.sub(r"\s+", " ", chunk.strip().lower())
        f = re.sub(r"^the\s+", "", f)
        if f and re.fullmatch(r"[a-z][a-z0-9 \-]*", f) and f not in fields:
            fields.append(f)
    return fields


# Label: value pairs in record-shaped text ("Customer: Elena Vasquez.
# Item: ... Status: ready for pickup."). A value ends at a period, a
# semicolon, or the line's end, the way the source notes punctuate.
# Label words join on spaces only, never across lines: a bare label
# on its own line ("Result:") must not swallow the next line's label.
_PAIR = re.compile(
    r"([A-Z][A-Za-z]+(?:[ \t]+[A-Z][A-Za-z]+)*)[ \t]*:[ \t]*([^\n.;]+)")


def _harvest(text: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for m in _PAIR.finditer(text or ""):
        label = re.sub(r"\s+", " ", m.group(1).strip().lower())
        value = re.sub(r"\s+", " ", m.group(2).strip())
        if value and (label, value) not in pairs:
            pairs.append((label, value))
    return pairs


def _record_text(ctx: VerifyContext, step_no: int) -> str | None:
    """The stored record of an earlier step in the current run, or
    None when it cannot be read (no run store, an ad-hoc context)."""
    try:
        latest = ctx.jail.resolve(".harness/latest.txt")
        if not latest.is_file():
            return None
        lines = latest.read_text(errors="replace").strip().splitlines()
        if not lines or not lines[0].strip():
            return None
        p = ctx.jail.resolve(
            f".harness/runs/{lines[0].strip()}/step-{step_no:02d}.md")
        if not p.is_file():
            return None
        return p.read_text(errors="replace")
    except (Refusal, OSError):
        return None


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").casefold()


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
def _v_accounts_for(m: re.Match, ctx: VerifyContext) -> CheckResult:
    rel = _pick(*m.groups()[0:4])
    step_no = int(m.group(5))
    then = m.string.strip()
    p, err = _resolve(ctx, rel)
    if err:
        return CheckResult(then, False, "accounts_for", err)
    items = list(ctx.prior_items.get(step_no) or [])
    excluded = ctx.excluded_files.get(rel) or set()
    if excluded:
        # Issue #47 option 1: see _v_indexes. Same subtraction for
        # the same reason, before the empty-set check.
        items = [i for i in items if i not in excluded]
    if not items:
        return CheckResult(then, True, "accounts_for",
                           f"step {step_no} produced no file list — "
                           f"accounting not checked")
    if not p.is_file():
        return CheckResult(then, False, "accounts_for",
                           f"{rel!r} is not a file — expected it to "
                           f"account for the {len(items)} files from "
                           f"step {step_no}")
    content = p.read_text(errors="replace")
    bad = _deferral_in(content)
    if bad:
        return CheckResult(then, False, "accounts_for",
                           f"{rel!r} contains a deferral ({bad!r}) — "
                           f"the document must be finished, not promised")
    missing = [i for i in items if i not in content]
    if not missing:
        return CheckResult(then, True, "accounts_for",
                           f"{rel!r} accounts for all {len(items)} "
                           f"files from step {step_no}")
    detail = (f"{rel!r} accounts for {len(items) - len(missing)} of "
              f"{len(items)} files from step {step_no}; every listed "
              f"file must be accounted for; missing include: "
              + ", ".join(missing[:12]))
    if len(missing) > 12:
        detail += (f" and {len(missing) - 12} more "
                   f"(see step {step_no}'s stored output)")
    return CheckResult(then, False, "accounts_for", detail)


@verifier("preserves",
          rf"""^{_FILE}{_QPATH}\s+preserves?\s+(.+?)\s+from\s+(.+?)\s*[.!]?\s*$""")
def _v_preserves(m: re.Match, ctx: VerifyContext) -> CheckResult:
    """Field preservation: every value the named source steps read
    for each named field must appear in the deliverable. Strict, like
    indexes: a compilation that drops one customer's status has not
    preserved the records. A field whose label never appears in the
    source records cannot be checked and is reported as such, the
    same honesty rule as covers with no measurable source list."""
    rel = _pick(*m.groups()[0:4])
    fields = _parse_fields(m.group(5))
    refs = [r for r in _refs_in(m.group(6)) if r != ctx.step_id]
    then = m.string.strip()
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
    p, err = _resolve(ctx, rel)
    if err:
        return CheckResult(then, False, "preserves", err)
    ref_list = ", ".join(str(r) for r in refs)
    if not p.is_file():
        return CheckResult(then, False, "preserves",
                           f"{rel!r} is not a file — expected it to "
                           f"preserve {', '.join(fields)} from "
                           f"step(s) {ref_list}")
    content = _norm(p.read_text(errors="replace"))
    records = {r: _record_text(ctx, r) for r in refs}
    total = 0
    missing: list[tuple[str, int, str]] = []
    unchecked: list[str] = []
    for f in fields:
        vals: list[tuple[int, str]] = []
        for r in refs:
            for label, value in _harvest(records.get(r) or ""):
                if (_field_matches(f, label)
                        and (r, value) not in vals):
                    vals.append((r, value))
        if not vals:
            unchecked.append(f)
            continue
        total += len(vals)
        for r, value in vals:
            if _norm(value) not in content:
                missing.append((f, r, value))
    note = (f"; field(s) {', '.join(unchecked)} never appear in the "
            f"source records — not checked" if unchecked else "")
    if total == 0:
        return CheckResult(then, True, "preserves",
                           f"no labeled values for {', '.join(fields)} "
                           f"found in the records of step(s) {ref_list} "
                           f"— preservation not checked")
    if missing:
        shown = "; ".join(f"{f} {v!r} (step {r})"
                          for f, r, v in missing[:8])
        if len(missing) > 8:
            shown += f"; and {len(missing) - 8} more"
        return CheckResult(then, False, "preserves",
                           f"{rel!r} is missing values the source "
                           f"steps read: {shown}{note}")
    return CheckResult(then, True, "preserves",
                       f"{rel!r} preserves all {total} values of "
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
def _v_excludes(m: re.Match, ctx: VerifyContext) -> CheckResult:
    g = m.groups()
    rel = _pick(*g[0:4])
    contents_rel = _pick(*g[4:8])
    literal = _pick(*g[8:11])
    then = m.string.strip()
    p, err = _resolve(ctx, rel)
    if err:
        return CheckResult(then, False, "excludes", err)
    if not p.is_file():
        return CheckResult(then, False, "excludes",
                           f"{rel!r} is not a file — expected a "
                           f"deliverable that excludes the named "
                           f"content")
    content = _norm(p.read_text(errors="replace"))
    leaks: list[str] = []
    basis = ""
    if contents_rel is not None:
        sp, serr = _resolve(ctx, contents_rel)
        if serr or sp is None or not sp.is_file():
            return CheckResult(then, True, "excludes",
                               f"source file {contents_rel!r} not "
                               f"found — exclusion not checked")
        src = sp.read_text(errors="replace")
        guarded = [v for _label, v in _harvest(src)]
        for line in src.splitlines():
            line = re.sub(r"\s+", " ", line.strip())
            if len(line) >= 12 and line not in guarded:
                guarded.append(line)
        leaks = [v for v in guarded if _norm(v) in content]
        basis = f"content from {contents_rel!r}"
        if not guarded:
            return CheckResult(then, True, "excludes",
                               f"{contents_rel!r} holds nothing "
                               f"checkable — exclusion not checked")
    elif literal is not None:
        if _norm(literal) in content:
            leaks = [literal]
        basis = f"the text {literal!r}"
    else:
        fields = _parse_fields(g[11] or "")
        refs = [r for r in _refs_in(g[12] or "") if r != ctx.step_id]
        if not fields or not refs:
            return CheckResult(then, True, "excludes",
                               "the clause names no fields or no "
                               "source steps — exclusion not checked")
        guarded = []
        for r in refs:
            for label, value in _harvest(_record_text(ctx, r) or ""):
                if any(_field_matches(f, label) for f in fields) \
                        and value not in guarded:
                    guarded.append(value)
        if not guarded:
            return CheckResult(then, True, "excludes",
                               f"no labeled values for "
                               f"{', '.join(fields)} found in the "
                               f"records of step(s) "
                               f"{', '.join(str(r) for r in refs)} "
                               f"— exclusion not checked")
        leaks = [v for v in guarded if _norm(v) in content]
        basis = (f"{', '.join(fields)} from step(s) "
                 f"{', '.join(str(r) for r in refs)}")
    if leaks:
        shown = "; ".join(repr(v) for v in leaks[:8])
        if len(leaks) > 8:
            shown += f"; and {len(leaks) - 8} more"
        return CheckResult(then, False, "excludes",
                           f"{rel!r} must exclude {basis}, but it "
                           f"contains: {shown}")
    return CheckResult(then, True, "excludes",
                       f"{rel!r} excludes {basis}")


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
    excludes = next((v for v in VERIFIERS if v.name == "excludes"), None)
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
            m = excludes.pattern.match(clause)
            if m is None:
                continue
            contents_rel = _pick(*m.groups()[4:8])
            if parsed[1] is None or contents_rel is None:
                continue
            excluded.setdefault(parsed[1], set()).add(contents_rel)
    return excluded


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
            result = _attest_or_floor(then, ctx)
        checks.append(result)
    return StepVerify(ok=all(c.ok for c in checks), checks=checks)


_QUOTED = re.compile(r'''"([^"]*)"|'([^']*)'|`([^`]*)`''')
# A saved plan's covers clause: the verb is retired, so the clause
# is never checked as covers, but it still names its deliverable.
# This is the retired verifier's target grammar, kept so the
# existence floor and the deliverable audit can read that name in
# every written form, including the bare and contents-of forms that
# contain no quoted span. (graphcheck keeps its own copy for the
# approval graph.)
_COVERS_CLAUSE = re.compile(
    r"^\s*(?:the\s+contents?\s+of\s+|contents?\s+of\s+)?"
    r"(?:the\s+file\s+)?"
    r"(?:\"([^\"]*)\"|'([^']*)'|`([^`]*)`|([\w./\\-]+))"
    r"\s+covers?\s+the\s+files\s+"
    r"(?:from|listed\s+(?:in|by))\s+step\s+\d+\s*[.!]?\s*$",
    re.IGNORECASE)
# Absence intent: the clause wants the named file NOT there, so a
# missing file is the success state, never a floor failure.
_ABSENCE_INTENT = re.compile(
    r"not\s+exist|should\s+not|does\s+not|n't\s+exist|no\s+longer|"
    r"\babsent\b|\bremoved\b|\bdeleted\b", re.IGNORECASE)


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
    if "/" in name or re.search(r"\.[\w-]+$", name):
        return name
    return None


_LEADING_QUOTED = re.compile(r"""^\s*(?:the\s+file\s+)?["'`]""",
                             re.IGNORECASE)


_ABSENCE_PREDICATE = re.compile(
    r"""(?:does|do)\s+not\s+exist\b"""
    r"""|doesn't\s+exist\b"""
    r"""|(?:should|shall)\s+not\s+exist\b"""
    r"""|(?:is|was)\s+absent\b"""
    r"""|no\s+longer\s+(?:exists|in)\b"""
    r"""|(?:is|was)\s+no\s+longer\s+(?:there|present|available)\b"""
    r"""|(?:has|have|had)\s+been\s+(?:removed|deleted)\b"""
    r"""|(?:was|were|is|are)\s+(?:removed|deleted)\b""",
    re.IGNORECASE)


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


_LEADING_SUBJECT = re.compile(
    r"""^\s*(?:the\s+file\s+)?""" + _QTEXT,
    re.IGNORECASE)


_PRESERVATION_PREDICATE = re.compile(
    r"(?:remains?|stays?)\s+(?:unchanged|unmodified|untouched|"
    r"as\s+it\s+was|the\s+same)\b"
    r"|(?:is|was)\s+(?:unchanged|unmodified|untouched)\b"
    r"|(?:is|was)\s+not\s+(?:modified|changed)\b"
    r"|(?:is|was)\s+left\s+(?:unchanged|unmodified|untouched|"
    r"as\s+it\s+was)\b"
    r"|(?:has|have)\s+not\s+been\s+(?:modified|changed)\b"
    r"|(?:does|do)\s+not\s+change\b",
    re.IGNORECASE)


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


def _attest_or_floor(then: str, ctx: VerifyContext) -> CheckResult:
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
        path, err = _resolve(ctx, named_file)
        if not err and path is not None and not path.exists():
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
    return sorted(p for p in promised if p not in excluded)


def missing_deliverables(steps, jail: Jail) -> list[str]:
    """Promised deliverables that do not exist in the workspace.
    Paths escaping the jail cannot be judged and are skipped."""
    missing: list[str] = []
    for rel in promised_deliverables(steps):
        try:
            p = jail.resolve(rel)
        except Refusal:
            continue
        if not p.exists():
            missing.append(rel)
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
