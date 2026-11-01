"""Single-file verifiers (issue #62).

Split out of verify.py: the checks that judge one named file on its
own, with no reference to earlier steps. Registration order inside
this family is the registry contract: contains_exactly before
contains, not_exists before exists. The other families' verbs
(indexes, accounts for, preserves, excludes) never match these
phrasings, so this family can register first as a whole.
"""
from __future__ import annotations

import re as regular_expression

from .verify_registry import (_FILE, _QPATH, _QTEXT, _SHOULD, CheckResult,
                              VerifyContext, verifier)
from .verify_text import _pick, _resolve


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
        detail = f"{relative_path!r} is not a file — expected exactly {text!r}"
        return CheckResult(then, False, "contains_exactly", detail)
    got = resolved_path.read_text().strip()
    if got == text:
        detail = f"{relative_path!r} contains exactly {text!r}"
        return CheckResult(then, True, "contains_exactly", detail)
    detail = f"{relative_path!r}: got {got!r}, expected exactly {text!r}"
    return CheckResult(then, False, "contains_exactly", detail)


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
        detail = f"{relative_path!r} is not a file — expected {expected_line_count} lines"
        return CheckResult(then, False, "has_lines", detail)
    got = len(resolved_path.read_text().splitlines())
    if got == expected_line_count:
        detail = f"{relative_path!r} has {expected_line_count} lines"
        return CheckResult(then, True, "has_lines", detail)
    detail = f"{relative_path!r} has {got} lines, expected {expected_line_count}"
    return CheckResult(then, False, "has_lines", detail)


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
        detail = f"{relative_path!r} is not a file — expected it to contain {text!r}"
        return CheckResult(then, False, "contains", detail)
    if text in resolved_path.read_text():
        return CheckResult(then, True, "contains", f"{relative_path!r} contains {text!r}")
    return CheckResult(then, False, "contains", f"{relative_path!r} does not contain {text!r}")


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
    return CheckResult(then, False, "not_exists", f"{relative_path!r} exists — expected it not to")


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
    return CheckResult(then, False, "exists", f"{relative_path!r} MISSING — expected it to exist")
