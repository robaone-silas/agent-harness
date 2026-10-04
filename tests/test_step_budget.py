#!/usr/bin/env python3
"""Tests for dynamic per-step turn budgets (issue #14).

Field failure, 2026-10-04 live replay: both persona plans were approved
after the issues #5 and #6 fixes, and both executions then died at step 2.
The per-step cap was a flat 6 turns regardless of the work the step's own
plan implied. Priya's compile step needed 7 reads (the step 1 record plus
six source files), 1 write, and the final answer: 9 turns of planned work
judged against a cap of 6. Deterministic arithmetic, not a model failure:
a step enumerating N reads needs at least N+1 turns, and the budget never
looked at the plan. The budget is now computed at step start from the
planned operations: distinct file operands the step names, plus, for each
earlier step it draws on, one read of that step's stored record and one
turn per source item that step produced, plus slack for the final answer,
floored at the configured base and capped at a hard ceiling.

Run: python3 tests/test_step_budget.py
"""
import re
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness import gherkin, planner
from harness.config import Config

FILES6 = ["alpha-policy.txt", "beta-invoice.csv", "delta-meeting.txt",
          "epsilon-reading.md", "gamma-recipe.txt", "zeta-travel.txt"]

PRIYA_PLAN = """Feature: Create file audit index
  Scenario: Step 1 - List all files
    Intent: know exactly which files must be included in the audit index
    When I list the files in the workspace
    Then the output lists all files in the workspace
  Scenario: Step 2 - Create the audit index
    Intent: create audit-index.md listing every file with its exact filename and one-line description
    When I read each file listed in step 1 and write audit-index.md with each filename and a one-line description
    Then "audit-index.md" covers the files from step 1
"""

GATHER_PLAN = """Feature: Gather the sources
  Scenario: Step 1 - Read every source
    When I read "alpha-policy.txt" and "beta-invoice.csv" and "delta-meeting.txt" and "epsilon-reading.md" and "gamma-recipe.txt" and "zeta-travel.txt"
    Then the output summarizes every source
"""


def make_cfg(d, **kw):
    base = dict(workspace=d, max_steps=12, step_max_steps=6,
                max_consecutive_errors=3)
    base.update(kw)
    return Config(**base)


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        raise AssertionError(name)


def parse(text):
    plan, err = gherkin.parse_feature(text)
    assert plan is not None, err
    return planner.to_plan_steps(plan)


def t_simple_step_keeps_base_budget():
    steps = parse("""Feature: One write
  Scenario: Step 1 - Write the file
    When Write data.txt containing alpha
    Then "data.txt" exists
""")
    cfg = make_cfg("unused")
    check("simple step budget is the base",
          planner.step_budget(steps[0], steps, cfg) == 6,
          str(planner.step_budget(steps[0], steps, cfg)))


def t_covers_step_scales_with_source_items():
    steps = parse(PRIYA_PLAN)
    steps[0].source_items = list(FILES6)
    cfg = make_cfg("unused")
    b = planner.step_budget(steps[1], steps, cfg)
    # 1 named operand (audit-index.md) + 1 record read + 6 item reads
    # + slack for the write and the final answer = 10 turns of room.
    check("covers step budget covers its planned reads", b >= 9, str(b))
    check("covers step budget is the counted value", b == 10, str(b))


def t_enumerated_reads_scale_the_budget():
    steps = parse(GATHER_PLAN)
    cfg = make_cfg("unused")
    b = planner.step_budget(steps[0], steps, cfg)
    check("six enumerated reads get more than the flat cap", b > 6, str(b))
    check("six enumerated reads budget is counted", b == 8, str(b))


def t_ceiling_caps_the_budget():
    many = " and ".join(f'"file-{i:02d}.txt"' for i in range(40))
    steps = parse(f"""Feature: Read everything
  Scenario: Step 1 - Read all
    When I read {many}
    Then the output summarizes every file
""")
    cfg = make_cfg("unused")
    check("budget never exceeds the ceiling",
          planner.step_budget(steps[0], steps, cfg) == 24,
          str(planner.step_budget(steps[0], steps, cfg)))
    cfg8 = make_cfg("unused", step_budget_ceiling=8)
    check("a configured ceiling is honored",
          planner.step_budget(steps[0], steps, cfg8) == 8,
          str(planner.step_budget(steps[0], steps, cfg8)))
    cfg4 = make_cfg("unused", step_max_steps=4)
    simple = parse("""Feature: One write
  Scenario: Step 1 - Write the file
    When Write data.txt containing alpha
    Then "data.txt" exists
""")
    check("a configured base is the floor",
          planner.step_budget(simple[0], simple, cfg4) == 4,
          str(planner.step_budget(simple[0], simple, cfg4)))


def t_replay_priya_shape_completes():
    d = tempfile.mkdtemp(prefix="step-budget-")
    try:
        for name in FILES6:
            Path(d, name).write_text(f"contents of {name}")
        state = {"task_seen": None, "phase": 0}

        def tool(name, args):
            return {"role": "assistant", "content": "",
                    "tool_calls": [{"function": {"name": name, "arguments": args}}]}

        def fake(messages, tool_defs):
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
            script = [tool("read_file", {"path": m.group(0)})]
            script += [tool("read_file", {"path": n}) for n in FILES6]
            script.append(tool("write_file", {
                "path": "audit-index.md",
                "content": "\n".join(f"{n}: contents of {n}" for n in FILES6)}))
            script.append({"role": "assistant", "content": "DONE: index written."})
            return script[min(phase, len(script) - 1)]

        cfg = make_cfg(d)  # default flat cap is 6; step 2 needs 9 turns
        r = planner.execute_plan("index every file", parse(PRIYA_PLAN),
                                 cfg, chat_fn=fake)
        check("replay plan completes", r.status == "done", r.status)
        check("step 2 records its computed budget",
              (r.steps[1].budget or 0) >= 9, str(r.steps[1].budget))
        check("the index was written",
              Path(d, "audit-index.md").exists())
        check("the index names every file",
              all(n in Path(d, "audit-index.md").read_text() for n in FILES6))
    finally:
        shutil.rmtree(d)


def main():
    t_simple_step_keeps_base_budget()
    t_covers_step_scales_with_source_items()
    t_enumerated_reads_scale_the_budget()
    t_ceiling_caps_the_budget()
    t_replay_priya_shape_completes()
    print("\nAll dynamic step budget tests passed.")


if __name__ == "__main__":
    main()
