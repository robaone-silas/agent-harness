#!/usr/bin/env python3
"""Tests for task-sensitive discovery steering and final-planner
reconciliation (issue #6, remediation step 4).

Field motivation (2026-10-03, Tomas Rivera's persona run): the discovery
prompt told the planner to read the files most likely to decide the
plan's categories or values and expressly not to read everything. That
sampling rule is sensible for learning categories or values, but Tomas's
task was a list of his house papers and what each one was for: every
root-level record was potentially in scope, and the sample silently
dropped water-heater-manual.txt. Separately, the final planner received
the four-file findings with no instruction to reconcile them against
the workspace, so it copied the discovery scope and called it "all
house documents".

Step 3 added the deterministic unexamined-file annotation to the
findings. This step makes the prompts task-sensitive:

- For list, inventory, index, or all/every-file tasks, the discovery
  prompt requires a listing step and accounting for every root-level
  file. Sampling is not acceptable for those tasks.
- For category, strategy, and value discovery, sampling remains
  acceptable and the original sampling guidance stays.
- The final planner prompt (when discovery findings are present) gains
  a reconciliation instruction: a workspace file absent from the
  findings is unexamined, not automatically out of scope.

Run: python3 tests/test_discovery_prompt_reconciliation.py
"""
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness import planner
from harness.config import Config

PLAN_OK = """Feature: Anything
  Scenario: Step 1 - Look
    When List the files
    Then the list is recorded
"""

EXHAUSTIVE_TASKS = [
    "Make a list of the house papers in this folder and say what each one is for",
    "Make an inventory of every file in this folder",
    "Make an index of all the documents in this folder, with a one-line description of each",
    "List all files in this folder and describe each file",
]

SAMPLING_TASKS = [
    "Suggest an organization strategy for the documents in this folder based on their categories",
    "Organize the files in this folder by category",
    "Recommend a filing strategy based on the values in these records",
]


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        raise AssertionError(name)


def make_ws(files):
    d = tempfile.mkdtemp(prefix="prompt-recon-")
    for name, content in files.items():
        p = Path(d, name)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return d


def t_exhaustive_discovery_prompt_requires_listing_and_full_accounting():
    d = make_ws({"water-heater-manual.txt": "w", "roof-warranty.txt": "r"})
    try:
        for task in EXHAUSTIVE_TASKS:
            p = planner.build_discovery_prompt(task, workspace=d)
            low = p.lower()
            check(f"exhaustive prompt requires a listing step: {task[:40]}",
                  "listing step" in low, p)
            check(f"exhaustive prompt accounts for every root file: {task[:40]}",
                  "every file at the workspace root" in low, p)
            check(f"exhaustive prompt leaves no root file unaccounted: {task[:40]}",
                  "unaccounted" in low, p)
    finally:
        shutil.rmtree(d)


def t_exhaustive_discovery_prompt_rejects_sampling():
    d = make_ws({"a.txt": "a"})
    try:
        for task in EXHAUSTIVE_TASKS:
            p = planner.build_discovery_prompt(task, workspace=d)
            low = p.lower()
            check(f"sampling ruled out for exhaustive task: {task[:40]}",
                  "sampling is not acceptable" in low, p)
            check(f"old sampling instruction gone for exhaustive task: {task[:40]}",
                  "do not read everything" not in low, p)
    finally:
        shutil.rmtree(d)


def t_sampling_discovery_prompt_keeps_sampling_guidance():
    d = make_ws({"a.txt": "a", "b.txt": "b"})
    try:
        for task in SAMPLING_TASKS:
            p = planner.build_discovery_prompt(task, workspace=d)
            low = p.lower()
            check(f"sampling acceptable for category/strategy task: {task[:40]}",
                  "sampling is acceptable" in low, p)
            check(f"sampling guidance retained: {task[:40]}",
                  "most likely to decide" in low
                  and "do not read everything" in low, p)
            check(f"no exhaustive demand for sampling task: {task[:40]}",
                  "every file at the workspace root" not in low, p)
    finally:
        shutil.rmtree(d)


def t_final_planner_prompt_reconciles_findings_against_workspace():
    d = make_ws({"budget.csv": "a,b", "water-heater-manual.txt": "w"})
    try:
        findings = ("Step 1 read budget.csv.\n\n"
                    "Unexamined workspace files: water-heater-manual.txt")
        p = planner.build_planner_prompt(
            "Make a list of the house papers", ["list_dir", "read_file"],
            workspace=d, discovery=findings)
        low = p.lower()
        check("final prompt carries the findings", "budget.csv" in p)
        check("final prompt tells the planner to reconcile findings "
              "against the workspace contents",
              "reconcile" in low and "workspace contents" in low, p[-1200:])
        check("absent from findings means unexamined, not out of scope",
              "unexamined, not automatically out of scope" in low, p[-1200:])
    finally:
        shutil.rmtree(d)


def t_request_plan_prompt_carries_the_reconciliation():
    d = make_ws({"budget.csv": "a,b", "mystery.txt": "m"})
    try:
        cfg = Config(workspace=d)
        seen = {}

        def fake(messages, tool_defs):
            seen["prompt"] = messages[0]["content"]
            return {"role": "assistant", "content": PLAN_OK}

        text, steps = planner.request_plan(
            "Make a list of the house papers", ["list_dir"], fake, cfg,
            discovery="Step 1 read budget.csv.")
        check("plan returned", text is not None and len(steps) == 1)
        check("request_plan prompt carries the reconciliation instruction",
              "unexamined, not automatically out of scope"
              in seen["prompt"].lower(), seen["prompt"][-1200:])
    finally:
        shutil.rmtree(d)


def t_request_discovery_plan_prompt_is_task_sensitive_end_to_end():
    d = make_ws({"water-heater-manual.txt": "w"})
    try:
        cfg = Config(workspace=d)
        seen = {}

        def fake(messages, tool_defs):
            seen["prompt"] = messages[0]["content"]
            return {"role": "assistant", "content": PLAN_OK}

        text, steps = planner.request_discovery_plan(
            "Make a list of the house papers in this folder and say "
            "what each one is for", cfg, fake)
        check("discovery plan returned", text is not None and len(steps) == 1)
        low = seen["prompt"].lower()
        check("discovery planner was steered exhaustively",
              "listing step" in low
              and "every file at the workspace root" in low
              and "sampling is not acceptable" in low, seen["prompt"])
    finally:
        shutil.rmtree(d)


if __name__ == "__main__":
    for fn in [t_exhaustive_discovery_prompt_requires_listing_and_full_accounting,
               t_exhaustive_discovery_prompt_rejects_sampling,
               t_sampling_discovery_prompt_keeps_sampling_guidance,
               t_final_planner_prompt_reconciles_findings_against_workspace,
               t_request_plan_prompt_carries_the_reconciliation,
               t_request_discovery_plan_prompt_is_task_sensitive_end_to_end]:
        fn()
    print("\nAll discovery prompt reconciliation tests passed.")
