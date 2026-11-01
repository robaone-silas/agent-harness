"""Plan requests: model output to validated PlanSteps (issue #62).

Split out of planner.py: asking the model for a Gherkin plan,
turning a parsed feature into PlanSteps, the parse-failure feedback
that teaches the grammar at the failure point, and the plan lint
pass that can trigger the one retry. planner.py re-exports these
names, so existing imports keep working.
"""
from __future__ import annotations

from . import gherkin
from .config import Config
from .plan_model import PlanStep, step_budget
from .plan_prompts import _root_workspace_files, build_planner_prompt


def to_plan_steps(plan: gherkin.FeaturePlan) -> list[PlanStep]:
    steps = []
    for step_index, step_definition in enumerate(plan.steps, 1):
        steps.append(PlanStep(
            id=step_index,
            instruction="\n".join(step_definition.when)[:500],
            # done_when is the step's verification contract: badges, lint,
            # executor prompts, and verification all consume it. It is NOT
            # length-capped. A 300-char cap silently truncated combined
            # Then clauses mid-clause (issue #7: a six-clause step lost its
            # tail), so every clause is preserved exactly.
            done_when="\n".join(step_definition.then),
            given="\n".join(step_definition.given)[:300],
            title=(step_definition.title or "")[:120],
            intent=(step_definition.intent or "")[:300],
        ))
    for plan_step in steps:
        plan_step.budget = step_budget(plan_step, steps)
    return steps


_PLAN_SHAPE_FEEDBACK = (
    "A plan contains only Feature, Scenario, Intent, and Given/When/Then "
    "lines. Notes and findings are background for writing steps, never "
    "lines in the plan.\n"
    "Shape:\n"
    "  Feature: <name>\n"
    "    Scenario: Step 1 - <goal>\n"
    "      When I <action>\n"
    "      Then <checkable result>"
)


def _normalize_for_matching(text: str) -> str:
    """Collapse whitespace and case so two texts can be compared
    without formatting differences getting in the way."""
    return " ".join((text or "").split()).lower()


def _offending_line(parse_error: str, response: str) -> str:
    """The plan line the parse error names, normalized, or "".

    The parser numbers lines in the fence-stripped text, so the line
    is read from that same text: indexing the raw response lands one
    line early whenever the model wrapped its plan in a Markdown
    fence."""
    import re
    line_match = re.search(r"line (\d+)", parse_error or "")
    if not line_match:
        return ""
    plan_lines = gherkin._strip_fences(response or "").splitlines()
    line_number = int(line_match.group(1))
    if not 1 <= line_number <= len(plan_lines):
        return ""
    return _normalize_for_matching(plan_lines[line_number - 1])


def _offending_line_from_notes(parse_error: str, response: str,
                               discovery_notes: str) -> bool:
    if not discovery_notes:
        return False
    offending_line = _offending_line(parse_error, response)
    return bool(offending_line) and offending_line in _normalize_for_matching(discovery_notes)


def _parse_feedback(parse_error: str, response: str,
                    discovery_notes: str = "") -> str:
    """The full rejection message for a plan that fails Gherkin parse.

    The parser's error names the symptom; the retry stands a better
    chance when the message also teaches at the failure point: the
    grammar in one sentence, a skeleton to copy, and, when the
    offending line was pasted out of the discovery notes (Tomas's
    2026-10-08 yard inventory: a findings bullet sitting where a
    step belonged, and the same mistake on the retry), that
    confusion named directly.
    """
    message_parts = [f"That plan was rejected: {parse_error}."]
    if _offending_line_from_notes(parse_error, response, discovery_notes):
        message_parts.append(
            "That line came from the discovery notes; "
            "do not paste findings into the plan.")
    message_parts.append(_PLAN_SHAPE_FEEDBACK)
    message_parts.append(
        "Return ONLY the corrected Gherkin, no other text.")
    return "\n".join(message_parts)


def _attach_lint(task: str, steps: list[PlanStep], config: Config) -> list:
    """Run the plan lint, attach each step's findings as step.lint,
    and return the error-level findings."""
    from . import planlint as _planlint
    findings = _planlint.lint_plan(task, steps, workspace_files=_root_workspace_files(config.workspace))
    findings_by_step: dict[int, list] = {}
    for finding in findings:
        findings_by_step.setdefault(finding.step_id, []).append(finding.as_dict())
    for plan_step in steps:
        plan_step.lint = findings_by_step.get(plan_step.id, [])
    return [finding for finding in findings if finding.level == "error"]


def request_plan(task: str, tool_names: list[str], chat_fn, config: Config,
                 discovery: str = ""
                 ) -> tuple[str | None, list[PlanStep] | str]:
    """Ask the model for a Gherkin plan; retry with rejections explained.

    Two validations run per proposal: structural (Gherkin parse) then design
    (deterministic plan lint). Either can trigger the one retry; findings are
    attached to the returned steps as s.lint for the approval display.
    Returns (gherkin_text, steps) or (None, error_string)."""
    messages = [{"role": "user",
                 "content": build_planner_prompt(task, tool_names,
                                                  workspace=config.workspace,
                                                  discovery=discovery)}]
    last_problem = "no response"
    for attempt in (1, 2):
        try:
            response_message = chat_fn(messages, None)
        except Exception as error:
            return None, f"planner call failed: {error}"
        content = (response_message.get("content") or "").strip()
        plan, parse_error = gherkin.parse_feature(content)
        if plan is None:
            feedback = _parse_feedback(parse_error, content, discovery)
            last_problem = parse_error
        else:
            steps = to_plan_steps(plan)
            error_findings = _attach_lint(task, steps, config)
            if not error_findings or attempt == 2:
                return content, steps
            last_problem = "; ".join(finding.detail for finding in error_findings)
            feedback = ("That plan has design flaws: " + last_problem
                        + ". Return ONLY the corrected Gherkin, no other text.")
        messages.append({"role": "assistant", "content": content})
        messages.append({"role": "user", "content": feedback})
    return None, last_problem
