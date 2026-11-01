"""Text analysis for Then verification (issue #62).

Split out of verify.py: pure phrasing work with no registry
dependency. Quoted-group picking, jail resolution, deferral
scanning, listing-item extraction, step-reference parsing,
field matching, Label: value harvesting, and step-record reads.
verify.py re-exports the names its verifiers and callers use, so
existing imports keep working.
"""
from __future__ import annotations

import re as regular_expression

from .tools import Refusal


def _pick(*groups: str | None) -> str | None:
    """First non-None regex group (quoted alternatives are mutually exclusive)."""
    return next((group for group in groups if group is not None), None)


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



def _lines_from_sub_step(sub_step) -> list[str] | None:
    """Name-list lines from one listing-shaped tool result, else None.

    list_dir and grep_files outputs are one workspace-relative name
    per line; exec output counts only when the command is ls or find,
    with exit-status lines stripped. Anything else is prose or
    content, never a name list."""
    if not sub_step.tool or sub_step.result is None:
        return None
    if sub_step.tool in ("list_dir", "grep_files"):
        return sub_step.result.splitlines()
    if sub_step.tool == "exec":
        cmd = str((sub_step.args or {}).get("command", ""))
        if regular_expression.match(r"\s*(ls|find)\b", cmd):
            return [line for line in sub_step.result.splitlines()
                    if not line.startswith("exit=")]
    return None


def _append_item_names(lines: list[str], items: list[str]) -> None:
    """Append cleaned, deduped item names from lines to items."""
    for line in lines:
        name = line.strip().rstrip("/")
        if not name or name in ("(empty)", "(no matches)"):
            continue
        if name.startswith("...[") or name.startswith("exit="):
            continue
        if name not in items:
            items.append(name)


def collect_step_items(sub_steps) -> list[str]:
    """Extract the item names a step produced, from listing-shaped tool
    results: list_dir and grep_files outputs (one workspace-relative name
    per line) and `ls`/`find` exec output. Other tools' results are prose
    or content, not name lists, and are never mined. Deterministic, deduped,
    order preserved."""
    items: list[str] = []
    for sub_step in sub_steps:
        lines = _lines_from_sub_step(sub_step)
        if lines is None:
            continue
        _append_item_names(lines, items)
    return items



# A step-reference phrase inside prose: singular, plural lists, and
# ranges. Same grammar as plan_model._step_refs; verify_text.py cannot import
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


def _refs_from_segment(segment_text: str) -> list[int]:
    """Step numbers one plural segment names, ranges expanded.

    One connector per numeric gap: the last separator spoken before
    the next number governs that gap, so repeated separators
    (", and") cannot shift the pairing between numbers and
    connectors. The token walk in plan_model._step_refs pairs them the
    same way."""
    numbers: list[int] = []
    gaps: list[str] = []
    pending: list[str] = []
    for token in _PRES_TOKEN.findall(segment_text):
        if token.isdigit():
            if numbers:
                gaps.append(pending[-1].strip().lower()
                            if pending else "")
            numbers.append(int(token))
            pending = []
        else:
            pending.append(token)
    refs: list[int] = []
    for index, number in enumerate(numbers):
        if (index + 1 < len(numbers) and gaps[index] in _PRES_RANGE_SEPS):
            first, second = number, numbers[index + 1]
            if 0 < abs(second - first) <= 64:
                refs.extend(range(min(first, second), max(first, second) + 1))
                continue
        refs.append(number)
    return refs


def _refs_in(text: str) -> list[int]:
    refs = [int(step_number_string) for step_number_string in regular_expression.findall(r"\bstep\s+(\d+)", text or "",
                                       regular_expression.IGNORECASE)]
    for match in _PRES_SEG.finditer(text or ""):
        refs.extend(_refs_from_segment(match.group(1)))
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

