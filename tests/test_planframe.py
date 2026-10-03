#!/usr/bin/env python3
"""Tests for the plan frame in step prompts (v0.8.1).

The v0.6 executor prompt was scoped to the current step only, on the theory
that a small model cannot sequence. Field failures flipped the evidence:
steps starved for context (a lost file list, an ungrounded write step) hurt
more than freelancing ever did. So the step prompt now carries the shape of
the whole plan: a frame explaining what a multi-step plan is, an outline
with every step's status, done steps annotated with their result summary
and stored output file, and the current step repeated as the operative
block at the end.

Run: python3 tests/test_planframe.py
"""
import re
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness import planner
from harness.config import Config

PLAN3 = """Feature: Frame test
  Scenario: Step 1 - Make the data
    When Write data.txt containing alpha
    Then "data.txt" exists
  Scenario: Step 2 - Use the data
    When Read data.txt and write out.txt from it
    Then "out.txt" exists
  Scenario: Step 3 - Finish up
    When Write final.txt
    Then "final.txt" exists
"""


def make_cfg(**kw):
    d = tempfile.mkdtemp(prefix="planframe-test-")
    base = dict(workspace=d, max_steps=12, step_max_steps=6,
                max_consecutive_errors=3)
    base.update(kw)
    return Config(**base), d


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        raise AssertionError(name)


def run_and_capture():
    """Run PLAN3 with a scripted model; return (result, {step_id: prompt})."""
    cfg, d = make_cfg()
    prompts = {}

    def fake(messages, tool_defs):
        if tool_defs is None and "Break the task" in messages[0]["content"]:
            return {"role": "assistant", "content": PLAN3}
        if tool_defs is None:
            return {"role": "assistant", "content": "DONE: ok."}
        task_msg = messages[1]["content"] if len(messages) > 1 else ""
        sid = next((s for s in (1, 2, 3)
                    if f"You are executing step {s} of 3" in task_msg), None)
        if sid is None:
            return {"role": "assistant", "content": "DONE: step done."}
        if sid not in prompts:
            prompts[sid] = task_msg
        # Act once per step (marker file); after that, declare completion.
        # Dispatch is by the step header, never by instruction text, because
        # the outline legitimately mentions every step's instruction.
        marker = Path(d, f".fake-step{sid}-acted")
        if marker.exists():
            return {"role": "assistant", "content": f"DONE: step {sid} done."}
        marker.write_text("1")
        action = {
            1: ("data.txt", "alpha"),
            2: ("out.txt", "from alpha"),
            3: ("final.txt", "done"),
        }[sid]
        return {"role": "assistant", "content": f"Writing {action[0]}.",
                "tool_calls": [{"function": {"name": "write_file",
                                             "arguments": {"path": action[0],
                                                           "content": action[1]}}}]}

    r = planner.run_planned("frame test goal", cfg, chat_fn=fake)
    return r, prompts, d


def t_frame_explains_the_situation():
    r, prompts, d = run_and_capture()
    try:
        check("run done", r.status == "done", r.status)
        p2 = prompts[2]
        check("frame names the multi-step plan", "multi-step plan" in p2)
        check("frame says done steps are finished", "do not redo" in p2)
        check("frame says later steps read your output",
              "will read your stored output" in p2)
        check("frame grounding rule: read the source step file first",
              "read that step's file first" in p2 and "read_file" in p2)
    finally:
        shutil.rmtree(d)


def t_outline_shows_every_step_with_status():
    r, prompts, d = run_and_capture()
    try:
        p2 = prompts[2]
        check("outline shows step 1 title", "Make the data" in p2)
        check("outline shows step 2 title", "Use the data" in p2)
        check("outline shows pending step 3 title", "Finish up" in p2)
        check("step 1 marked done", "[done]" in p2)
        check("step 3 marked pending", "[pending]" in p2)
        check("current step marked", "YOUR STEP" in p2 or "your step" in p2.lower())
        p3 = prompts[3]
        check("step 3 sees two done steps",
              len(re.findall(r"Step \d+ \[done\]", p3)) == 2,
              str(re.findall(r"Step \d+ \[done\]", p3)))
    finally:
        shutil.rmtree(d)


def t_done_steps_annotated_with_result_and_file():
    r, prompts, d = run_and_capture()
    try:
        p2 = prompts[2]
        check("done step carries its stored output path",
              ".harness/runs/" in p2 and "step-01.md" in p2, p2[:800])
        p3 = prompts[3]
        check("step 3 sees both prior output files",
              "step-01.md" in p3 and "step-02.md" in p3, p3[:800])
        p1 = prompts[1]
        check("step 1 has no done steps",
              re.search(r"Step \d+ \[done\]", p1) is None)
        check("step 1 still gets the frame", "multi-step plan" in p1)
        check("step 1 sees pending steps",
              re.search(r"Step \d+ \[pending\]", p1) is not None)
    finally:
        shutil.rmtree(d)


def t_operative_block_stays_scoped():
    r, prompts, d = run_and_capture()
    try:
        p2 = prompts[2]
        operative = p2.rsplit("\nStep: ", 1)[-1]
        check("operative block is step 2's instruction",
              operative.startswith("Read data.txt and write out.txt"), operative[:120])
        check("operative block carries step 2's done-when",
              "out.txt" in operative and "exists" in operative)
        check("operative block does not carry step 3's work",
              "Write final.txt" not in operative, operative[:200])
        check("do-only-this-step instruction intact",
              "Do ONLY this step" in p2)
    finally:
        shutil.rmtree(d)


def t_planstep_carries_scenario_title():
    cfg, d = make_cfg()
    try:
        def fake(messages, tool_defs):
            return {"role": "assistant", "content": PLAN3}
        text, steps = planner.request_plan("frame test", ["exec"], fake, cfg)
        check("plan parsed", text is not None and len(steps) == 3)
        check("titles carried onto steps",
              steps[0].title == "Step 1 - Make the data"
              and steps[2].title == "Step 3 - Finish up",
              str([s.title for s in steps]))
    finally:
        shutil.rmtree(d)


if __name__ == "__main__":
    for fn in [t_frame_explains_the_situation, t_outline_shows_every_step_with_status,
               t_done_steps_annotated_with_result_and_file,
               t_operative_block_stays_scoped, t_planstep_carries_scenario_title]:
        fn()
    print("\nAll plan frame tests passed.")
