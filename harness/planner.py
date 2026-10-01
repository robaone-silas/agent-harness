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

from dataclasses import dataclass, field, replace

from . import gherkin
from . import loop
from .config import Config


def build_planner_prompt(task: str, tool_names: list[str]) -> str:
    idioms = _idiom_block(task)
    return f"""You are a planner. Break the task below into a short sequence of small steps.
Write the plan in Gherkin — one Scenario per step, in order.

Format:
Feature: <short title for this task>
  Scenario: Step 1 - <short name>
    When <one concrete action naming exact files or commands>
    Then <how to tell the step worked, checkable in files or command output>
  Scenario: Step 2 - <short name>
    When <one concrete action>
    Then <checkable outcome>

Available tools for the steps: {", ".join(tool_names)}.

Rules:
- 2 to 6 scenarios, ordered so each step's output feeds the next.
- Each When is one concrete action; each Then is checkable in files or command output.
- Write Then clauses as direct verifiable phrasings the harness checks itself:
  `"path" exists`, `"path" contains "text"`, `"path" contains exactly "text"`,
  `"path" has 3 lines`. Prefer these over prose — the harness verifies them
  in code after each step, and only falls back to trust for other phrasings.
- The Then must accurately describe THIS step's expected outcome. A precisely
  worded wrong expectation fails verification just as surely as a vague one.
- Concrete values come from the task statement only — never <angle-bracket
  placeholders> or phrases like "content from step 2". For values a step must
  COMPUTE, do not invent the result: write "total.txt contains the computed sum",
  never "total.txt contains exactly "12345"". Acceptance criteria state WHAT to
  check, not the implementation's literal output.
- A Given line is optional, for starting-state context a step needs.
- Do NOT add verify or summarize scenarios — the harness handles finishing.
- Return ONLY the Gherkin, no other text.
{idioms}
Task: {task}"""


def _idiom_block(task: str) -> str:
    """Known idioms matching this task, for the planner prompt (may be empty)."""
    from . import suggestions as _sug
    found = _sug.for_task(task)
    if not found:
        return ""
    return ("\nKnown idioms that apply to this task — use them instead of "
            "hardcoding:\n" + _sug.render(found) + "\n")


@dataclass
class PlanStep:
    id: int
    instruction: str
    done_when: str
    given: str = ""
    status: str = "pending"  # pending | done | failed
    result: str = ""
    verify: list = field(default_factory=list)  # per-Then check records (v0.7)
    lint: list = field(default_factory=list)  # planlint findings (dicts)
    verify_waived: bool = False  # verification failed but step accepted anyway


def to_plan_steps(plan: gherkin.FeaturePlan) -> list[PlanStep]:
    steps = []
    for i, p in enumerate(plan.steps, 1):
        steps.append(PlanStep(
            id=i,
            instruction="\n".join(p.when)[:500],
            done_when="\n".join(p.then)[:300],
            given="\n".join(p.given)[:300],
        ))
    return steps


def request_plan(task: str, tool_names: list[str], chat_fn, cfg: Config
                 ) -> tuple[str | None, list[PlanStep] | str]:
    """Ask the model for a Gherkin plan; retry with rejections explained.

    Two validations run per proposal: structural (Gherkin parse) then design
    (deterministic plan lint). Either can trigger the one retry; findings are
    attached to the returned steps as s.lint for the approval display.
    Returns (gherkin_text, steps) or (None, error_string)."""
    from . import planlint as _planlint
    messages = [{"role": "user",
                 "content": build_planner_prompt(task, tool_names)}]
    last_problem = "no response"
    for attempt in (1, 2):
        try:
            msg = chat_fn(messages, None)
        except Exception as e:
            return None, f"planner call failed: {e}"
        content = (msg.get("content") or "").strip()
        plan, err = gherkin.parse_feature(content)
        if plan is None:
            feedback = f"That plan was rejected: {err}."
            last_problem = err
        else:
            steps = to_plan_steps(plan)
            findings = _planlint.lint_plan(task, steps)
            by_step: dict[int, list] = {}
            for f in findings:
                by_step.setdefault(f.step_id, []).append(f.as_dict())
            for s in steps:
                s.lint = by_step.get(s.id, [])
            errors = [f for f in findings if f.level == "error"]
            if not errors or attempt == 2:
                return content, steps
            feedback = ("That plan has design flaws: "
                        + "; ".join(f.detail for f in errors) + ".")
            last_problem = "; ".join(f.detail for f in errors)
        messages.append({"role": "assistant", "content": content})
        messages.append({"role": "user", "content":
                         feedback + " Return ONLY the corrected Gherkin, no other text."})
    return None, last_problem


def _chat_and_tools(cfg: Config, chat_fn=None):
    from . import client as _client
    from . import tools as _tools
    if chat_fn is None:
        def chat_fn(messages, tool_defs):
            return _client.chat(
                cfg.endpoint, cfg.model, messages,
                tools=_client.to_ollama_tools(tool_defs) if tool_defs else None,
                temperature=cfg.temperature, top_p=cfg.top_p,
                timeout=cfg.request_timeout)
    jail = _tools.Jail(cfg.workspace)
    registry = _tools.make_tools(jail, cfg.blocked_substrings, cfg.exec_timeout,
                                 cfg.max_output_chars)
    return chat_fn, jail, registry


def propose(task: str, cfg: Config, chat_fn=None
            ) -> tuple[str | None, list[PlanStep] | str]:
    """Propose a Gherkin plan without executing it. Returns (gherkin_text,
    steps) or (None, error_string)."""
    chat_fn, jail, registry = _chat_and_tools(cfg, chat_fn)
    return request_plan(task, list(registry), chat_fn, cfg)


@dataclass
class PlannedRun:
    status: str  # done | plan_failed | step_failed | model_error
    answer: str
    steps: list[PlanStep] = field(default_factory=list)

    def plan_dict(self) -> dict:
        return {"steps": [
            {"id": s.id, "instruction": s.instruction, "done_when": s.done_when,
             "status": s.status, "result": s.result[:500],
             "verify": s.verify, "lint": s.lint,
             "verify_waived": s.verify_waived} for s in self.steps]}


def _run_step_verified(task: str, step: PlanStep, n_steps: int,
                     context: list[str], cfg: Config, chat_fn, jail, emit) -> bool:
    """Run one plan step; after the sub-run reports DONE, verify the Then
    clauses in code before advancing. A failed verification warns and retries
    the step once (bounded by cfg.verify_retries) with the exact failure as
    feedback. If verification still fails, the failure is downgraded to a
    warning: the step is accepted and the plan continues, because the verifier
    checks plan *adherence* while the task-level checker judges *correctness* —
    a wrong plan must not block right work. Verification itself costs zero
    model calls. Sub-run failures (model errors, limits) still hard-fail."""
    from . import verify as _verify
    vctx = _verify.VerifyContext(jail=jail, step_id=step.id, task=task)
    feedback = ""
    last_vres = None
    for _ in range(1 + cfg.verify_retries):
        prompt = (f"Overall goal: {task}\n"
                  f"You are executing step {step.id} of {n_steps}. "
                  f"Do ONLY this step, nothing else.\n")
        if step.given:
            prompt += f"Starting state: {step.given}\n"
        prompt += (f"Step: {step.instruction}\n"
                   f"This step is done when: {step.done_when or 'its instruction is complete'}\n")
        if context:
            prompt += ("Results of previous steps (context only, do not redo them):\n"
                       + "\n".join(context) + "\n")
        if feedback:
            prompt += (f"Your previous attempt failed verification: {feedback} "
                       f"Fix exactly this and try again.\n")
        prompt += 'When this step is complete, answer starting with "DONE:".'

        sub_cfg = replace(cfg, max_steps=cfg.step_max_steps)
        try:
            r = loop.run(prompt, sub_cfg, chat_fn=chat_fn,
                         on_step=lambda s: emit("step_sub", step, s))
        except Exception as e:
            step.status = "failed"
            step.result = f"harness error: {e}"
            emit("step_failed", step)
            return False
        step.result = (r.answer or "")[:800]
        if r.status != "done":
            step.status = "failed"
            emit("step_failed", step)
            return False
        thens = [t.strip() for t in step.done_when.split("\n") if t.strip()]
        vres = _verify.verify_step(thens, vctx)
        step.verify = [{"then": c.then, "verifier": c.verifier,
                        "ok": c.ok, "detail": c.detail} for c in vres.checks]
        emit("step_verify", step, vres)
        if vres.ok:
            step.status = "done"
            context.append(f"- Step {step.id}: {step.result[:250]}")
            emit("step_done", step)
            return True
        last_vres = vres
        feedback = "; ".join(c.detail for c in vres.checks if not c.ok)
    # Retries exhausted: downgrade to a warning and continue. The step ran;
    # only its acceptance criteria are disputed (often a wrong plan, not
    # wrong work) — the task-level checker remains the backstop.
    step.status = "done"
    step.verify_waived = True
    context.append(f"- Step {step.id}: {step.result[:250]}")
    emit("step_verify_waived", step, last_vres)
    emit("step_done", step)
    return True


def execute_plan(task: str, steps: list[PlanStep], cfg: Config, chat_fn=None,
                 on_event=None) -> PlannedRun:
    """Execute an approved plan, one scoped step at a time. on_event(kind, *args)
    reports ("step_start", step), ("step_sub", step, sub), ("step_verify", step,
    StepVerify), ("step_verify_waived", step, StepVerify), ("step_done", step),
    ("step_failed", step). A waived verification still counts the step done."""
    chat_fn, jail, _registry = _chat_and_tools(cfg, chat_fn)

    def emit(kind, *args):
        if on_event:
            on_event(kind, *args)

    context: list[str] = []
    for step in steps:
        emit("step_start", step)
        if not _run_step_verified(task, step, len(steps), context,
                                  cfg, chat_fn, jail, emit):
            break

    status = "done" if steps and all(s.status == "done" for s in steps) else "step_failed"

    summary = (f"Overall goal: {task}\nPlan execution ended: {status}.\n"
               + "\n".join(f"Step {s.id} [{s.status}]: {s.instruction}"
                            + (f" -> {s.result[:200]}" if s.result else "")
                            for s in steps)
               + '\nSummarize the outcome in 2-4 sentences, starting with "DONE:".')
    try:
        msg = chat_fn([{"role": "user", "content": summary}], None)
        answer = (msg.get("content") or "").strip() or "(no summary)"
    except Exception as e:
        answer = f"(summary call failed: {e})"
    return PlannedRun(status, answer, steps)


def run_planned(task: str, cfg: Config, chat_fn=None, on_event=None) -> PlannedRun:
    """Auto mode: propose a plan and execute it in one go. The eval uses this."""
    text, result = propose(task, cfg, chat_fn)
    if text is None:
        return PlannedRun("plan_failed", f"Could not get a valid plan: {result}", [])
    if on_event:
        on_event("plan", result)
    return execute_plan(task, result, cfg, chat_fn, on_event)
