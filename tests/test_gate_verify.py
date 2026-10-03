#!/usr/bin/env python3
"""Tests for verify-before-redo: the gate's surgical fix (v0.8.1).

Field failure behind it (intent run, 2026-10-03): step 2 wrote a good
index.md on its first attempt. The grounding gate's stage 1 made it
read step 1's record; stage 2 ("you read it but changed nothing")
then ordered a redo, and the redo briefly overwrote the good index
with a worse one copied from step 1's draft. The final artifact was
saved only because a third write happened to restore the good
format. Stage 2 fires blind: it demands rework before verification
has ever looked at the deliverable it suspects.

The fix: at the moment stage 2 would fire, run the step's own Thens
against the workspace (verification is deterministic, zero model
calls). If every Then is machine-checked and passing, the suspicion
is answered and the gate stays silent. Where a Then attests (no
machine evidence) or a check fails, the pushback proceeds exactly as
before: on trust, the gate's skepticism is the only skepticism there
is, and a failing deliverable deserves the redo.

Run: python3 tests/test_gate_verify.py
"""
import re
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness import loop, planner
from harness.config import Config

STAGE2_MARK = "have not changed anything"
GOOD = "real summary of alpha.txt"

PLAN_CHECKED = """Feature: Gate verify test
  Scenario: Step 1 - List the files
    When List the files in the workspace
    Then the list is recorded
  Scenario: Step 2 - Write the summary
    When Write summary.txt based on the file list
    Then "summary.txt" contains exactly "real summary of alpha.txt"
"""

PLAN_ATTESTED = """Feature: Gate verify test (attested)
  Scenario: Step 1 - List the files
    When List the files in the workspace
    Then the list is recorded
  Scenario: Step 2 - Write the summary
    When Write summary.txt based on the file list
    Then the summary is written
"""

PLAN_EXISTS_ONLY = """Feature: Gate verify test (exists only)
  Scenario: Step 1 - List the files
    When List the files in the workspace
    Then the list is recorded
  Scenario: Step 2 - Write the summary
    When Write summary.txt based on the file list
    Then "summary.txt" exists
"""


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        raise AssertionError(name)


# ------------------------------------------------------------ unit level

def _gate(verify_now):
    done1 = planner.PlanStep(id=1, instruction="List files",
                             done_when="the list is recorded", status="done",
                             output_path=".harness/runs/X/step-01.md")
    cur = planner.PlanStep(id=2, instruction="Write summary.txt",
                           done_when='"summary.txt" exists',
                           output_path=".harness/runs/X/step-02.md")
    return planner._grounding_gate(cur, [done1, cur], verify_now=verify_now)


def _read_step():
    return loop.Step(n=1, thought="", tool="read_file",
                     args={"path": ".harness/runs/X/step-01.md"},
                     result="contents", path="native")


def t_unit_stage2_suppressed_only_on_full_machine_pass():
    check("stage 1 unaffected by verify_now",
          _gate(lambda: True)([]) is not None)
    check("stage 2 suppressed when verify_now passes",
          _gate(lambda: True)([_read_step()]) is None)
    check("stage 2 fires when verify_now fails",
          _gate(lambda: False)([_read_step()]) is not None)
    check("stage 2 fires when no verify_now supplied",
          _gate(None)([_read_step()]) is not None)
    check("a verify_now exception never suppresses",
          _gate(lambda: 1 / 0)([_read_step()]) is not None)


# ------------------------------------------------------------ plan level

class Actor:
    """Step 1 lists. Step 2 follows a fixed script of (kind, payload):
    the field sequence — write, DONE, read the record, DONE — and, if
    stage 2 still pushes, a final write of `after_push` and DONE."""

    def __init__(self, plan_text, first_write, after_push=None):
        self.plan_text = plan_text
        self.first_write = first_write
        self.after_push = after_push
        self.writes = []
        self.stage2_seen = False
        self._task_seen = None
        self._phase = 0

    def __call__(self, messages, tool_defs):
        if tool_defs is None and "Break the task" in messages[0]["content"]:
            return {"role": "assistant", "content": self.plan_text}
        if tool_defs is None:
            return {"role": "assistant", "content": "DONE: ok."}
        text = "\n".join(m.get("content") or "" for m in messages)
        if STAGE2_MARK in text:
            self.stage2_seen = True
        task_msg = messages[1]["content"] if len(messages) > 1 else ""
        if task_msg != self._task_seen:
            self._task_seen = task_msg
            self._phase = 0
        phase = self._phase
        self._phase += 1
        if "You are executing step 1" in task_msg:
            if phase == 0:
                return {"role": "assistant", "content": "Listing.",
                        "tool_calls": [{"function": {"name": "list_dir",
                                                     "arguments": {"path": "."}}}]}
            return {"role": "assistant", "content": "DONE: listed."}
        m = re.search(r"\.harness/runs/\S+/step-01\.md", task_msg)
        script = [
            ("write", self.first_write),
            ("done", "DONE: written."),
            ("read", m.group(0) if m else ""),
            ("done", "DONE: read it."),
        ]
        if self.after_push is not None:
            script += [("write", self.after_push),
                       ("done", "DONE: redone.")]
        kind, payload = script[min(phase, len(script) - 1)]
        if kind == "write":
            self.writes.append(payload)
            return {"role": "assistant", "content": "Writing.",
                    "tool_calls": [{"function": {"name": "write_file",
                                                 "arguments": {"path": "summary.txt",
                                                               "content": payload}}}]}
        if kind == "read":
            return {"role": "assistant", "content": "Reading.",
                    "tool_calls": [{"function": {"name": "read_file",
                                                 "arguments": {"path": payload}}}]}
        return {"role": "assistant", "content": payload}


def run_plan(plan_text, first_write, after_push=None):
    d = tempfile.mkdtemp(prefix="gate-verify-")
    Path(d, "alpha.txt").write_text("alpha body")
    cfg = Config(workspace=d, max_steps=12, step_max_steps=8,
                 max_consecutive_errors=3)
    actor = Actor(plan_text, first_write, after_push)
    r = planner.run_planned("summarize the files", cfg, chat_fn=actor)
    return r, actor, d


def t_good_deliverable_is_not_ordered_redone():
    r, actor, d = run_plan(PLAN_CHECKED, GOOD)
    try:
        check("run done", r.status == "done", r.status)
        check("stage 2 never fired", not actor.stage2_seen)
        check("exactly one write happened", len(actor.writes) == 1,
              str(actor.writes))
        check("the good version stands",
              Path(d, "summary.txt").read_text() == GOOD)
    finally:
        shutil.rmtree(d)


def t_attested_then_keeps_the_pushback():
    r, actor, d = run_plan(PLAN_ATTESTED, GOOD, after_push="rewritten summary")
    try:
        check("run done", r.status == "done", r.status)
        check("stage 2 fired on trust as before", actor.stage2_seen)
        check("the redo happened", len(actor.writes) == 2, str(actor.writes))
        check("the rewritten version stands",
              Path(d, "summary.txt").read_text() == "rewritten summary")
    finally:
        shutil.rmtree(d)


def t_exists_alone_does_not_suppress():
    # Stage 2's founding field sequence (run 20261002-192409): the
    # deliverable was a stub that satisfied "exists" while promising
    # its content later. A bare existence pass must never answer
    # stage 2's suspicion; only content-bearing checks can.
    r, actor, d = run_plan(PLAN_EXISTS_ONLY, "stub", after_push=GOOD)
    try:
        check("run done", r.status == "done", r.status)
        check("stage 2 fired despite the exists pass", actor.stage2_seen)
        check("the redo replaced the stub",
              Path(d, "summary.txt").read_text() == GOOD)
    finally:
        shutil.rmtree(d)


def t_failing_check_keeps_the_pushback():
    r, actor, d = run_plan(PLAN_CHECKED, "stub", after_push=GOOD)
    try:
        check("run done", r.status == "done", r.status)
        check("stage 2 fired when the check failed", actor.stage2_seen)
        check("the redo fixed the file",
              Path(d, "summary.txt").read_text() == GOOD)
    finally:
        shutil.rmtree(d)


if __name__ == "__main__":
    for fn in [t_unit_stage2_suppressed_only_on_full_machine_pass,
               t_good_deliverable_is_not_ordered_redone,
               t_attested_then_keeps_the_pushback,
               t_exists_alone_does_not_suppress,
               t_failing_check_keeps_the_pushback]:
        fn()
    print("\nAll gate verify-before-redo tests passed.")
