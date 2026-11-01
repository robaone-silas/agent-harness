"""Verified execution of one plan step (issue #62).

Split out of planner.py: the per-step engine. A _StepRun carries one
step through the indexing primitive, its executor sub-runs, the
world-state override, verification and its retries, and the
warn-downgrade, recording every attempt. planner.py re-exports
_run_step_verified and _index_describe, so existing imports keep
working.
"""
from __future__ import annotations

import json as _json
from dataclasses import replace

from . import indexing as _indexing
from . import loop
from . import verify as _verify
from .config import Config
from .grounding import _grounding_gate, _then_named_outputs
from .plan_model import PlanStep, step_budget
from .run_metrics import (_is_content_bearing_pass, _record_attempt_metrics,
                          _write_step_to_store)
from .step_prompt import _graph_contract_block, _refusal_hint, _step_prompt


def _index_describe(chat_fn, name: str, content: str) -> str:
    """One per-file description call for the indexing primitive.

    Returns the model's raw reply, or "" on any failure (the indexing
    build treats an empty description as a decline, and the step
    falls back to its normal executor sub-run)."""
    messages = [{"role": "user", "content": _indexing.describe_prompt(name, content)}]
    try:
        response_message = chat_fn(messages, None)
    except Exception:
        return ""
    if not isinstance(response_message, dict):
        return ""
    return (response_message.get("content") or "")


def _check_records(verification_result) -> list[dict]:
    return [{"then": check.then, "verifier": check.verifier,
             "ok": check.ok, "detail": check.detail}
            for check in verification_result.checks]


def _failure_feedback(verification_result) -> str:
    return "; ".join(check.detail for check in verification_result.checks if not check.ok)


def _judgeable_on_world_state(run_result) -> bool:
    """A sub-run that ended at the turn limit with a real final
    answer. A dead run gets no override: consecutive tool errors
    (error_limit), a model error, or a final summary call that itself
    failed leaves nothing finished to judge."""
    answer = (run_result.answer or "").strip()
    if run_result.status != "max_steps" or not answer:
        return False
    return not answer.startswith(("(summary call failed", "(no summary)"))


class _StepRun:
    """One plan step's verified execution and its attempt record."""

    def __init__(self, task: str, step: PlanStep, steps: list[PlanStep],
                 config: Config, chat_fn, jail, emit_event,
                 run_store=None, only_tools=None):
        self.task = task
        self.step = step
        self.steps = steps
        self.config = config
        self.chat_fn = chat_fn
        self.jail = jail
        self.emit_event = emit_event
        self.run_store = run_store
        self.only_tools = only_tools
        self.verify_context = _verify.VerifyContext(
            jail=jail, step_id=step.id, task=task,
            prior_items={prior_step.id: list(prior_step.source_items)
                         for prior_step in steps if prior_step.source_items},
            intent=step.intent or "",
            excluded_files=_verify._excluded_files_for(steps, step.id))
        self.then_clauses = [clause.strip() for clause in step.done_when.split("\n") if clause.strip()]
        self.attempts: list[dict] = []
        self.feedback_message = ""

    def verify_now(self) -> bool:
        """True only when the step's Thens machine-check as passing right
        now AND at least one passing check is content-bearing.

        Attested clauses are not evidence, and neither is a bare
        existence pass: the stub that founded stage 2 satisfied
        `"file" exists` while promising its content "to be filled in".
        The checks that answer stage 2's suspicion measure content:
        contains, contains_exactly, indexes (against the source items),
        has_lines. See _grounding_gate."""
        if not self.then_clauses:
            return False
        verification_result = _verify.verify_step(self.then_clauses, self.verify_context)
        return _is_content_bearing_pass(verification_result)

    def persist(self, status: str) -> None:
        _record_attempt_metrics(self.step, self.attempts)
        _write_step_to_store(self.run_store, self.step, self.attempts, status)

    def verify(self):
        verification_result = _verify.verify_step(self.then_clauses, self.verify_context)
        self.step.verify = _check_records(verification_result)
        return verification_result

    def record_attempt(self, sub_steps: list, answer: str, verify_checks: list) -> None:
        self.attempts.append({"sub_steps": sub_steps, "answer": answer, "verify_checks": verify_checks})

    def accept(self, *extra_events) -> bool:
        self.step.status = "done"
        self.persist("done")
        for event in extra_events:
            self.emit_event(*event)
        self.emit_event("step_done", self.step)
        return True

    def fail(self) -> bool:
        self.step.status = "failed"
        self.persist("failed")
        self.emit_event("step_failed", self.step)
        return False

    def run(self) -> bool:
        gate = _grounding_gate(self.step, self.steps, verify_now=self.verify_now)
        # Experimental: the step's plan-graph node contract rides in its
        # prompt when enabled. Computed once here; the graph derives from
        # plan text, so the verification retry sees the same contract.
        contract = ""
        if getattr(self.config, "graph_contracts", False):
            contract = _graph_contract_block(self.task, self.step, self.steps)
        # Issue #14: this step's turn budget is computed from its planned
        # operations now that prior steps' source items are known, and the
        # same budget governs the verification retry.
        self.step.budget = step_budget(self.step, self.steps, self.config)
        sub_run_config = replace(self.config, max_steps=self.step.budget)
        if self.try_indexing_primitive():
            return True
        # Issue #48: files the step's Thens name that its When does not.
        # The prompt presents them as the step's output file, and a
        # refused write's feedback names them.
        output_files = _then_named_outputs(self.step)
        refusal_hint = _refusal_hint(output_files)
        last_verification_result = None
        for _ in range(1 + self.config.verify_retries):
            prompt = self.prompt(output_files, contract)
            outcome, last_verification_result = self.attempt(prompt, sub_run_config, gate, refusal_hint)
            if outcome is not None:
                return outcome
        return self.waive(last_verification_result)

    def prompt(self, output_files: list[str], contract: str) -> str:
        store_active = self.run_store is not None
        return _step_prompt(self.task, self.step, self.steps, output_files, contract, store_active, self.feedback_message)

    def try_indexing_primitive(self) -> bool:
        """Indexing primitive: when the step carries an `indexes` clause,
        the harness tries to build the target itself (enumerate the
        source step's items, one description call per file, write the
        index) before any executor sub-run. Success skips the sub-run
        entirely; a declined build (unreadable file, empty description)
        or a failed verification of the step's other Thens falls
        through to the normal loop, with any verification failure as
        its feedback."""
        clause = _indexing.clause_for(self.step)
        if clause is None:
            return False
        target_path, source_step_id = clause
        source_items = self.source_items_of(source_step_id)
        records = self.build_index(source_items) if source_items else None
        if records is None:
            return False
        self.write_index(target_path, records, source_items)
        verification_result = self.verify()
        self.record_attempt([], self.step.result, list(self.step.verify))
        self.emit_event("step_verify", self.step, verification_result)
        if verification_result.ok:
            return self.accept()
        self.feedback_message = _failure_feedback(verification_result)
        return False

    def source_items_of(self, source_step_id: int) -> list[str]:
        source_step = next((candidate for candidate in self.steps if candidate.id == source_step_id), None)
        return list(source_step.source_items) if source_step is not None else []

    def build_index(self, source_items: list[str]):
        def describe(name, content):
            return _index_describe(self.chat_fn, name, content)
        return _indexing.build_index(self.jail.root, source_items, describe)

    def write_index(self, target_path: str, records, source_items: list[str]) -> None:
        destination_path = self.jail.resolve(target_path)
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        destination_path.write_text(_indexing.render_index(records))
        self.step.result = f"DONE: indexed {len(records)} files into {target_path} with the harness indexing primitive."
        self.step.source_items = list(source_items)
        touched_paths = list(source_items) + [target_path]
        self.step.touched_args |= {_json.dumps({"path": name}) for name in touched_paths}

    def attempt(self, prompt: str, sub_run_config: Config, gate, refusal_hint):
        """One executor sub-run and its verification.

        Returns (True/False, result) when the step is settled, or
        (None, failed verification) when it should retry."""
        def on_step(sub_step):
            self.emit_event("step_sub", self.step, sub_step)

        sub_run_options = {
            "chat_fn": self.chat_fn, "on_step": on_step, "done_gate": gate,
            "only_tools": self.only_tools, "refusal_hint": refusal_hint,
        }
        try:
            run_result = loop.run(prompt, sub_run_config, **sub_run_options)
        except Exception as error:
            self.step.result = f"harness error: {error}"
            self.record_attempt([], self.step.result, [])
            return self.fail(), None
        self.step.result = (run_result.answer or "")[:800]
        if run_result.status != "done":
            return self.settle_without_done(run_result), None
        self.step.source_items = _verify.collect_step_items(list(run_result.steps))
        verification_result = self.verify()
        self.record_attempt(list(run_result.steps), run_result.answer or "", list(self.step.verify))
        self.emit_event("step_verify", self.step, verification_result)
        if verification_result.ok:
            return self.accept(), verification_result
        self.feedback_message = _failure_feedback(verification_result)
        return None, verification_result

    def settle_without_done(self, run_result) -> bool:
        """World-state override (Ansel's ruling, 2026-10-04, the Tomas
        protocol question): a sub-run that ended at the turn limit with
        a real final answer missed the DONE protocol, but the protocol
        token adds no information about the world. Judge the step on
        the world instead: run verification exactly as after DONE, and
        if every clause passes, the step is done, recorded as such.
        Attested clauses count when they pass under the current rules
        (existence floor included): a DONE ending with the same
        verification is already accepted, so the same verification
        after a protocol miss stands identically."""
        verification_result = None
        if _judgeable_on_world_state(run_result):
            self.step.source_items = _verify.collect_step_items(list(run_result.steps))
            verification_result = self.verify()
            self.emit_event("step_verify", self.step, verification_result)
        verify_checks = list(self.step.verify) if verification_result is not None else []
        self.record_attempt(list(run_result.steps), run_result.answer or "", verify_checks)
        if verification_result is None or not verification_result.ok:
            return self.fail()
        self.step.done_via_world_state = True
        return self.accept(("step_done_world_state", self.step, verification_result))

    def waive(self, last_verification_result) -> bool:
        """Retries exhausted: downgrade to a warning and continue. The
        step ran; only its acceptance criteria are disputed (often a
        wrong plan, not wrong work) — the task-level checker remains
        the backstop."""
        self.step.verify_waived = True
        return self.accept(("step_verify_waived", self.step, last_verification_result))


def _run_step_verified(task: str, step: PlanStep, steps: list[PlanStep],
                       config: Config, chat_fn, jail, emit_event,
                       run_store=None, only_tools=None) -> bool:
    """Run one plan step; after the sub-run reports DONE, verify the Then
    clauses in code before advancing. A failed verification warns and retries
    the step once (bounded by config.verify_retries) with the exact failure as
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
    step_run = _StepRun(task, step, steps, config, chat_fn, jail, emit_event,
                        run_store=run_store, only_tools=only_tools)
    return step_run.run()
