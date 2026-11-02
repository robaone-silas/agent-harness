"""Completeness verifiers: indexes and accounts_for (issue #62).

Split out of verify.py. Both judge a deliverable against the item
names an earlier listing step produced, strictly: every expected
item must appear. They differ only in wording, so they share one
check, parameterized by a _CompletenessWording.
"""
from __future__ import annotations

import re as regular_expression
from dataclasses import dataclass

from .verify_registry import _FILE, _QPATH, CheckResult, VerifyContext, verifier
from .verify_text import _deferral_in, _pick, _resolve


@dataclass(frozen=True)
class _CompletenessWording:
    verifier_name: str
    verb_phrase: str        # "indexes" / "accounts for"
    expectation: str        # "index" / "account for"
    checked_word: str       # "coverage" / "accounting"
    deferral_subject: str   # "index" / "document"
    requirement: str


_INDEXES_WORDING = _CompletenessWording(
    verifier_name="indexes", verb_phrase="indexes", expectation="index",
    checked_word="coverage", deferral_subject="index",
    requirement="an index must name every file")

_ACCOUNTS_FOR_WORDING = _CompletenessWording(
    verifier_name="accounts_for", verb_phrase="accounts for",
    expectation="account for", checked_word="accounting",
    deferral_subject="document",
    requirement="every listed file must be accounted for")


def _empty_list_detail(step_number: int, had_items: bool, checked_word: str) -> str:
    """Not-checked detail for a completeness check with no items left.

    Issue #47 option 1: subtraction runs before the empty-set check,
    so a fully excluded listing reports ok with its own detail: it
    had a list, and every item in it was excluded."""
    if had_items:
        return f"all files from step {step_number} excluded: {checked_word} not checked"
    return f"step {step_number} produced no file list: {checked_word} not checked"


def _missing_items_detail(verb_phrase: str, relative_path: str,
                          items: list[str], covered_count: int,
                          missing: list[str], step_number: int,
                          requirement: str) -> str:
    """Failure detail for a completeness check naming what is missing,
    capped at 12 names with a count for the rest."""
    detail = (f"{relative_path!r} {verb_phrase} {covered_count} of {len(items)} files "
              f"from step {step_number}; {requirement}; "
              f"missing include: " + ", ".join(missing[:12]))
    if len(missing) > 12:
        detail += (f" and {len(missing) - 12} more "
                   f"(see step {step_number}'s stored output)")
    return detail


def _expected_items(verify_context: VerifyContext, relative_path: str,
                    step_number: int) -> tuple[list[str], bool]:
    """The items the deliverable must name, and whether the listing
    had any before exclusion.

    Issue #47 option 1: a completeness check honors the plan's
    contents-of excludes clauses for this deliverable. Subtraction
    runs before the empty-set check, so a fully excluded listing
    still reports ok, but with its own detail: it had a list, and
    every item in it was excluded."""
    listed_items = list(verify_context.prior_items.get(step_number) or [])
    excluded = verify_context.excluded_files.get(relative_path) or set()
    remaining = [item for item in listed_items if item not in excluded]
    return remaining, bool(listed_items)


def _completeness_detail(wording: _CompletenessWording, relative_path: str,
                         resolved_path, items: list[str],
                         step_number: int) -> tuple[bool, str]:
    """(ok, detail) for a deliverable that must name every item."""
    if not resolved_path.is_file():
        return False, (f"{relative_path!r} is not a file — expected it to "
                       f"{wording.expectation} the {len(items)} files from "
                       f"step {step_number}")
    content = resolved_path.read_text(errors="replace")
    deferral = _deferral_in(content)
    if deferral:
        return False, (f"{relative_path!r} contains a deferral ({deferral!r}) — "
                       f"the {wording.deferral_subject} must be finished, not promised")
    missing = [item for item in items if item not in content]
    if not missing:
        return True, (f"{relative_path!r} {wording.verb_phrase} all {len(items)} "
                      f"files from step {step_number}")
    return False, _missing_items_detail(
        wording.verb_phrase, relative_path, items, len(items) - len(missing),
        missing, step_number, wording.requirement)


def _check_completeness(match: regular_expression.Match, verify_context: VerifyContext,
                        wording: _CompletenessWording) -> CheckResult:
    relative_path = _pick(*match.groups()[0:4])
    step_number = int(match.group(5))
    then = match.string.strip()
    resolved_path, error = _resolve(verify_context, relative_path)
    if error:
        return CheckResult(then, False, wording.verifier_name, error)
    items, had_items = _expected_items(verify_context, relative_path, step_number)
    if not items:
        detail = _empty_list_detail(step_number, had_items, wording.checked_word)
        return CheckResult(then, True, wording.verifier_name, detail)
    ok, detail = _completeness_detail(wording, relative_path, resolved_path,
                                      items, step_number)
    return CheckResult(then, ok, wording.verifier_name, detail)


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
    return _check_completeness(match, verify_context, _INDEXES_WORDING)


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
    return _check_completeness(match, verify_context, _ACCOUNTS_FOR_WORDING)
