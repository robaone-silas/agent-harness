"""Clause subjects and intent for Then verification (issue #62).

Split out of verify.py: pure phrasing reads over Then clauses. Which
file a clause names, whether it wants that file gone or left alone,
and which files a step's contents-of excludes clauses subtract from
a deliverable's expected set. None of this touches the workspace.
verify.py re-exports these names, so existing imports keep working.
"""
from __future__ import annotations

import re as regular_expression

from .verify_registry import _QTEXT, VERIFIERS, parse_then
from .verify_text import _pick

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


def _is_path_shaped(name: str) -> bool:
    if not name or any(character.isspace() for character in name):
        return False
    return "/" in name or bool(regular_expression.search(r"\.[\w-]+$", name))


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
    name = _pick(*quoted_match.groups()).strip()
    return name if _is_path_shaped(name) else None


def _is_absence_clause(clause: str) -> bool:
    """Absence-intent wording, unless the clause is a retired covers
    clause: that one promises its target exists, even when the
    filename itself contains an absence word."""
    if _legacy_covers_target(clause) is not None:
        return False
    return bool(_ABSENCE_INTENT.search(clause))


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


def _subject_predicate_matches(clause: str, predicate: regular_expression.Pattern) -> bool:
    """True when the predicate sits directly after the clause's
    leading quoted subject."""
    subject_match = _LEADING_SUBJECT.match(clause)
    if subject_match is None:
        return False
    remainder = clause[subject_match.end():].strip()
    return bool(predicate.match(remainder))


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
    clause = clause or ""
    parsed = parse_then(clause)
    if parsed is not None:
        return parsed[0] == "not_exists"
    if _legacy_covers_target(clause) is not None:
        return False
    return _subject_predicate_matches(clause, _ABSENCE_PREDICATE)


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
    clause = clause or ""
    if parse_then(clause) is not None:
        return False
    return _subject_predicate_matches(clause, _PRESERVATION_PREDICATE)


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


def _clauses(step) -> list[str]:
    """A step's non-empty Then clauses, stripped."""
    stripped_clauses = (clause.strip() for clause in (step.done_when or "").split("\n"))
    return [clause for clause in stripped_clauses if clause]


def _excluded_source(clause: str, excludes_pattern) -> tuple[str, str] | None:
    """(deliverable, excluded source) for a contents-of excludes
    clause, or None for any other clause."""
    parsed = parse_then(clause)
    if parsed is None or parsed[0] != "excludes" or parsed[1] is None:
        return None
    match = excludes_pattern.match(clause)
    if match is None:
        return None
    contents_relative_path = _pick(*match.groups()[4:8])
    if contents_relative_path is None:
        return None
    return parsed[1], contents_relative_path


def _excluded_from_step(step, current_step_id: int, excludes_pattern) -> dict[str, set[str]]:
    """Per-deliverable excluded filenames from one step's contents-of
    excludes clauses: same deliverable only, current or earlier
    steps only. A clause that does not parse, or whose contents
    source is prose, yields nothing."""
    excluded: dict[str, set[str]] = {}
    if step.id > current_step_id:
        return excluded
    for clause in _clauses(step):
        found = _excluded_source(clause, excludes_pattern)
        if found is not None:
            deliverable, source = found
            excluded.setdefault(deliverable, set()).add(source)
    return excluded


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
    excludes = next((registered for registered in VERIFIERS if registered.name == "excludes"), None)
    if excludes is None:
        return excluded
    for step in steps or []:
        for deliverable, sources in _excluded_from_step(step, current_step_id, excludes.pattern).items():
            excluded[deliverable] = excluded.get(deliverable, set()) | sources
    return excluded
