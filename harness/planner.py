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
- When a step writes a document derived from an earlier step's file listing
  (a summary, strategy, report, or index of those files), write its Then as
  `"out.txt" covers the files from step N` (N = the listing step) — the
  harness checks the document actually mentions the source files.
- The Then must accurately describe THIS step's expected outcome. A precisely
  worded wrong expectation fails verification just as surely as a vague one.
- Concrete values come from the task statement only — never <angle-bracket
  placeholders> or phrases like "content from step 2". For values a step must
  COMPUTE, do not invent the result: write "total.txt contains the computed sum",
  never "total.txt contains exactly "12345"". Acceptance criteria state WHAT to
  check, not the implementation's literal output.
- A Given line is optional, for starting-state context a step needs.
- Steps may rely on data produced by earlier steps: the harness stores each
  step's full output (tool results included) in a file and tells the executor
  the exact path, so a later step can read an earlier step's real output.
  Still never invent the values themselves in a Then.
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
    title: str = ""  # Scenario title from the Gherkin (v0.8.1 plan frame)
    status: str = "pending"  # pending | done | failed
    result: str = ""
    verify: list = field(default_factory=list)  # per-Then check records (v0.7)
    lint: list = field(default_factory=list)  # planlint findings (dicts)
    verify_waived: bool = False  # verification failed but step accepted anyway
    output_path: str = ""  # .harness/runs/<run>/step-NN.md (v0.8.1, issue #2)
    source_items: list = field(default_factory=list)  # names this step produced (v0.8.1 covers)
    tool_summary: str = ""  # harness-computed "tools used; output size" line


def to_plan_steps(plan: gherkin.FeaturePlan) -> list[PlanStep]:
    steps = []
    for i, p in enumerate(plan.steps, 1):
        steps.append(PlanStep(
            id=i,
            instruction="\n".join(p.when)[:500],
            done_when="\n".join(p.then)[:300],
            given="\n".join(p.given)[:300],
            title=(p.title or "")[:120],
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
    run_id: str = ""  # .harness/runs/<run_id>/ for this run (v0.8.1)

    def plan_dict(self) -> dict:
        return {"run_id": self.run_id,
                "steps": [
            {"id": s.id, "instruction": s.instruction, "done_when": s.done_when,
             "status": s.status, "result": s.result[:500],
             "verify": s.verify, "lint": s.lint,
             "verify_waived": s.verify_waived,
             "output_path": s.output_path} for s in self.steps]}


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
    "read_file instead of working from the summary."
)


def _plan_outline(steps: list[PlanStep], current_id: int) -> str:
    """The shape of the whole plan, one line per step, with statuses.

    Done steps are annotated with their (short) result and, when the run
    store is active, the exact file holding their full output."""
    import re as _re
    lines = ["The plan:"]
    for s in steps:
        if s.id == current_id:
            mark = "YOUR STEP"
        elif s.status == "done":
            mark = "done"
        else:
            mark = "pending"
        title = _re.sub(r"^Step\s+\d+\s*[-:]\s*", "", (s.title or "").strip())
        label = title or (s.instruction.split("\n")[0][:100] if s.instruction else "")
        lines.append(f"  Step {s.id} [{mark}] {label}")
        if s.id != current_id and s.instruction and title:
            lines.append(f"    When: {s.instruction.split(chr(10))[0][:160]}")
        if s.status == "done" and s.id != current_id:
            body = s.result or ""
            if body.strip().upper().startswith("DONE:"):
                body = body.strip()[5:].strip()
            if body:
                lines.append(f"    Result: {s.result[:200]}")
            elif s.tool_summary:
                # The model answered a bare "DONE:" — the summary channel
                # carried nothing. The harness computes a factual line from
                # its own record instead of leaving a blank.
                lines.append(f"    Result: (no summary given) {s.tool_summary}")
            elif s.result:
                lines.append(f"    Result: {s.result[:200]}")
            if s.output_path:
                lines.append(f"    Full output: {s.output_path}")
    return "\n".join(lines) + "\n"


def _grounding_gate(step: PlanStep, steps: list[PlanStep]):
    """Build the DONE gate for one step (v0.8.1), or None when the step has
    no completed prior steps with stored outputs.

    Field failure behind it: a step wrote its deliverable without ever
    reading the previous step's stored output, and produced a placeholder.
    The frame advises reading the source; the gate checks it. Any tool call
    whose arguments name a prior step's output file counts as grounded
    (read_file, an exec cat, anything). The gate fires once per sub-run;
    a second DONE is the escape hatch for genuinely independent steps."""
    priors = [s.output_path for s in steps
              if s.id < step.id and s.status == "done" and s.output_path]
    if not priors:
        return None

    def gate(sub_steps) -> str | None:
        import json as _json

        def names_prior(s) -> bool:
            return bool(s.tool and s.args and any(
                p in _json.dumps(s.args, default=str) for p in priors))

        read_idx = next((i for i, s in enumerate(sub_steps) if names_prior(s)),
                        None)
        if read_idx is None:
            # Stage 1: never touched the source at all.
            return (f"HARNESS: you are finishing step {step.id} without having read "
                    f"the output of the earlier step(s) this plan builds on: "
                    f"{', '.join(priors)}. Those files are the record of what the "
                    f"earlier steps actually produced. Read the file now with "
                    f"read_file and redo this step using its real contents. If this "
                    f"step genuinely does not depend on that output, answer DONE: "
                    f"again and say why in one sentence.")
        if any(s.tool and not names_prior(s) for s in sub_steps[read_idx + 1:]):
            return None  # real work happened after the read: grounded
        # Stage 2 (the 20261002-192409 field sequence): the source was read,
        # possibly only because stage 1 forced it, and then DONE was
        # re-asserted with nothing changed. Reading is not redoing.
        return (f"HARNESS: you read {', '.join(priors)} but you have not changed "
                f"anything since reading it. Redo this step now using what the "
                f"file contains: write or edit the deliverable from its real "
                f"contents. If nothing genuinely needs to change, answer DONE: "
                f"again and say why in one sentence.")

    return gate


def _run_step_verified(task: str, step: PlanStep, steps: list[PlanStep],
                     cfg: Config, chat_fn, jail, emit,
                     store=None) -> bool:
    """Run one plan step; after the sub-run reports DONE, verify the Then
    clauses in code before advancing. A failed verification warns and retries
    the step once (bounded by cfg.verify_retries) with the exact failure as
    feedback. If verification still fails, the failure is downgraded to a
    warning: the step is accepted and the plan continues, because the verifier
    checks plan *adherence* while the task-level checker judges *correctness* —
    a wrong plan must not block right work. Verification itself costs zero
    model calls. Sub-run failures (model errors, limits) still hard-fail.

    v0.8.1 (issue #2): when a RunStore is present, every attempt's full
    record (tool calls with verbatim results, answer, verification) is
    written to .harness/runs/<run>/step-NN.md.

    v0.8.1 (plan frame): the prompt leads with FRAME_TEXT and the plan
    outline (every step, its status, done steps' output files), then
    repeats the current step as the operative block at the end. The
    outline replaces both the old 250-char context list and the store's
    separate path block: summaries and paths now ride with the step they
    describe."""
    from . import verify as _verify
    vctx = _verify.VerifyContext(
        jail=jail, step_id=step.id, task=task,
        prior_items={s.id: list(s.source_items) for s in steps if s.source_items})
    feedback = ""
    last_vres = None
    attempts: list[dict] = []
    n_steps = len(steps)
    gate = _grounding_gate(step, steps)

    def persist(status: str):
        if attempts:
            counts: dict[str, int] = {}
            n_lines = 0
            for s in attempts[-1].get("sub_steps") or []:
                if s.tool:
                    counts[s.tool] = counts.get(s.tool, 0) + 1
                    n_lines += len((s.result or "").splitlines())
            if counts:
                step.tool_summary = (
                    ", ".join(f"{t} x{n}" for t, n in counts.items())
                    + f"; {n_lines} lines of tool output")
        if store is None:
            return
        try:
            store.write_step(
                step_id=step.id, instruction=step.instruction,
                given=step.given, done_when=step.done_when,
                answer=step.result, sub_steps=[], status=status,
                verify_checks=step.verify, verify_waived=step.verify_waived,
                attempts=attempts or None)
        except Exception:
            pass  # the store is a record, never a reason to fail real work

    for _ in range(1 + cfg.verify_retries):
        prompt = (f"Overall goal: {task}\n"
                  f"You are executing step {step.id} of {n_steps}. "
                  f"Do ONLY this step, nothing else.\n\n")
        prompt += FRAME_TEXT + "\n\n"
        prompt += _plan_outline(steps, step.id) + "\n"
        if step.given:
            prompt += f"Starting state: {step.given}\n"
        prompt += (f"Step: {step.instruction}\n"
                   f"This step is done when: {step.done_when or 'its instruction is complete'}\n")
        if store is not None and step.output_path:
            prompt += (f"Your full output will be stored at {step.output_path} "
                       f"for later steps.\n")
        if feedback:
            prompt += (f"Your previous attempt failed verification: {feedback} "
                       f"Fix exactly this and try again.\n")
        prompt += 'When this step is complete, answer starting with "DONE:".'

        sub_cfg = replace(cfg, max_steps=cfg.step_max_steps)
        try:
            r = loop.run(prompt, sub_cfg, chat_fn=chat_fn,
                         on_step=lambda s: emit("step_sub", step, s),
                         done_gate=gate)
        except Exception as e:
            step.status = "failed"
            step.result = f"harness error: {e}"
            attempts.append({"sub_steps": [], "answer": step.result,
                             "verify_checks": []})
            persist("failed")
            emit("step_failed", step)
            return False
        step.result = (r.answer or "")[:800]
        if r.status != "done":
            step.status = "failed"
            attempts.append({"sub_steps": list(r.steps), "answer": r.answer or "",
                             "verify_checks": []})
            persist("failed")
            emit("step_failed", step)
            return False
        step.source_items = _verify.collect_step_items(list(r.steps))
        thens = [t.strip() for t in step.done_when.split("\n") if t.strip()]
        vres = _verify.verify_step(thens, vctx)
        step.verify = [{"then": c.then, "verifier": c.verifier,
                        "ok": c.ok, "detail": c.detail} for c in vres.checks]
        attempts.append({"sub_steps": list(r.steps), "answer": r.answer or "",
                         "verify_checks": list(step.verify)})
        emit("step_verify", step, vres)
        if vres.ok:
            step.status = "done"
            persist("done")
            emit("step_done", step)
            return True
        last_vres = vres
        feedback = "; ".join(c.detail for c in vres.checks if not c.ok)
    # Retries exhausted: downgrade to a warning and continue. The step ran;
    # only its acceptance criteria are disputed (often a wrong plan, not
    # wrong work) — the task-level checker remains the backstop.
    step.status = "done"
    step.verify_waived = True
    persist("done")
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

    store = None
    try:
        from .runstore import RunStore
        store = RunStore(cfg.workspace)
        for s in steps:
            s.output_path = store.rel_path(s.id)
    except Exception:
        store = None  # record-keeping must never block execution

    for step in steps:
        emit("step_start", step)
        if not _run_step_verified(task, step, steps,
                                  cfg, chat_fn, jail, emit, store=store):
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
    return PlannedRun(status, answer, steps,
                      run_id=store.run_id if store is not None else "")


def run_planned(task: str, cfg: Config, chat_fn=None, on_event=None) -> PlannedRun:
    """Auto mode: propose a plan and execute it in one go. The eval uses this."""
    text, result = propose(task, cfg, chat_fn)
    if text is None:
        return PlannedRun("plan_failed", f"Could not get a valid plan: {result}", [])
    if on_event:
        on_event("plan", result)
    return execute_plan(task, result, cfg, chat_fn, on_event)
