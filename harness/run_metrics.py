"""Run metrics and step persistence (issue #62).

Split out of planner.py: what a run records about itself. The
content-bearing verdict set, the per-attempt tool-call accounting,
and the best-effort write of each step's record to the run store.
planner.py re-exports _record_attempt_metrics and
_write_step_to_store, so existing imports keep working.
"""
from __future__ import annotations

import logging

from .plan_model import PlanStep

logger = logging.getLogger(__name__)


_CONTENT_BEARING_VERIFIERS = frozenset({
    "contains", "contains_exactly", "indexes", "accounts_for", "has_lines"
})


def _is_content_bearing_pass(verification_result) -> bool:
    if not verification_result.checks:
        return False
    if any(not check.ok or check.verifier == "attest" for check in verification_result.checks):
        return False
    return any(check.verifier in _CONTENT_BEARING_VERIFIERS for check in verification_result.checks)


def _collect_attempt_args(attempts: list[dict], step: PlanStep) -> None:
    """Dump every attempted sub-step's tool-call args onto the step,
    for the read-coverage audit (issue #17)."""
    import json as _json
    attempted_sub_steps = [sub_step for attempt in attempts for sub_step in attempt.get("sub_steps") or []]
    called_sub_steps = [sub_step for sub_step in attempted_sub_steps if sub_step.tool and sub_step.args]
    step.touched_args |= {_json.dumps(sub_step.args, default=str) for sub_step in called_sub_steps}


def _last_attempt_tool_summary(last_sub_steps: list) -> str | None:
    """One-line tool usage summary for the last attempt, or None
    when it used no tools."""
    tool_counts: dict[str, int] = {}
    total_output_lines = 0
    for sub_step in last_sub_steps:
        if sub_step.tool:
            tool_counts[sub_step.tool] = tool_counts.get(sub_step.tool, 0) + 1
            total_output_lines += len((sub_step.result or "").splitlines())
    if not tool_counts:
        return None
    return (", ".join(f"{tool_name} x{count}" for tool_name, count in tool_counts.items())
            + f"; {total_output_lines} lines of tool output")


def _record_attempt_metrics(step: PlanStep, attempts: list[dict]):
    if not attempts:
        return
    _collect_attempt_args(attempts, step)
    summary = _last_attempt_tool_summary(attempts[-1].get("sub_steps") or [])
    if summary is not None:
        step.tool_summary = summary


def _write_step_to_store(run_store, step: PlanStep, attempts: list[dict], status: str):
    if run_store is None:
        return
    step_record = dict(
        step_id=step.id, instruction=step.instruction,
        given=step.given, done_when=step.done_when,
        answer=step.result, sub_steps=[], status=status,
        verify_checks=step.verify, verify_waived=step.verify_waived,
        attempts=attempts or None)
    try:
        run_store.write_step(**step_record)
    except Exception:
        # Issue #62: persisting the step record is best-effort, so a
        # failing store still must not fail the run (the old silent
        # pass did the same), but the failure is now logged instead
        # of swallowed silently.
        logger.warning("run store write failed; step record dropped", exc_info=True)

