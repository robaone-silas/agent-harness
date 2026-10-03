#!/usr/bin/env python3
"""Tests for pushback budget accounting (v0.8.1).

Field failures behind it (three live deaths with one signature): a
plan step did its work, then the harness spent the step's remaining
turns on its own process and the step failed for running out of
budget. Organize run 20261003-060010 step 3 (gate pushback, a failed
compliance read, the DONE-after-error pushback, the repeat breaker);
index run 1 step 2 and index run 3 step 3 (both gate pushbacks plus
compliance reads; in run 3 the step's append had landed and the step
still failed, unverified, two turns after its work was done).

The budget exists to bound the MODEL's work. Pushbacks are the
harness exercising executive function, and the harness is capped
already: the gate fires at most twice, the error pushback once.
Accounting rule: a fired pushback refunds its turn (the step's
effective limit grows by the pushbacks used), so supervision is not
charged to the supervised. Everything else still counts: model-chosen
actions, redundant reads, and the narration nudge (uncapped, so
refunding it would let a chattering model loop forever).

Run: python3 tests/test_budget.py
"""
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness import loop, planner
from harness.config import Config


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        raise AssertionError(name)


def make_cfg(d, **kw):
    base = dict(workspace=d, max_steps=12, step_max_steps=6,
                max_consecutive_errors=3)
    base.update(kw)
    return Config(**base)


def scripted(responses):
    it = iter(responses)

    def fake(messages, tool_defs):
        if tool_defs is None:
            return {"role": "assistant", "content": "SUMMARY: end."}
        try:
            return next(it)
        except StopIteration:
            return {"role": "assistant", "content": "SUMMARY: stopped."}

    return fake


def tool(name, args):
    return {"role": "assistant", "content": "Working.",
            "tool_calls": [{"function": {"name": name, "arguments": args}}]}


def grounding_gate(steps):
    read_idx = next((i for i, s in enumerate(steps) if s.tool == "read_file"),
                    None)
    if read_idx is None:
        return "HARNESS: read the source first."
    if any(s.tool and s.tool != "read_file" for s in steps[read_idx + 1:]):
        return None
    return "HARNESS: you read it but changed nothing."


def t_gate_pushbacks_are_refunded():
    d = tempfile.mkdtemp(prefix="budget-test-")
    try:
        cfg = make_cfg(d, max_steps=5)
        fake = scripted([
            tool("exec", {"command": "true"}),              # work
            {"role": "assistant", "content": "DONE: done."},  # gate fire 1
            tool("read_file", {"path": "x.txt"}),           # compliance read
            {"role": "assistant", "content": "DONE: done."},  # gate fire 2
            tool("exec", {"command": "true"}),              # work after read
            {"role": "assistant", "content": "DONE: for real."},
        ])
        Path(d, "x.txt").write_text("x")
        r = loop.run("do it", cfg, chat_fn=fake, done_gate=grounding_gate)
        check("gated run completes inside a 5-turn work budget",
              r.status == "done", r.status)
        check("final answer is the accepted DONE",
              r.answer == "DONE: for real.", r.answer)
        check("six model turns happened (5 budget + refunds)",
              len(r.steps) == 6, str(len(r.steps)))
    finally:
        shutil.rmtree(d)


def t_error_pushback_is_refunded():
    d = tempfile.mkdtemp(prefix="budget-test-")
    try:
        cfg = make_cfg(d, max_steps=3)
        fake = scripted([
            tool("read_file", {"path": "missing.txt"}),     # errors
            {"role": "assistant", "content": "DONE: done."},  # error pushback
            tool("exec", {"command": "true"}),              # engages, succeeds
            {"role": "assistant", "content": "DONE: really."},
        ])
        r = loop.run("do it", cfg, chat_fn=fake)
        check("run completes inside a 3-turn work budget",
              r.status == "done", r.status)
        check("final answer is the post-error DONE",
              r.answer == "DONE: really.", r.answer)
    finally:
        shutil.rmtree(d)


def t_refunds_stay_bounded():
    # A gate that always pushes still cannot imprison the run: the gate
    # cap (2) bounds the refunds, and the third DONE is accepted.
    d = tempfile.mkdtemp(prefix="budget-test-")
    try:
        cfg = make_cfg(d, max_steps=2)
        fake = scripted([
            {"role": "assistant", "content": "DONE: one."},
            {"role": "assistant", "content": "DONE: two."},
            {"role": "assistant", "content": "DONE: three, accepted."},
        ])
        r = loop.run("do it", cfg, chat_fn=fake,
                     done_gate=lambda steps: "HARNESS: ALWAYS PUSH")
        check("run completes", r.status == "done", r.status)
        check("total turns bounded by budget + 2",
              len(r.steps) <= 4, str(len(r.steps)))
    finally:
        shutil.rmtree(d)


def t_narration_nudges_are_not_refunded():
    # The narration nudge has no cap; refunding it would let a model
    # that only chats loop forever. Chatter spends budget like work.
    d = tempfile.mkdtemp(prefix="budget-test-")
    try:
        cfg = make_cfg(d, max_steps=3)

        def fake(messages, tool_defs):
            if tool_defs is None:
                return {"role": "assistant", "content": "SUMMARY: end."}
            return {"role": "assistant", "content": "Let me think about this."}

        r = loop.run("do it", cfg, chat_fn=fake)
        check("chattering run hits the step limit",
              r.status == "max_steps", r.status)
        check("exactly the budgeted turns were used",
              len(r.steps) == 3, str(len(r.steps)))
    finally:
        shutil.rmtree(d)


def t_plain_work_gets_no_bonus():
    d = tempfile.mkdtemp(prefix="budget-test-")
    try:
        cfg = make_cfg(d, max_steps=3)
        counter = {"n": 0}

        def fake(messages, tool_defs):
            if tool_defs is None:
                return {"role": "assistant", "content": "SUMMARY: end."}
            counter["n"] += 1
            return tool("exec", {"command": f"true {counter['n']}"})

        r = loop.run("do it", cfg, chat_fn=fake)
        check("endless work still stops at the limit",
              r.status == "max_steps", r.status)
        check("exactly the budgeted turns were used",
              len(r.steps) == 3, str(len(r.steps)))
    finally:
        shutil.rmtree(d)


PLAN2 = """Feature: Budget test
  Scenario: Step 1 - List the files
    When List the files in the workspace
    Then the list is recorded
  Scenario: Step 2 - Write the summary
    When Write summary.txt based on the file list
    Then "summary.txt" exists
"""


def t_plan_step_survives_gate_with_tight_budget():
    # The index-run death, replayed at plan level: step 2 writes, takes
    # both gate stages, rewrites, and finishes — six model turns
    # against a step budget of 4. It survives only if the two gate
    # pushbacks are refunded.
    d = tempfile.mkdtemp(prefix="budget-plan-")
    try:
        Path(d, "alpha.txt").write_text("a")
        state = {"task_seen": None, "phase": 0}
        import re

        def fake(messages, tool_defs):
            if tool_defs is None and "Break the task" in messages[0]["content"]:
                return {"role": "assistant", "content": PLAN2}
            if tool_defs is None:
                return {"role": "assistant", "content": "DONE: ok."}
            task_msg = messages[1]["content"] if len(messages) > 1 else ""
            if task_msg != state["task_seen"]:
                state["task_seen"] = task_msg
                state["phase"] = 0
            phase = state["phase"]
            state["phase"] += 1
            if "You are executing step 1" in task_msg:
                if phase == 0:
                    return tool("list_dir", {"path": "."})
                return {"role": "assistant", "content": "DONE: listed."}
            m = re.search(r"\.harness/runs/\S+/step-01\.md", task_msg)
            script = [
                tool("write_file", {"path": "summary.txt", "content": "stub"}),
                {"role": "assistant", "content": "DONE: written."},
                tool("read_file", {"path": m.group(0)}),
                {"role": "assistant", "content": "DONE: read it."},
                tool("write_file", {"path": "summary.txt",
                                    "content": "real summary of alpha.txt"}),
                {"role": "assistant", "content": "DONE: redone."},
            ]
            return script[min(phase, len(script) - 1)]

        cfg = make_cfg(d, step_max_steps=4)
        r = planner.run_planned("summarize the files", cfg, chat_fn=fake)
        check("plan completes on a 4-turn step budget",
              r.status == "done", r.status)
        check("step 2 done", r.steps[1].status == "done", r.steps[1].status)
        check("final file is the rewrite",
              Path(d, "summary.txt").read_text() == "real summary of alpha.txt")
    finally:
        shutil.rmtree(d)


if __name__ == "__main__":
    for fn in [t_gate_pushbacks_are_refunded, t_error_pushback_is_refunded,
               t_refunds_stay_bounded, t_narration_nudges_are_not_refunded,
               t_plain_work_gets_no_bonus,
               t_plan_step_survives_gate_with_tight_budget]:
        fn()
    print("\nAll budget accounting tests passed.")
