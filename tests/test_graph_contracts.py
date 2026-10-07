"""Tests for graph contracts in step prompts (experiment, default off).

When Config.graph_contracts is on, each plan step's execution prompt
carries the step's node contract from the plan graph (harness/
graphcheck.py): the input fields available to it, with their
producers, and the output fields it must produce. The block is
computed from the plan text alone (no workspace scan), so every step
sees the same graph the approval display derives from the plan.

Run: python3 tests/test_graph_contracts.py
"""
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness import planner
from harness.config import Config, from_args
from harness.planner import PlanStep, to_plan_steps
from harness import gherkin

HEALTHY_PLAN = """Feature: File Audit Index
  Scenario: Step 1 - List all files
    Intent: know exactly which files the audit index must cover
    When I list the files in the folder
    Then the output lists all files in the workspace
  Scenario: Step 2 - Read files and write the index
    Intent: create audit-index.md listing every file with a one-line description
    When I read every file from step 1 and write audit-index.md
    Then "audit-index.md" indexes the files from step 1
"""

TOTALS_PLAN = """Feature: Contracts
  Scenario: Step 1 - Make totals
    When I write totals.csv with the summed values
    Then "totals.csv" exists
  Scenario: Step 2 - Write the report
    When I read totals.csv and write report.md
    Then "report.md" exists
"""


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name
          + (f" -- {detail}" if detail and not cond else ""))
    assert cond, f"{name}: {detail}"


def t_config_default_off_and_flag():
    check("graph contracts default off",
          Config().graph_contracts is False)
    cfg, _args = from_args(["some task", "--graph-contracts"])
    check("--graph-contracts turns it on", cfg.graph_contracts is True)
    cfg2, _ = from_args(["some task"])
    check("absent flag stays off", cfg2.graph_contracts is False)


def t_block_content():
    plan, err = gherkin.parse_feature(HEALTHY_PLAN)
    assert plan is not None, err
    steps = to_plan_steps(plan)
    task = "Read every file, then create audit-index.md."
    b1 = planner._graph_contract_block(task, steps[0], steps)
    check("listing step block names its list output",
          "Outputs this step must produce" in b1
          and '"files from step 1"' in b1, b1)
    b2 = planner._graph_contract_block(task, steps[1], steps)
    check("compile step block names the listing input and its producer",
          '"files from step 1" (produced by step 1)' in b2, b2)
    check("compile step block names its required output",
          '"audit-index.md"' in b2, b2)
    check("block states the completion rule",
          "complete only when every output above exists" in b2, b2)


def t_block_empty_when_no_fields():
    step = PlanStep(id=1, instruction="Say hello to the user",
                    done_when="the greeting is given")
    block = planner._graph_contract_block("Greet the user.", step, [step])
    check("no fields, no block", block == "", repr(block))


def _run_and_capture(graph_contracts):
    d = tempfile.mkdtemp(prefix="graph-contracts-test-")
    cfg = Config(workspace=d, max_steps=12, step_max_steps=6,
                 max_consecutive_errors=3, graph_contracts=graph_contracts)
    prompts = {}

    def fake(messages, tool_defs):
        if tool_defs is None and "Break the task" in messages[0]["content"]:
            return {"role": "assistant", "content": TOTALS_PLAN}
        if tool_defs is None:
            return {"role": "assistant", "content": "DONE: ok."}
        task_msg = messages[1]["content"] if len(messages) > 1 else ""
        sid = next((s for s in (1, 2)
                    if f"You are executing step {s} of 2" in task_msg), None)
        if sid is None:
            return {"role": "assistant", "content": "DONE: step done."}
        if sid not in prompts:
            prompts[sid] = task_msg
        marker = Path(d, f".fake-step{sid}-acted")
        if marker.exists():
            return {"role": "assistant",
                    "content": f"DONE: step {sid} done."}
        marker.write_text("1")
        action = {1: ("totals.csv", "a,1"), 2: ("report.md", "done")}[sid]
        return {"role": "assistant", "content": f"Writing {action[0]}.",
                "tool_calls": [{"function": {
                    "name": "write_file",
                    "arguments": {"path": action[0],
                                  "content": action[1]}}}]}

    r = planner.run_planned("contracts goal", cfg, chat_fn=fake)
    return r, prompts, d


def t_prompt_integration_flag_on():
    r, prompts, d = _run_and_capture(True)
    try:
        check("run done with contracts on", r.status == "done", r.status)
        p2 = prompts[2]
        check("step 2 prompt carries the graph block",
              "plan graph" in p2, p2[:400])
        check("step 2 prompt names its input and producer",
              '"totals.csv" (produced by step 1)' in p2, p2)
        check("step 2 prompt names its required output",
              '"report.md"' in p2, p2)
        check("step 1 prompt names its output too",
              '"totals.csv"' in prompts[1], prompts[1][:400])
    finally:
        shutil.rmtree(d)


def t_prompt_integration_flag_off():
    r, prompts, d = _run_and_capture(False)
    try:
        check("run done with contracts off", r.status == "done", r.status)
        check("no graph block when the flag is off",
              all("plan graph" not in p for p in prompts.values()),
              str(list(prompts)))
    finally:
        shutil.rmtree(d)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("t_") and callable(v)]
    for fn in fns:
        fn()
    print("\nAll graph contract tests passed.")
