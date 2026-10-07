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
  unaccounted_file   (WARN, workspace-aware): under narrow exhaustive
                     cues (a list, inventory, index, or catalogue task,
                     or all/every/each of the files), a workspace root
                     file mentioned nowhere in the task or plan, in a
                     plan that neither generically lists the workspace
                     nor uses an indexes clause (field, issue #6: Tomas's
                     water-heater-manual.txt, named nowhere and silently
                     dropped). Needs the workspace root file set; lint
                     without it stays workspace-blind for this check.
  deliverable_clarity (WARN): list, inventory, report, or index language
                     names no output file and declares no answer-only
                     result, so nobody can tell whether the deliverable
                     is a file to open or words in an answer (field,
                     issue #6: Tomas's plan named no deliverable at all).
  attested_compile   (WARN): a step writes a deliverable out of what
                     earlier read steps gathered, and every Then clause
                     guarding that deliverable is on trust, so nothing
                     checks that the compiled file keeps what the
                     earlier steps gathered (field, issue #37: Marcus's
                     work list dropped every customer and status the
                     task required while the run reported done; the
                     existence floor passed because the file exists,
                     and indexes cannot express per-record
                     field preservation). The warning makes the trust
                     visible at approval; it never blocks.
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
# A step whose When lists files (the source set a later deliverable is
# derived from).
_LISTING = re.compile(r"\blist(?:s|ing|ed)?\b", re.IGNORECASE)
# A step that generically lists the workspace itself, as opposed to a
# step that merely creates "a list" of something: the list verb must sit
# close to the thing listed (files, workspace, folder, contents), or
# the step must name the list_dir tool. Tomas's "I compile a list of
# house papers ... from the files read in step 1" is a deliverable
# step, not a listing step, and must not count here.
_GENERIC_LISTING = re.compile(
    r"\blist(?:s|ing|ed)?\b[^.\n]{0,25}\b(?:files?|workspace|folder|"
    r"director\w+|contents)\b|\blist_dir\b",
    re.IGNORECASE,
)
# Narrow exhaustive cues in the task: the same cue set the discovery
# prompt treats as exhaustive (planner._is_exhaustive_task). A list,
# inventory, index, or catalogue, or all/every/each of the files,
# documents, papers, or records, makes every root file potentially in
# scope; category, strategy, and value tasks carry none of these cues
# and keep sampling, so they never warn here.
_EXHAUSTIVE_TASK = re.compile(
    r"\b(?:list|listing|inventory|index|catalogue|catalog)\b|"
    r"\b(?:all|every|each)\b[^.\n]{0,40}"
    r"\b(?:files?|documents?|papers?|records?)\b",
    re.IGNORECASE,
)
# Task language that promises a deliverable artifact of some kind.
_DELIVERABLE_TASK = re.compile(
    r"\b(?:list|listing|inventory|inventories|report|reports|index|"
    r"indexes|indexing)\b",
    re.IGNORECASE,
)
# An explicit declaration that the result lives in the answer, not in
# a file. That is a legitimate deliverable choice; it just has to be
# stated, not left ambiguous.
_ANSWER_ONLY = re.compile(
    r"answer[- ]only|in (?:your|the) (?:answer|reply|response)\b|"
    r"\bno (?:output )?file\b|without (?:creating|writing|making) a file|"
    r"do(?:es)? not (?:create|write) a file|"
    r"don['’]t (?:create|write) a file",
    re.IGNORECASE,
)
# Verbs that produce a file in a step's When. A file a step mentions
# while writing is a candidate output; a file a step only reads is not.
_WRITE_VERB = re.compile(
    r"\b(?:writ\w*|creat\w*|sav\w*|produc\w*|compil\w*|generat\w*|"
    r"draft\w*)\b",
    re.IGNORECASE,
)
# Verbs that produce a deliverable, for the attested-compile rule:
# the write family above plus run/execute/append forms
# (a step that runs a script to build its target produces it, even
# without a literal write verb). "Prepare" and other gather verbs
# deliberately do not count: preparing entries is not producing the
# deliverable.
_PRODUCE_VERB = re.compile(
    r"\b(?:writ\w*|creat\w*|sav\w*|produc\w*|compil\w*|generat\w*|"
    r"draft\w*|append\w*|runs?|running|execut\w*)\b",
    re.IGNORECASE,
)
_STEP_REF = re.compile(r"\bstep\s+(\d+)\b", re.IGNORECASE)
# A step that reads source material (as opposed to listing it or
# writing): the gather half of a read-then-compile plan.
_READ_VERB = re.compile(r"\bread\w*\b", re.IGNORECASE)
# Verifiers that machine-check a file's content (as opposed to its
# mere existence): a deliverable guarded by one of these does not
# rest entirely on trust.
_CONTENT_CHECKS = {"contains", "contains_exactly", "has_lines",
                   "indexes"}


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


def _gathers_sources(step) -> bool:
    """A step that reads source material into the run: a read verb
    over named files, or a generic listing of the workspace."""
    when = step.instruction or ""
    return bool((_READ_VERB.search(when) and _mentions(when))
                or _GENERIC_LISTING.search(when))


def _attested_compile_warnings(steps: list) -> list[LintFinding]:
    """The read-then-compile trust warning (issue #37).

    Fires on a step that writes a deliverable out of what earlier
    read steps gathered when every Then clause guarding that
    deliverable is attested: existence is floored by the runtime
    checks, but nothing anywhere verifies the compiled content
    preserves what the earlier steps read, and a compilation can
    drop fields (every customer, every status) while the run
    reports done. A deliverable carrying any content-bearing
    machine check (contains, has_lines, indexes) anywhere
    in the plan does not rest entirely on trust and never warns.
    Advisory only, like every warning: it makes the trust visible
    at approval, it does not block the plan."""
    checked: set[str] = set()
    for s in steps:
        for t in (s.done_when or "").split("\n"):
            parsed = _verify.parse_then(t.strip())
            if parsed and parsed[0] in _CONTENT_CHECKS and parsed[1]:
                checked.add(parsed[1])
    findings: list[LintFinding] = []
    gathered = False
    for s in steps:
        when = s.instruction or ""
        if gathered and _PRODUCE_VERB.search(when):
            deliverables = {p for p in _mentions(s.done_when or "")
                            if p not in checked}
            if not deliverables:
                deliverables = {p for p in _mentions(when)
                                if p not in checked}
            thens = [t.strip() for t in (s.done_when or "").split("\n")
                     if t.strip()]
            if deliverables and thens and all(
                    _verify.parse_then(t) is None for t in thens):
                target = sorted(deliverables)[0]
                findings.append(LintFinding(
                    s.id, "warn", "attested_compile",
                    f"Step {s.id}: this step compiles \"{target}\" "
                    f"from what earlier steps read, but every Then "
                    f"clause guarding it is on trust, so nothing "
                    f"checks that the compiled file keeps what those "
                    f"steps gathered (the details and fields in the "
                    f"source records). The completeness of "
                    f"\"{target}\" rests entirely on the model's "
                    f"word; approve knowing that, or ask for a "
                    f"machine-checkable clause where the content is "
                    f"known ahead of time."))
        if _gathers_sources(s):
            gathered = True
    return findings


def _workspace_file_set(workspace_files) -> set[str] | None:
    """Normalize the optional workspace root file set for lint_plan.

    Accepts an iterable of root file names (or paths, whose final
    component is the root name) or a single workspace directory path,
    in which case the root regular files are read from disk, matching
    the digest's view: folders and `.harness/` bookkeeping never
    count. None means the caller supplied no workspace information,
    and the workspace-aware check stays off."""
    if workspace_files is None:
        return None
    import os
    from pathlib import Path
    if isinstance(workspace_files, (str, os.PathLike)):
        p = Path(workspace_files)
        if p.is_dir():
            try:
                return {e.name for e in p.iterdir()
                        if e.name != ".harness" and e.is_file()}
            except OSError:
                return set()
        return set()
    out: set[str] = set()
    for item in workspace_files:
        name = str(item).replace("\\", "/").rstrip("/")
        name = name.rsplit("/", 1)[-1] if "/" in name else name
        if name and name != ".harness":
            out.add(name)
    return out


def _plan_text(task: str, steps: list) -> str:
    """Everything the task and plan say, for mention searches."""
    parts = [task or ""]
    for s in steps:
        parts.append("\n".join([
            getattr(s, "instruction", "") or "",
            getattr(s, "done_when", "") or "",
            getattr(s, "given", "") or "",
            getattr(s, "title", "") or "",
            getattr(s, "intent", "") or ""]))
    return "\n".join(parts)


def _workspace_warnings(task: str, steps: list, workspace_set,
                        generic_listing: bool, indexes_any: bool,
                        output_files: set) -> list[LintFinding]:
    """The two workspace-aware warnings (issue #6, repair step 5).

    Both are warnings, never errors: an unaccounted file can be
    legitimately irrelevant, and an answer-only deliverable can be
    exactly what the user wanted. The lint's job is to make the
    choice visible at approval time, not to block it."""
    if not steps:
        return []
    findings: list[LintFinding] = []
    last_id = steps[-1].id
    if workspace_set and _EXHAUSTIVE_TASK.search(task or "") \
            and not generic_listing and not indexes_any:
        corpus = _plan_text(task, steps)
        for name in sorted(workspace_set):
            if name not in corpus:
                findings.append(LintFinding(
                    last_id, "warn", "unaccounted_file",
                    f"Plan: the workspace file '{name}' is mentioned "
                    f"nowhere in the task or the plan, and the plan "
                    f"neither lists the workspace nor uses an indexes "
                    f"clause. Under this task every root file is "
                    f"potentially in scope, so '{name}' is "
                    f"unaccounted for: include it, or exclude it "
                    f"explicitly."))
    cue = _DELIVERABLE_TASK.search(task or "")
    if cue and not output_files \
            and not _ANSWER_ONLY.search(_plan_text(task, steps)):
        findings.append(LintFinding(
            last_id, "warn", "deliverable_clarity",
            f"Plan: the task asks for a {cue.group(0).lower()}, but "
            f"no step names an output file and the plan does not "
            f"declare an answer-only result. Name the deliverable "
            f"file the user should open, or state explicitly that "
            f"the result is answer-only."))
    return findings


def lint_plan(task: str, steps: list,
              workspace_files=None) -> list[LintFinding]:
    """Lint a proposed plan. `steps` are PlanStep (id, instruction, done_when).

    `workspace_files` optionally gives lint the workspace root file
    set: an iterable of root file names, or a workspace directory
    path. Without it, the unaccounted_file check cannot run and lint
    stays workspace-blind, exactly as before."""
    workspace_set = _workspace_file_set(workspace_files)
    findings: list[LintFinding] = []
    known: set[str] = set(_mentions(task))  # files the task itself names
    whens: list[str] = [task or ""]
    listing_ids: list[int] = []  # earlier steps whose When lists files
    generic_listing = False  # a step generically lists the workspace
    indexes_any = False  # a step's Then uses an indexes clause
    output_files: set[str] = set()  # files the plan names as deliverables
    for idx, s in enumerate(steps):
        known |= _mentions(s.instruction)
        known |= _move_destinations(s.instruction)
        output_files |= _move_destinations(s.instruction)
        if _WRITE_VERB.search(s.instruction or ""):
            # Files a writing step mentions are candidate outputs,
            # except the workspace's own source files: reading every
            # source into a write step does not make them deliverables.
            mentioned = _mentions(s.instruction)
            if workspace_set is not None:
                mentioned -= workspace_set
            output_files |= mentioned
        known |= _mentions(s.given)
        whens.append(s.instruction or "")
        if _GENERIC_LISTING.search(s.instruction or "") \
                or _GENERIC_LISTING.search(s.done_when or ""):
            generic_listing = True
        thens = [t.strip() for t in (s.done_when or "").split("\n") if t.strip()]
        for t in thens:
            parsed = _verify.parse_then(t)
            if parsed is None:
                if _CHECKY.search(t):
                    findings.append(LintFinding(
                        s.id, "warn", "unparseable_check",
                        f"Step {s.id}: Then reads like a check but matches no "
                        f"verifier — it will be taken on trust: {t[:80]}"))
                continue
            _name, path, value = parsed
            if _name == "indexes" and path:
                # The indexes clause is the machine-checked
                # completeness form (the harness builds the target
                # itself, one entry per source file): it accounts
                # for the listing.
                indexes_any = True
            if path:
                # A file a Then checks is a named deliverable target,
                # whatever the verifier: exists, contains, indexes.
                output_files.add(path)
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
        if _LISTING.search(s.instruction or ""):
            listing_ids.append(s.id)
    drift = _intent_drift(task, steps)
    if drift is not None:
        findings.append(drift)
    findings.extend(_attested_compile_warnings(steps))
    findings.extend(_workspace_warnings(
        task, steps, workspace_set, generic_listing, indexes_any,
        output_files))
    return findings
