"""The executor's step prompt (issue #62).

Split out of planner.py: everything one plan step's sub-run is told.
The plan frame and outline, the plan-graph node contract, the
step's operative block, and the refusal hint naming its Then-only
output files. Pure text building; nothing here runs a model or
touches the workspace. planner.py re-exports FRAME_TEXT,
_plan_outline, and _graph_contract_block, so existing imports keep
working.
"""
from __future__ import annotations

import re as _re

from . import graphcheck as _graphcheck
from .plan_model import PlanStep

# The plan frame (v0.8.1): every step prompt opens by explaining the
# situation the executor is in. v0.6 scoped the prompt to the current step
# only, fearing the model would freelance across steps; three field
# failures (a lost file list, an evaporated deliverable, an ungrounded
# write step) were all starvation instead. So the executor now sees the
# whole plan, its place in it, and where the data lives — while the
# operative instruction stays scoped and last.
FRAME_TEXT = (
    "How this works: this task is a multi-step plan, carried out one step "
    "at a time in separate runs like this one. Steps marked [done] are "
    "finished: do not redo them. Their full outputs, tool results "
    "included, are stored in the files named beside them, and the short "
    "summaries lose details. Steps marked [pending] run after you, in "
    "their own runs: do not do them, but they will read your stored "
    "output, so finish your step completely. If your step's work is based "
    "on an earlier step's output, read that step's file first with "
    "read_file instead of working from the summary. Your own step's "
    "output file is different: the harness writes it after you finish, "
    "it does not exist yet, and it is not a source. Do not try to read it."
)


def _format_outline_step_result(step: PlanStep) -> str:
    body = step.result or ""
    if body.strip().upper().startswith("DONE:"):
        body = body.strip()[5:].strip()
    if body:
        return f"    Result: {step.result[:200]}"
    if step.tool_summary:
        return f"    Result: (no summary given) {step.tool_summary}"
    if step.result:
        return f"    Result: {step.result[:200]}"
    return ""


def _outline_status_marker(step: PlanStep, current_step_id: int) -> str:
    if step.id == current_step_id:
        return "YOUR STEP"
    if step.status == "done":
        return "done"
    return "pending"


def _format_outline_step(step: PlanStep, current_step_id: int) -> list[str]:
    status_marker = _outline_status_marker(step, current_step_id)
    title = _re.sub(r"^Step\s+\d+\s*[-:]\s*", "", (step.title or "").strip())
    label = title or (step.instruction.split("\n")[0][:100] if step.instruction else "")
    lines = [f"  Step {step.id} [{status_marker}] {label}"]

    if step.id != current_step_id and step.instruction and title:
        lines.append(f"    When: {step.instruction.split(chr(10))[0][:160]}")

    if step.status == "done" and step.id != current_step_id:
        result_line = _format_outline_step_result(step)
        if result_line:
            lines.append(result_line)
        if step.output_path:
            lines.append(f"    Full output: {step.output_path}")
    return lines


def _plan_outline(steps: list[PlanStep], current_step_id: int) -> str:
    """The shape of the whole plan, one line per step, with statuses.

    Done steps are annotated with their (short) result and, when the run
    store is active, the exact file holding their full output."""
    lines = ["The plan:"]
    for plan_step in steps:
        lines.extend(_format_outline_step(plan_step, current_step_id))
    return "\n".join(lines) + "\n"


def _describe_contract_input(label: str, producers: dict) -> str:
    if label in producers:
        return f'"{label}" (produced by step {producers[label]})'
    if label.startswith("files from step ") or (label.startswith("step ") and label.endswith(" output")):
        return f'"{label}"'
    return f'"{label}" (from the task or the workspace)'


def _graph_contract_block(task: str, step: PlanStep,
                          steps: list[PlanStep]) -> str:
    """The step's node contract from the plan graph, for its prompt.

    Experimental (Config.graph_contracts): the executor is told, in
    field terms, what inputs it may rely on and who produced them, and
    what outputs it must produce, instead of inferring the data flow
    from prose. The graph is computed from the plan text alone (no
    workspace scan), so the contract is the same graph the approval
    display derives from the plan, and it does not drift as steps
    create files. Returns "" when the step has no fields."""
    graph = _graphcheck.build_graph(task, steps)
    graph_node = next((node for node in graph.nodes if node.step_id == step.id), None)
    if graph_node is None or (not graph_node.inputs and not graph_node.outputs):
        return ""
    producers = {edge.field: edge.producer for edge in graph.edges
                 if edge.consumer == step.id}
    lines = ["Your place in the plan graph (computed from the plan, "
             "deterministic):"]
    if graph_node.inputs:
        described_inputs = [_describe_contract_input(input_field, producers) for input_field in graph_node.inputs]
        lines.append("  Inputs available to you: " + "; ".join(described_inputs) + ".")
    if graph_node.outputs:
        quoted_outputs = [f'"{output_field}"' for output_field in graph_node.outputs]
        lines.append("  Outputs this step must produce: " + "; ".join(quoted_outputs) + ".")
        lines.append("  This step is complete only when every output "
                     "above exists with its content in place, under "
                     "exactly that name. Later steps consume these "
                     "outputs as named.")
    return "\n".join(lines) + "\n"


def _listed_outputs(output_files: list[str]) -> str:
    return ", ".join(f"'{output_file}'" for output_file in output_files)


def _refusal_hint(output_files: list[str]) -> str | None:
    """Issue #48: a refused write's feedback names the files the
    step's Thens name that its When does not, so a plan that reaches
    execution with the gap still gives the step a way to recover."""
    if not output_files:
        return None
    return (f"HARNESS: this step's output file is "
            f"{_listed_outputs(output_files)} (named in the step's Then). Write "
            f"to that file instead.")


# Wording matters (field, 2026-10-03): "Your full output will be
# stored at..." read as a write instruction and a step wrote its
# deliverable into its own record file. Say who does the storing.
def _store_notice(output_path: str) -> str:
    return (f"The harness will store your full output at "
            f"{output_path} for later steps. Do not write to "
            f"that file yourself, and do not try to read it: it "
            f"does not exist yet. The harness writes it after you "
            f"finish. The files you can read are the earlier "
            f"steps' outputs, named beside the [done] steps "
            f"above.\n")


def _operative_block(step: PlanStep, output_files: list[str]) -> str:
    """The current step itself, repeated last as the operative block."""
    block = ""
    if step.given:
        block += f"Starting state: {step.given}\n"
    if step.intent:
        block += f"Intent of this step: {step.intent}\n"
    block += f"Step: {step.instruction}\n"
    block += f"This step is done when: {step.done_when or 'its instruction is complete'}\n"
    if output_files:
        block += _output_files_line(output_files)
    return block


def _output_files_line(output_files: list[str]) -> str:
    return (f"Output file for this step: {_listed_outputs(output_files)} (named "
            f"in this step's Then). Write this step's "
            f"result to that file.\n")


def _step_prompt(task: str, step: PlanStep, steps: list[PlanStep],
                 output_files: list[str], contract: str,
                 store_active: bool, feedback_message: str) -> str:
    """The full prompt for one attempt at one plan step."""
    prompt = (f"Overall goal: {task}\n"
              f"You are executing step {step.id} of {len(steps)}. "
              f"Do ONLY this step, nothing else.\n\n")
    prompt += FRAME_TEXT + "\n\n"
    prompt += _plan_outline(steps, step.id) + "\n"
    prompt += _operative_block(step, output_files)
    prompt += contract
    if store_active and step.output_path:
        prompt += _store_notice(step.output_path)
    if feedback_message:
        prompt += (f"Your previous attempt failed verification: {feedback_message} "
                   f"Fix exactly this and try again.\n")
    prompt += 'When this step is complete, answer starting with "DONE:".'
    return prompt
