"""Plan model and per-step budgeting (issue #62).

Split out of planner.py: the deterministic half of planning. A
PlanStep is the plan's unit of work, _step_refs reads which earlier
steps a step's text draws on, and step_budget turns that into the
step's turn budget. None of this touches the workspace or calls a
model, so it stays importable without planner's execution half.
planner.py re-exports PlanStep, step_budget, _step_refs, and
_parse_step_segment_tokens, so existing imports keep working.
"""
from __future__ import annotations

import re as _re
from dataclasses import dataclass, field


@dataclass
class PlanStep:
    id: int
    instruction: str
    done_when: str
    given: str = ""
    title: str = ""  # Scenario title from the Gherkin (v0.8.1 plan frame)
    intent: str = ""  # the step's Intent: line (v0.8.1 intent preservation)
    status: str = "pending"  # pending | done | failed
    result: str = ""
    verify: list = field(default_factory=list)  # per-Then check records (v0.7)
    lint: list = field(default_factory=list)  # planlint findings (dicts)
    verify_waived: bool = False  # verification failed but step accepted anyway
    done_via_world_state: bool = False  # sub-run missed DONE; verification passed (2026-10-04 ruling)
    output_path: str = ""  # .harness/runs/<run>/step-NN.md (v0.8.1, issue #2)
    source_items: list = field(default_factory=list)  # names this step produced (v0.8.1 covers)
    tool_summary: str = ""  # harness-computed "tools used; output size" line
    budget: int = 0  # effective turn budget for this step (issue #14)
    touched_args: set = field(default_factory=set)  # dumped tool-call args
    # from this step's completed attempts (issue #17 read coverage)


# Dynamic per-step budgets (issue #14): a flat cap judged every step
# against the same number of turns no matter how much work its own plan
# implied, so a compile step needing 7 reads, a write, and the final
# answer died against a cap of 6 with its deliverable unwritten. The
# budget now counts the planned operations deterministically: every
# distinct file operand the step names, plus, for each earlier step it
# draws on, one turn to read that step's stored record and one turn per
# source item that step produced, plus slack for the write and the
# final answer. Floored at the configured base, capped at the ceiling;
# the repeat-call breaker, the error limits, and the pushback refunds
# still bound a step that wanders instead of working.
_BUDGET_SLACK = 2


_RANGE_CONNECTORS = frozenset({"through", "to", "-", "–", "—"})


def _expand_step_range(start: int, end: int) -> set[int]:
    lower_bound, upper_bound = min(start, end), max(start, end)
    if upper_bound - lower_bound <= 64:
        return set(range(lower_bound, upper_bound + 1))
    return {start, end}


def _add_step_token(step_references: set[int], step_number: int,
                    previous_step_number: int | None,
                    connector: str | None) -> None:
    """Fold one digit token into the referenced set (issue #62).

    A range connector after a previous step expands the whole span;
    anything else names the step itself, plus the previous step when
    one is pending (a list member the loop has not flushed yet).
    """
    if previous_step_number is not None and connector in _RANGE_CONNECTORS:
        step_references.update(_expand_step_range(previous_step_number, step_number))
        return
    step_references.add(step_number)
    if previous_step_number is not None:
        step_references.add(previous_step_number)


def _parse_step_segment_tokens(tokens: list[str]) -> set[int]:
    step_references: set[int] = set()
    previous_step_number: int | None = None
    connector: str | None = None
    for token in tokens:
        if not token.isdigit():
            connector = token.lower()
            continue
        step_number = int(token)
        _add_step_token(step_references, step_number,
                        previous_step_number, connector)
        previous_step_number, connector = step_number, None
    if previous_step_number is not None:
        step_references.add(previous_step_number)
    return step_references


_STEP_SINGULAR_PATTERN = _re.compile(r"\bstep\s+(\d+)", _re.IGNORECASE)
_STEP_SEGMENT_PATTERN = _re.compile(
    r"\bsteps?\s+(\d+(?:(?:\s*(?:,|and|through|to|[-–—])\s*)+\d+)+)",
    _re.IGNORECASE)
_STEP_TOKEN_PATTERN = _re.compile(r"\d+|through|to|and|,|[-–—]", _re.IGNORECASE)


def _step_refs_from_segments(text: str) -> set[int]:
    """Step numbers named by plural list and range segments (issue #62)."""
    segment_references: set[int] = set()
    for segment_match in _STEP_SEGMENT_PATTERN.finditer(text):
        tokens = _STEP_TOKEN_PATTERN.findall(segment_match.group(1))
        segment_references.update(_parse_step_segment_tokens(tokens))
    return segment_references


def _step_refs(text: str) -> set[int]:
    """Step numbers a step's text draws on (issue #32).

    Planners refer to earlier steps in whatever phrasing comes
    naturally: singular ("step 1", "step 1 and step 2"), plural lists
    ("steps 1 and 2", "steps 1, 2, and 3"), and ranges ("steps 1
    through 5", "steps 1 to 5", "steps 1-5", "step 1 through 5").
    The budget counter originally recognized only the singular form,
    so a compile step phrased as a plural range counted no prior
    steps at all and kept the flat base. A range counts every step
    in it, a list counts each member: a referenced prior step is
    read once however it was named.
    """
    step_references = {int(matched_step) for matched_step in _STEP_SINGULAR_PATTERN.findall(text)}
    step_references.update(_step_refs_from_segments(text))
    return step_references


def _step_quoted_operands(text: str) -> set[str]:
    return {quoted_token for quoted_token in _re.findall(r'"([^"\n]+)"', text)
            if quoted_token.strip() and not any(character.isspace() for character in quoted_token)}


def _prior_step_cost(prior_step: PlanStep) -> int:
    return 1 + len(prior_step.source_items or [])


def step_budget(step: PlanStep, steps: list[PlanStep], config=None) -> int:
    """Effective turn budget for one plan step, from its planned work."""
    base_budget = getattr(config, "step_max_steps", 6) if config is not None else 6
    ceiling_budget = (getattr(config, "step_budget_ceiling", 24)
               if config is not None else 24)
    step_text = "\n".join([step.given or "", step.intent or "",
                           step.instruction or "", step.done_when or ""])
    operations_count = len(_step_quoted_operands(step_text))
    steps_by_id = {plan_step.id: plan_step for plan_step in steps}
    for step_reference in _step_refs(step_text):
        prior_step = steps_by_id.get(step_reference)
        if prior_step is not None and prior_step.id < step.id:
            operations_count += _prior_step_cost(prior_step)
    return max(base_budget, min(ceiling_budget, operations_count + _BUDGET_SLACK))
