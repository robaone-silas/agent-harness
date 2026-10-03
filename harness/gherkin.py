"""Gherkin as the shared plan language between human, planner, and executor.

v0.6: the planner proposes plans in Gherkin; the human reviews and edits the
.feature file; the harness parses the approved plan back into steps and
validates it before executing. Validation fails fast — a plan with a missing
Then or an unresolvable <placeholder> is rejected at plan time and never run.
(The e4b lesson: don't execute what you can't parse.)

Subset supported: Feature:, Scenario: (one per plan step, in order),
Given/When/Then with And/But continuations, an optional Intent: line per
Scenario (the step's purpose in the task's own terms, preserved as data
so decomposition cannot silently dissolve it), # comments, blank lines.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

PLACEHOLDER_RE = re.compile(r"<[^<>\n]+>")
_FENCE_START_RE = re.compile(r"^```(?:gherkin|feature)?\s*", re.IGNORECASE)


@dataclass
class ParsedStep:
    title: str
    given: list[str] = field(default_factory=list)
    when: list[str] = field(default_factory=list)
    then: list[str] = field(default_factory=list)
    intent: str = ""  # optional Intent: line (v0.8.1 intent preservation)


@dataclass
class FeaturePlan:
    title: str
    steps: list[ParsedStep]


def render_feature(title: str, steps: list[ParsedStep]) -> str:
    """Render parsed steps as canonical Gherkin (normalizes model output)."""
    lines = [f"Feature: {title.strip() or 'Task plan'}"]
    for i, s in enumerate(steps, 1):
        name = s.title.strip() or f"Step {i}"
        lines.append(f"  Scenario: {name}")
        if s.intent:
            lines.append(f"    Intent: {s.intent}")
        for g in s.given:
            lines.append(f"    Given {g}")
        for w in s.when:
            lines.append(f"    When {w}")
        for t in s.then:
            lines.append(f"    Then {t}")
    return "\n".join(lines) + "\n"


def _strip_fences(text: str) -> str:
    text = (text or "").strip()
    text = _FENCE_START_RE.sub("", text)
    text = re.sub(r"\s*```\s*$", "", text)
    return text.strip()


def parse_feature(text: str) -> tuple[FeaturePlan | None, str]:
    """Parse the Gherkin subset. Returns (FeaturePlan, '') or (None, error)."""
    text = _strip_fences(text)
    title = ""
    scenarios: list[ParsedStep] = []
    current: ParsedStep | None = None
    last_kw: str | None = None  # for And/But continuation

    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^(Feature|Scenario(?:\s+Outline)?|Example)\s*:\s*(.*)$",
                     line, re.IGNORECASE)
        if m:
            kind, rest = m.group(1).lower(), m.group(2).strip()
            if kind == "feature":
                title = rest
                continue
            current = ParsedStep(title=rest or f"Step {len(scenarios) + 1}")
            scenarios.append(current)
            last_kw = None
            continue
        m = re.match(r"^Intent\s*:\s*(.*)$", line, re.IGNORECASE)
        if m:
            if current is None:
                return None, f"line {lineno}: Intent outside any Scenario"
            body = m.group(1).strip()
            if not body:
                return None, f"line {lineno}: empty Intent step"
            if current.intent:
                return None, (f"line {lineno}: duplicate Intent in one "
                              "Scenario — one step, one purpose")
            ph = PLACEHOLDER_RE.search(body)
            if ph:
                return None, (f"line {lineno}: unresolvable placeholder "
                              f"{ph.group(0)} — every step must name concrete "
                              "files, commands, and values")
            current.intent = body
            continue
        m = re.match(r"^(Given|When|Then|And|But)\b\s*(.*)$", line, re.IGNORECASE)
        if not m:
            return None, f"line {lineno}: not a Gherkin step: {line[:80]}"
        kw, body = m.group(1).capitalize(), m.group(2).strip()
        if kw in ("And", "But"):
            if last_kw is None:
                return None, f"line {lineno}: '{kw}' with no preceding step"
            kw = last_kw
        if current is None:
            return None, f"line {lineno}: step outside any Scenario"
        if not body:
            return None, f"line {lineno}: empty {kw} step"
        ph = PLACEHOLDER_RE.search(body)
        if ph:
            return None, (f"line {lineno}: unresolvable placeholder {ph.group(0)} — "
                          "every step must name concrete files, commands, and values")
        getattr(current, kw.lower()).append(body)
        last_kw = kw

    if not scenarios:
        return None, "no Scenario found — the plan needs at least one"
    if len(scenarios) > 8:
        return None, f"plan must have 1-8 steps, got {len(scenarios)}"
    for i, s in enumerate(scenarios, 1):
        if not s.when:
            return None, f"scenario {i} ('{s.title}'): no When step"
        if not s.then:
            return None, f"scenario {i} ('{s.title}'): no Then step"
    return FeaturePlan(title=title or "Task plan", steps=scenarios), ""
