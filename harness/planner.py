"""Plan-then-execute mode, v0.6: plans are Gherkin.

Measured e2b behavior: reliable single atomic actions, no sequencing,
no completion tracking. So the harness takes over the sequencing, and the
plan itself is a Gherkin feature file — one Scenario per step — which is
the shared language between the planner, the human approving the plan,
and the executor:

  1. Propose: ask the model (once) for a Gherkin plan; parse and validate
     it. A plan with a missing Then or an unresolvable <placeholder> is
     rejected here, never executed.
  2. Approve (human): the .feature file is reviewed/edited, Then clauses
     first — they define what "done" means.
  3. Execute: each Scenario runs as one small scoped instruction, tracked
     step by step in code.

`run_planned` does propose+execute in one go (auto mode, what the eval
uses). `propose` + `execute_plan` are the separable halves for the
approval flow.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from . import gherkin
from . import verify as _verify
from .config import Config
from .discovery import (READ_ONLY_TOOLS, _discovery_steering, _is_exhaustive_task,
                        build_discovery_prompt, discovery_findings_text,
                        request_discovery_plan, unexamined_workspace_files)
from .grounding import _grounding_gate, _then_named_outputs
from .plan_model import PlanStep, _parse_step_segment_tokens, _step_refs, step_budget
from .plan_prompts import (_EXAMPLE_PLANS, _idiom_block, _root_workspace_files,
                           build_planner_prompt, workspace_digest)
from .plan_request import _parse_feedback, request_plan, to_plan_steps
from .run_metrics import (_is_content_bearing_pass, _record_attempt_metrics,
                          _write_step_to_store)
from .step_executor import _index_describe, _run_step_verified
from .step_prompt import FRAME_TEXT, _graph_contract_block, _plan_outline

logger = logging.getLogger(__name__)


def _default_chat_fn(config: Config):
    from . import client as _client

    def chat_fn(messages, tool_definitions):
        tools = _client.to_ollama_tools(tool_definitions) if tool_definitions else None
        return _client.chat(
            config.endpoint, config.model, messages,
            tools=tools,
            temperature=config.temperature, top_p=config.top_p,
            timeout=config.request_timeout)

    return chat_fn


def _chat_and_tools(config: Config, chat_fn=None):
    from . import tools as _tools
    effective_chat_fn = chat_fn or _default_chat_fn(config)
    jail = _tools.Jail(config.workspace)
    registry = _tools.make_tools(jail, config.blocked_substrings, config.exec_timeout, config.max_output_chars)
    return effective_chat_fn, jail, registry


def propose(task: str, config: Config, chat_fn=None
            ) -> tuple[str | None, list[PlanStep] | str]:
    """Propose a Gherkin plan without executing it. Returns (gherkin_text,
    steps) or (None, error_string)."""
    chat_fn, jail, registry = _chat_and_tools(config, chat_fn)
    return request_plan(task, list(registry), chat_fn, config)


def plan_with_discovery(task: str, config: Config, chat_fn=None, on_event=None
                        ) -> tuple[str | None, list[PlanStep] | str, dict]:
    """Tier 2: discovery pass, then the real plan written with the
    findings in hand. Returns (gherkin_text, steps, info) or
    (None, error, info); info carries the discovery run id, its status,
    and the findings text. A failed discovery execution still replans
    with whatever findings completed steps produced."""
    chat_fn, jail, registry = _chat_and_tools(config, chat_fn)
    discovery_text, discovery_steps = request_discovery_plan(task, config, chat_fn)
    if discovery_text is None:
        failed_info = {"discovery_run_id": "", "discovery_status": "plan_failed", "findings": ""}
        return None, discovery_steps, failed_info
    discovery_run = execute_plan(task, discovery_steps, config, chat_fn=chat_fn,
                                 on_event=on_event, only_tools=list(READ_ONLY_TOOLS))
    findings = discovery_findings_text(config.workspace, discovery_run.run_id, len(discovery_steps),
                                       discovery_steps=discovery_steps)
    text, steps = request_plan(task, list(registry), chat_fn, config,
                               discovery=findings)
    info = {"discovery_run_id": discovery_run.run_id,
            "discovery_status": discovery_run.status,
            "findings": findings,
            "discovery_steps": discovery_steps}
    return text, steps, info


@dataclass
class PlannedRun:
    status: str  # done | done_on_trust | plan_failed | step_failed | model_error
    answer: str
    steps: list[PlanStep] = field(default_factory=list)
    run_id: str = ""  # .harness/runs/<run_id>/ for this run (v0.8.1)
    trust_only: list = field(default_factory=list)  # [{"step": id,
        # "deliverables": [names]}] for steps whose deliverable rests
        # entirely on attested checks (issue #37)

    def plan_dict(self) -> dict:
        return {"run_id": self.run_id,
                "trust_only": self.trust_only,
                "steps": [
            {"id": plan_step.id, "instruction": plan_step.instruction, "done_when": plan_step.done_when,
             "intent": plan_step.intent, "budget": plan_step.budget,
             "status": plan_step.status, "result": plan_step.result[:500],
             "verify": plan_step.verify, "lint": plan_step.lint,
             "verify_waived": plan_step.verify_waived,
             "done_via_world_state": plan_step.done_via_world_state,
             "output_path": plan_step.output_path} for plan_step in self.steps]}


@dataclass
class PlannedRun:
    status: str  # done | done_on_trust | plan_failed | step_failed | model_error
    answer: str
    steps: list[PlanStep] = field(default_factory=list)
    run_id: str = ""  # .harness/runs/<run_id>/ for this run (v0.8.1)
    trust_only: list = field(default_factory=list)  # [{"step": id,
        # "deliverables": [names]}] for steps whose deliverable rests
        # entirely on attested checks (issue #37)

    def plan_dict(self) -> dict:
        return {"run_id": self.run_id,
                "trust_only": self.trust_only,
                "steps": [_step_dict(plan_step) for plan_step in self.steps]}


def _step_dict(plan_step: PlanStep) -> dict:
    return {"id": plan_step.id, "instruction": plan_step.instruction, "done_when": plan_step.done_when,
            "intent": plan_step.intent, "budget": plan_step.budget,
            "status": plan_step.status, "result": plan_step.result[:500],
            "verify": plan_step.verify, "lint": plan_step.lint,
            "verify_waived": plan_step.verify_waived,
            "done_via_world_state": plan_step.done_via_world_state,
            "output_path": plan_step.output_path}


def _open_run_store(config: Config, steps: list[PlanStep]):
    """The run store with every step's output path assigned, or None.
    Record-keeping must never block execution, so a store that cannot
    open is logged and the run goes on without one."""
    try:
        from .runstore import RunStore
        run_store = RunStore(config.workspace)
        for plan_step in steps:
            plan_step.output_path = run_store.rel_path(plan_step.id)
        return run_store
    except Exception:
        logger.warning("run store unavailable; running without step records", exc_info=True)
        return None


def _missing_deliverables_answer(missing_deliverables: list[str]) -> str:
    return ("The plan's steps finished, but this run is a "
            "failure: promised deliverable(s) were never "
            "created: " + ", ".join(missing_deliverables) + ". A run only "
            "reports done when every file the plan promises "
            "exists.")


def _trust_only_notice(trust_only_entries: list) -> str:
    named_steps = [f"step {entry['step']} ({', '.join(entry['deliverables'])})" for entry in trust_only_entries]
    names = "; ".join(named_steps)
    return (f"\nSteps finished on trust: {names}. Their Then "
            f"clauses were attested, not machine-checked, so "
            f"their deliverables' contents were never verified. "
            f"Say plainly which steps finished on trust.")


def _summary_prompt(task: str, status: str, steps: list[PlanStep],
                    trust_only_entries: list) -> str:
    step_lines = [f"Step {plan_step.id} [{plan_step.status}]: {plan_step.instruction}"
                  + (f" -> {plan_step.result[:200]}" if plan_step.result else "")
                  for plan_step in steps]
    summary = f"Overall goal: {task}\nPlan execution ended: {status}.\n" + "\n".join(step_lines)
    if trust_only_entries:
        summary += _trust_only_notice(trust_only_entries)
    return summary + '\nSummarize the outcome in 2-4 sentences, starting with "DONE:".'


def _summarize(chat_fn, summary_prompt: str) -> str:
    try:
        response_message = chat_fn([{"role": "user", "content": summary_prompt}], None)
        return (response_message.get("content") or "").strip() or "(no summary)"
    except Exception as error:
        return f"(summary call failed: {error})"


def _run_steps(task: str, steps: list[PlanStep], config: Config, chat_fn, jail,
               emit_event, run_store, only_tools) -> str:
    """Run the steps in order, stopping at the first failure; the
    run's status before the end-of-run audits."""
    for step in steps:
        emit_event("step_start", step)
        if not _run_step_verified(task, step, steps, config, chat_fn, jail, emit_event,
                                  run_store=run_store, only_tools=only_tools):
            break
    all_done = steps and all(plan_step.status == "done" for plan_step in steps)
    return "done" if all_done else "step_failed"


def execute_plan(task: str, steps: list[PlanStep], config: Config, chat_fn=None,
                 on_event=None, only_tools=None) -> PlannedRun:
    """Execute an approved plan, one scoped step at a time. on_event(kind, *args)
    reports ("step_start", step), ("step_sub", step, sub), ("step_verify", step,
    StepVerify), ("step_verify_waived", step, StepVerify), ("step_done", step),
    ("step_failed", step). A waived verification still counts the step done.
    Issue #22 adds ("deliverables_missing", [paths]): after all steps, the
    promised-deliverable audit can still fail the run (see below). The
    world-state override adds ("step_done_world_state", step, StepVerify):
    a sub-run that missed DONE but whose verification passes counts the
    step done, recorded on the step as done_via_world_state."""
    chat_fn, jail, _registry = _chat_and_tools(config, chat_fn)

    def emit_event(kind, *args):
        if on_event:
            on_event(kind, *args)

    run_store = _open_run_store(config, steps)
    run_id = run_store.run_id if run_store is not None else ""
    status = _run_steps(task, steps, config, chat_fn, jail, emit_event, run_store, only_tools)
    if status != "done":
        return PlannedRun(status, _summarize(chat_fn, _summary_prompt(task, status, steps, [])),
                          steps, run_id=run_id, trust_only=[])

    # Issue #22: the deliverable audit. Steps can finish with their
    # verification waived, and a waived step still counts done, so the
    # per-step machinery alone cannot promise the user a deliverable.
    # Before the run may report done, every file the plan's Thens
    # promised must exist. This judges existence only, neither plan
    # adherence nor content quality, so it does not reopen the
    # warn-downgrade decision: a warning can excuse a wrong check,
    # never a missing delivery.
    missing_deliverables = _verify.missing_deliverables(steps, jail)
    if missing_deliverables:
        emit_event("deliverables_missing", missing_deliverables)
        return PlannedRun("step_failed", _missing_deliverables_answer(missing_deliverables),
                          steps, run_id=run_id)

    # Issue #37: a run that finished with a deliverable resting
    # entirely on attested checks must not report plain done. The
    # trust-only steps are named in the final status, while the run
    # still counts as completed (see run.py's exit mapping).
    trust_only_entries = _verify.trust_only_steps(steps)
    if trust_only_entries:
        status = "done_on_trust"
    answer = _summarize(chat_fn, _summary_prompt(task, status, steps, trust_only_entries))
    return PlannedRun(status, answer, steps, run_id=run_id, trust_only=trust_only_entries)


def run_planned(task: str, config: Config, chat_fn=None, on_event=None) -> PlannedRun:
    """Auto mode: propose a plan and execute it in one go. The eval uses this."""
    gherkin_text, planned_steps = propose(task, config, chat_fn)
    if gherkin_text is None:
        return PlannedRun("plan_failed", f"Could not get a valid plan: {planned_steps}", [])
    if on_event:
        on_event("plan", planned_steps)
    return execute_plan(task, planned_steps, config, chat_fn, on_event)
