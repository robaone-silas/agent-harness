"""Deterministic plan lint: catch plan design errors with zero model calls.

Runs on a proposed Gherkin plan after structural validation, before the plan
reaches the user for approval (or auto-execution). Complements the v0.7
per-step verifiers: those check plan *adherence* at runtime, this checks plan
*coherence* up front.

Findings come in two severities:
  ERROR — almost certainly a bug. Triggers one planner retry with the finding
          as feedback (same shape as the Gherkin validation retry).
  WARN  — suspicious. Advisory only; shown at approval time, never blocks.

Checks:
  dangling_file      (ERROR): a Then targets a file no step creates and the
                     task doesn't mention (e.g. Then checks greeting.txt
                     while the plan writes hello.txt). mv/cp destinations
                     count as creations: `mv a.txt Dir/` creates Dir/a.txt.
  value_from_nowhere (WARN): a Then asserts an exact value appearing nowhere
                     in the task or prior steps — computed or invented?
  unparseable_check  (WARN): a Then reads like a check ("contains exactly",
                     "should exist", ...) but matches no verifier, so the
                     harness will take it on trust.
  intent_drift       (WARN): the task's outcome words (description,
                     summary, caption, one-line, ...) survive in no step's
                     Intent, When, or Then — decomposition may have
                     silently traded the task's intent for mechanics
                     (field: "a one-line description" became "the content").
  per_item_instead_of_covers (ERROR): a listing step followed by a step
                     carrying at least two unmatched per-item containment
                     Then clauses against the same target and no covers
                     clause for that target (field, issue #5: Priya's
                     `"audit-index.md" contains an entry for "<file>"`
                     times six). Completeness expressed that way matches
                     no verifier and rests entirely on trust; the covers
                     clause is the machine-checkable form.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from . import verify as _verify

# Any quoted span is a candidate file/value mention in free text.
_QUOTED = re.compile(r"""["'`]([^"'`]+)["'`]""")
# Bare path-like token: must contain a dot or slash (hello.txt, src/main.py).
_BARE_PATH = re.compile(r"(?<![\w./-])([\w-]+(?:[./][\w.-]+)+)(?![\w.-])")
# Phrasing that *tries* to be a machine check.
_CHECKY = re.compile(
    r"""contains?\s+(exactly\s+)?["'`]|\bexists?\s*[.!]*$|does\s+not\s+exist|"""
    r"""has\s+\d+\s+lines?|covers?\s+the\s+files""",
    re.IGNORECASE,
)
# A per-item containment clause: a quoted target, "contains", then some
# bridge text and a quoted item (`"index.md" contains an entry for
# "a.txt"`). Group 1 is the target, group 2 the item. On its own this is
# just an unmatched clause; two or more against one target after a
# listing step are the issue #5 near miss for a covers clause.
_PER_ITEM_CONTAINS = re.compile(
    r"""^\s*(?:the\s+file\s+)?["'`]([^"'`]+)["'`]\s+(?:should\s+)?"""
    r"""contains?\b.*["'`]([^"'`]+)["'`]""",
    re.IGNORECASE,
)
# A step whose When lists files (the source set a later deliverable is
# derived from).
_LISTING = re.compile(r"\blist(?:s|ing|ed)?\b", re.IGNORECASE)


@dataclass
class LintFinding:
    step_id: int
    level: str  # "error" | "warn"
    code: str
    detail: str

    def as_dict(self) -> dict:
        return {"step_id": self.step_id, "level": self.level,
                "code": self.code, "detail": self.detail}


def _mentions(text: str) -> set[str]:
    """File-ish mentions in free text (over-approximates; lenient by design).

    Quoted spans plus bare path-like tokens (must contain a dot or slash,
    so prose words like 'output' never count as files).
    """
    out = {m.group(1).strip() for m in _QUOTED.finditer(text or "")
           if m.group(1).strip()}
    out |= {m.group(1).rstrip(".") for m in _BARE_PATH.finditer(text or "")}
    return {p for p in out if p}


_MOVE_CMD = re.compile(r"\b(?:mv|cp)\s+([^;&\n]+)", re.IGNORECASE)


def _move_destinations(text: str) -> set[str]:
    """Paths an mv/cp command in a When creates (v0.8.1 lint fix).

    dangling_file worked from literal mentions, so the destination of
    `mv garden-plan.md Garden/` (namely Garden/garden-plan.md) counted
    as created by nobody, and the lint errored on the exact plan shape
    the planner prompt's own archive example teaches (field, 2026-10-03).
    Compute the destinations instead: target ending in "/" (or several
    sources) means a folder, destination is folder + source basename;
    a single source with a file target is a rename/copy, destination is
    the target itself."""
    import posixpath
    import shlex
    out: set[str] = set()
    for m in _MOVE_CMD.finditer(text or ""):
        try:
            tokens = shlex.split(m.group(1))
        except ValueError:
            tokens = m.group(1).split()
        tokens = [t for t in tokens if not t.startswith("-")]
        if len(tokens) < 2:
            continue
        sources, target = tokens[:-1], tokens[-1].rstrip(",.")
        if target.endswith("/") or len(sources) > 1:
            folder = target.rstrip("/")
            for src in sources:
                base = posixpath.basename(src.rstrip("/"))
                if base:
                    out.add(f"{folder}/{base}" if folder else base)
        else:
            out.add(target)
    return out


# Words that carry a task's qualitative specification of its deliverable.
# If the task uses one and no step does, the spec likely died in
# decomposition (intent_drift). Stems, matched case-insensitively.
_SPEC_STEMS = ("descri", "summar", "caption", "one-line", "one-sentence")


def _intent_drift(task: str, steps: list) -> LintFinding | None:
    task_low = (task or "").lower()
    plan_low = "\n".join(
        " ".join([getattr(s, "intent", "") or "", s.instruction or "",
                  s.done_when or "", s.given or "",
                  getattr(s, "title", "") or ""])
        for s in steps).lower()
    missing = []
    for stem in _SPEC_STEMS:
        if stem in task_low and stem not in plan_low:
            m = re.search(r"[\w-]*" + re.escape(stem) + r"[\w-]*", task_low)
            missing.append(m.group(0) if m else stem)
    if not missing or not steps:
        return None
    return LintFinding(
        steps[-1].id, "warn", "intent_drift",
        f"Plan: the task asks for {', '.join(missing)}, but no step's "
        f"Intent, When, or Then mentions it — the task's intent may "
        f"have been lost in decomposition.")


def _per_item_covers_finding(step_id: int, listing_id: int,
                             per_item: dict, covers_targets: set):
    """The issue #5 aggregate near miss, as a retryable ERROR.

    Fires when a deliverable step checks one target with at least two
    unmatched per-item containment clauses and has no covers clause for
    that target. The detail is the retry feedback, so it names the exact
    replacement clause the planner should write."""
    for target, count in per_item.items():
        if count >= 2 and target not in covers_targets:
            return LintFinding(
                step_id, "error", "per_item_instead_of_covers",
                f"Step {step_id}: {count} Then clauses check "
                f"\"{target}\" one file at a time, but none of them "
                f"matches a verifier, so completeness would be taken on "
                f"trust. Replace those per-item clauses with one clause: "
                f"\"{target}\" covers the files from step {listing_id}.")
    return None


def lint_plan(task: str, steps: list) -> list[LintFinding]:
    """Lint a proposed plan. `steps` are PlanStep (id, instruction, done_when)."""
    findings: list[LintFinding] = []
    known: set[str] = set(_mentions(task))  # files the task itself names
    whens: list[str] = [task or ""]
    listing_ids: list[int] = []  # earlier steps whose When lists files
    for s in steps:
        known |= _mentions(s.instruction)
        known |= _move_destinations(s.instruction)
        known |= _mentions(s.given)
        whens.append(s.instruction or "")
        thens = [t.strip() for t in (s.done_when or "").split("\n") if t.strip()]
        per_item: dict[str, int] = {}
        covers_targets: set[str] = set()
        for t in thens:
            parsed = _verify.parse_then(t)
            if parsed is None:
                m = _PER_ITEM_CONTAINS.match(t)
                if m:
                    target = m.group(1).strip()
                    per_item[target] = per_item.get(target, 0) + 1
                if _CHECKY.search(t):
                    findings.append(LintFinding(
                        s.id, "warn", "unparseable_check",
                        f"Step {s.id}: Then reads like a check but matches no "
                        f"verifier — it will be taken on trust: {t[:80]}"))
                continue
            _name, path, value = parsed
            if _name == "covers" and path:
                covers_targets.add(path)
            if path and path not in known:
                findings.append(LintFinding(
                    s.id, "error", "dangling_file",
                    f"Step {s.id}: Then refers to '{path}', which no step "
                    f"creates and the task doesn't mention."))
            if value:
                v = value.strip()
                if v and not any(v in w for w in whens):
                    findings.append(LintFinding(
                        s.id, "warn", "value_from_nowhere",
                        f"Step {s.id}: Then asserts exact value '{v[:60]}', "
                        f"which appears nowhere in the task or prior steps — "
                        f"computed or invented?"))
        if listing_ids:
            agg = _per_item_covers_finding(s.id, listing_ids[-1],
                                           per_item, covers_targets)
            if agg is not None:
                findings.append(agg)
        if _LISTING.search(s.instruction or ""):
            listing_ids.append(s.id)
    drift = _intent_drift(task, steps)
    if drift is not None:
        findings.append(drift)
    return findings
