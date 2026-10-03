#!/usr/bin/env python3
"""Tests for Tier 1 discovery: a deterministic workspace digest in the
planner prompt (v0.8.1).

Field motivation (2026-10-03): the bare prompt "organize files in this
folder by category" produced a plan built from the model's priors:
category folders for .pdf and .jpg files, in a sandbox containing
neither. The planner never saw the folder. Tier 1 is the smallest fix:
the harness itself lists the workspace (names, kinds, sizes; no model
calls, no content reading) and puts that digest in the planner prompt,
with an instruction to plan against what is actually there.

Run: python3 tests/test_discovery.py
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


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        raise AssertionError(name)


def make_ws(files):
    d = tempfile.mkdtemp(prefix="discovery-")
    for name, content in files.items():
        p = Path(d, name)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return d


def t_digest_lists_real_files_with_sizes():
    d = make_ws({"alpha.txt": "abc", "budget.csv": "x,y\n1,2\n"})
    try:
        digest = planner.workspace_digest(d)
        check("digest names real files", "alpha.txt" in digest and "budget.csv" in digest,
              digest)
        check("digest gives sizes", "3 bytes" in digest, digest)
        check("digest carries the plan-against-reality instruction",
              "do not invent" in digest.lower(), digest)
    finally:
        shutil.rmtree(d)


def t_digest_marks_folders_and_does_not_descend():
    d = make_ws({"top.txt": "t", "Archive/inner-secret.txt": "s"})
    try:
        digest = planner.workspace_digest(d)
        check("folder listed as folder", "Archive/" in digest, digest)
        check("folder contents not listed", "inner-secret.txt" not in digest, digest)
    finally:
        shutil.rmtree(d)


def t_digest_hides_harness_bookkeeping():
    d = make_ws({"real.txt": "r", ".harness/runs/20260101-000000/step-01.md": "# Step 1"})
    try:
        digest = planner.workspace_digest(d)
        check("real file listed", "real.txt" in digest, digest)
        check(".harness not listed", ".harness" not in digest, digest)
        check("run records not listed", "step-01.md" not in digest, digest)
    finally:
        shutil.rmtree(d)


def t_digest_empty_and_missing_workspace():
    d = tempfile.mkdtemp(prefix="discovery-")
    try:
        digest = planner.workspace_digest(d)
        check("empty workspace says so", "empty" in digest.lower(), digest)
    finally:
        shutil.rmtree(d)
    check("missing workspace yields no digest (planning proceeds)",
          planner.workspace_digest(str(Path(d, "nope"))) == "")
    check("no workspace yields no digest", planner.workspace_digest(None) == "")


def t_digest_is_capped():
    d = make_ws({f"file-{i:03d}.txt": "x" for i in range(150)})
    try:
        digest = planner.workspace_digest(d)
        listed = digest.count("\n- ")
        check("digest caps listed entries", listed <= 101, str(listed))
        check("truncation is announced", "more" in digest, digest[-200:])
    finally:
        shutil.rmtree(d)


def t_planner_prompt_includes_digest_only_with_workspace():
    d = make_ws({"garden-plan.md": "# Garden"})
    try:
        with_ws = planner.build_planner_prompt("organize files", ["list_dir"],
                                               workspace=d)
        check("prompt includes the digest", "Workspace contents" in with_ws, with_ws[-600:])
        check("prompt includes real filenames", "garden-plan.md" in with_ws)
        without = planner.build_planner_prompt("organize files", ["list_dir"])
        check("no workspace, no digest block", "Workspace contents" not in without)
    finally:
        shutil.rmtree(d)


def t_request_plan_prompt_carries_the_digest():
    d = make_ws({"tax-receipt-2025.txt": "r", "household-budget-2026.csv": "a,b"})
    try:
        cfg = Config(workspace=d)
        seen = {}

        def fake(messages, tool_defs):
            seen["prompt"] = messages[0]["content"]
            return {"role": "assistant", "content": PLAN_OK}

        text, steps = planner.request_plan("organize files by category",
                                           ["list_dir"], fake, cfg)
        check("plan returned", text is not None and len(steps) == 1)
        check("planner saw the real files",
              "tax-receipt-2025.txt" in seen["prompt"]
              and "household-budget-2026.csv" in seen["prompt"], seen["prompt"][-700:])
    finally:
        shutil.rmtree(d)


if __name__ == "__main__":
    for fn in [t_digest_lists_real_files_with_sizes,
               t_digest_marks_folders_and_does_not_descend,
               t_digest_hides_harness_bookkeeping,
               t_digest_empty_and_missing_workspace,
               t_digest_is_capped,
               t_planner_prompt_includes_digest_only_with_workspace,
               t_request_plan_prompt_carries_the_digest]:
        fn()
    print("\nAll discovery tests passed.")
