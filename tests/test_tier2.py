#!/usr/bin/env python3
"""Tests for Tier 2 discovery (v0.8.1).

Design: some plans cannot be written well until someone has looked.
Tier 2 runs a model-planned, READ-ONLY discovery pass first (list_dir,
read_file, grep_files: the registry is restricted at execution time,
so a discovery step cannot write even if its plan says to), stores its
records like any run, then calls the planner again with the discovery
findings in the prompt. The plan the human approves is the informed
one; approval and execution after that are unchanged.

Run: python3 tests/test_tier2.py
"""
import re
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness import config, loop, planner
from harness.config import Config

DISCOVERY_PLAN = """Feature: Discovery pass
  Scenario: Step 1 - List files
    When I list the files in the workspace
    Then the files are listed
  Scenario: Step 2 - Read the budget
    When I read budget.csv
    Then its contents are known
"""

FINAL_PLAN = """Feature: Organize for real
  Scenario: Step 1 - List files
    When I list the files in the workspace
    Then the files are listed
  Scenario: Step 2 - Write the summary
    When I write summary.txt about budget.csv
    Then "summary.txt" exists
"""


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        raise AssertionError(name)


def make_cfg(d, **kw):
    base = dict(workspace=d, max_steps=12, step_max_steps=8,
                max_consecutive_errors=3)
    base.update(kw)
    return Config(**base)


def t_discovery_prompt_is_read_only():
    d = tempfile.mkdtemp(prefix="tier2-")
    try:
        Path(d, "budget.csv").write_text("a,b\n1,2\n")
        p = planner.build_discovery_prompt("organize the files", workspace=d)
        low = p.lower()
        check("prompt frames a discovery pass", "discovery" in low)
        check("prompt names the read-only tools",
              "list_dir" in p and "read_file" in p and "grep_files" in p)
        check("prompt forbids changing anything",
              "changes nothing" in low or "do not" in low and "write" in low, p[:400])
        check("prompt excludes write tools from the offer",
              "write_file" not in p and "exec" not in p, p[:600])
        check("prompt includes the workspace digest", "budget.csv" in p)
    finally:
        shutil.rmtree(d)


def t_request_discovery_plan_with_retry():
    d = tempfile.mkdtemp(prefix="tier2-")
    try:
        cfg = make_cfg(d)
        responses = iter(["not gherkin",
                            {"role": "assistant", "content": DISCOVERY_PLAN}])
        calls = {"n": 0}

        def fake(messages, tool_defs):
            calls["n"] += 1
            r = next(responses)
            return r if isinstance(r, dict) else {"role": "assistant", "content": r}

        text, steps = planner.request_discovery_plan("organize", cfg, fake)
        check("discovery plan parsed after retry", text is not None and len(steps) == 2)
        check("two calls used", calls["n"] == 2, str(calls["n"]))
    finally:
        shutil.rmtree(d)


def t_loop_only_tools_filters_registry():
    d = tempfile.mkdtemp(prefix="tier2-")
    try:
        cfg = make_cfg(d)
        offered = {}

        def fake(messages, tool_defs):
            offered["names"] = [t["name"] for t in tool_defs or []]
            return {"role": "assistant", "content": "DONE: looked."}

        r = loop.run("look around", cfg, chat_fn=fake, only_tools=["list_dir"])
        check("run completes", r.status == "done", r.status)
        check("only list_dir offered", offered["names"] == ["list_dir"],
              str(offered["names"]))
    finally:
        shutil.rmtree(d)


def t_discovery_execution_cannot_write():
    d = tempfile.mkdtemp(prefix="tier2-")
    try:
        cfg = make_cfg(d)
        evil_plan = """Feature: Sneaky discovery
  Scenario: Step 1 - Write a file
    When I write evil.txt
    Then evil.txt exists
"""
        text, steps = planner.gherkin.parse_feature(evil_plan)[0], None
        plan, err = planner.gherkin.parse_feature(evil_plan)
        steps = planner.to_plan_steps(plan)

        def fake(messages, tool_defs):
            if tool_defs is None:
                return {"role": "assistant", "content": "DONE: summary."}
            return {"role": "assistant", "content": "Writing.",
                    "tool_calls": [{"function": {"name": "write_file",
                                                 "arguments": {"path": "evil.txt",
                                                               "content": "x"}}}]}

        run = planner.execute_plan("sneak", steps, cfg, chat_fn=fake,
                                   only_tools=list(planner.READ_ONLY_TOOLS))
        check("write attempt fails the step", run.status == "step_failed", run.status)
        check("evil.txt was never written", not Path(d, "evil.txt").exists())
    finally:
        shutil.rmtree(d)


def t_findings_text_reads_store_and_caps():
    d = tempfile.mkdtemp(prefix="tier2-")
    try:
        run_dir = Path(d, ".harness", "runs", "20990101-000000")
        run_dir.mkdir(parents=True)
        (run_dir / "step-01.md").write_text("# Step 1\n" + "x" * 20000)
        findings = planner.discovery_findings_text(d, "20990101-000000", 1)
        check("findings bounded", 0 < len(findings) <= 8000, str(len(findings)))
        check("truncation announced with a pointer",
              "truncated" in findings and "step-01.md" in findings, findings[-200:])
        check("no run id, no findings",
              planner.discovery_findings_text(d, "", 3) == "")
    finally:
        shutil.rmtree(d)


def t_plan_with_discovery_end_to_end():
    d = tempfile.mkdtemp(prefix="tier2-")
    try:
        Path(d, "budget.csv").write_text("cat,jan\ngroceries,10\n")
        Path(d, "notes.txt").write_text("hello")
        cfg = make_cfg(d)
        seen = {"replan_prompt": None}
        state = {"task_seen": None, "phase": 0}

        def fake(messages, tool_defs):
            first = messages[0]["content"] if messages else ""
            if tool_defs is None and "You are planning a read-only discovery pass" in first:
                return {"role": "assistant", "content": DISCOVERY_PLAN}
            if tool_defs is None and "Plan execution ended" in first:
                return {"role": "assistant", "content": "DONE: discovered."}
            if tool_defs is None and "You are a planner" in first:
                seen["replan_prompt"] = first
                return {"role": "assistant", "content": FINAL_PLAN}
            task_msg = messages[1]["content"] if len(messages) > 1 else ""
            if task_msg != state["task_seen"]:
                state["task_seen"] = task_msg
                state["phase"] = 0
            phase = state["phase"]
            state["phase"] += 1
            if "You are executing step 1" in task_msg:
                if phase == 0:
                    return {"role": "assistant", "content": "Listing.",
                            "tool_calls": [{"function": {"name": "list_dir",
                                                         "arguments": {"path": "."}}}]}
                return {"role": "assistant", "content": "DONE: listed."}
            if "You are executing step 2" in task_msg:
                if phase == 0:
                    return {"role": "assistant", "content": "Reading.",
                            "tool_calls": [{"function": {"name": "read_file",
                                                         "arguments": {"path": "budget.csv"}}}]}
                return {"role": "assistant", "content": "DONE: read."}
            return {"role": "assistant", "content": "DONE: ok."}

        text, steps, info = planner.plan_with_discovery("organize the files",
                                                        cfg, chat_fn=fake)
        check("final plan returned", text is not None and len(steps) == 2)
        check("discovery ran clean", info["discovery_status"] == "done",
              info["discovery_status"])
        check("discovery run recorded", bool(info["discovery_run_id"]))
        check("findings carry the discovered file",
              "budget.csv" in info["findings"], info["findings"][:300])
        check("replan prompt carried the findings",
              seen["replan_prompt"] is not None
              and "Discovery findings" in seen["replan_prompt"]
              and "budget.csv" in seen["replan_prompt"])
        check("final plan is the informed one",
              "budget.csv" in steps[1].instruction, steps[1].instruction)
    finally:
        shutil.rmtree(d)


def t_config_discover_flag():
    cfg, args = config.from_args(["some task", "--discover"])
    check("--discover parses", args.discover is True)
    cfg, args = config.from_args(["some task"])
    check("--discover defaults off", args.discover is False)


if __name__ == "__main__":
    for fn in [t_discovery_prompt_is_read_only,
               t_request_discovery_plan_with_retry,
               t_loop_only_tools_filters_registry,
               t_discovery_execution_cannot_write,
               t_findings_text_reads_store_and_caps,
               t_plan_with_discovery_end_to_end,
               t_config_discover_flag]:
        fn()
    print("\nAll Tier 2 discovery tests passed.")
