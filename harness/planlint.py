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
                     while the plan writes hello.txt).
  value_from_nowhere (WARN): a Then asserts an exact value appearing nowhere
                     in the task or prior steps — computed or invented?
  unparseable_check  (WARN): a Then reads like a check ("contains exactly",
                     "should exist", ...) but matches no verifier, so the
                     harness will take it on trust.
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
    r"""has\s+\d+\s+lines?""",
    re.IGNORECASE,
)


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


def lint_plan(task: str, steps: list) -> list[LintFinding]:
    """Lint a proposed plan. `steps` are PlanStep (id, instruction, done_when)."""
    findings: list[LintFinding] = []
    known: set[str] = set(_mentions(task))  # files the task itself names
    whens: list[str] = [task or ""]
    for s in steps:
        known |= _mentions(s.instruction)
        known |= _mentions(s.given)
        whens.append(s.instruction or "")
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
    return findings
