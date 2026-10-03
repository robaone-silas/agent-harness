#!/usr/bin/env python3
"""Tests for two papercuts found live on 2026-10-03.

1. --task-file reached nobody. config.from_args read the task file into
   a local variable but returned the argparse namespace, and run.py reads
   args.task, which was still None: the planner was handed no task and
   produced a plan titled "No Task Provided". The resolved task must be
   what callers get.

2. The step prompt's stored-at line ("Your full output will be stored at
   <path> for later steps") reads as a write instruction to a small model:
   in the live run, step 2 wrote its strategy into its own record file.
   The line must say the harness does the storing, and that the model
   should not write there. Sequel, same day: a step pushed to read a
   prior step's record read its OWN record path instead, which does not
   exist until the step finishes; the line must also say the file does
   not exist yet and must not be read.

Run: python3 tests/test_papercuts.py
"""
import re
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness import config, planner
from harness.config import Config


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        raise AssertionError(name)


def t_task_file_reaches_args():
    d = tempfile.mkdtemp(prefix="papercut-")
    try:
        f = Path(d, "task.txt")
        f.write_text("organize the files in this folder\n")
        cfg, args = config.from_args(["--task-file", str(f), "--plan"])
        check("args.task holds the file's text",
              args.task == "organize the files in this folder",
              repr(args.task))
    finally:
        shutil.rmtree(d)


def t_positional_task_unchanged():
    cfg, args = config.from_args(["do the thing", "--plan"])
    check("positional task unchanged", args.task == "do the thing", repr(args.task))


def t_run_mode_needs_no_task():
    cfg, args = config.from_args(["--run", "plan.feature"])
    check("run mode parses without a task", args.run == "plan.feature")


PLAN2 = """Feature: Papercut stored-at
  Scenario: Step 1 - List files
    When List the files in the workspace
    Then the list is recorded
  Scenario: Step 2 - Summarize
    When Write summary.txt from the list
    Then "summary.txt" exists
"""


def t_stored_at_line_is_not_a_write_instruction():
    d = tempfile.mkdtemp(prefix="papercut-")
    try:
        Path(d, "alpha.txt").write_text("a")
        cfg = Config(workspace=d, max_steps=12, step_max_steps=8,
                     max_consecutive_errors=3)
        prompts = []

        def fake(messages, tool_defs):
            if tool_defs is None and "Break the task" in messages[0]["content"]:
                return {"role": "assistant", "content": PLAN2}
            if tool_defs is None:
                return {"role": "assistant", "content": "DONE: ok."}
            task_msg = messages[1]["content"] if len(messages) > 1 else ""
            if "You are executing step" in task_msg and task_msg not in prompts:
                prompts.append(task_msg)
            if "You are executing step 1" in task_msg:
                if not getattr(fake, "_listed", False):
                    fake._listed = True
                    return {"role": "assistant", "content": "Listing.",
                            "tool_calls": [{"function": {"name": "list_dir",
                                                         "arguments": {"path": "."}}}]}
                return {"role": "assistant", "content": "DONE: listed."}
            if "You are executing step 2" in task_msg:
                m = re.search(r"\.harness/runs/\S+/step-01\.md", task_msg)
                if not getattr(fake, "_acted", False):
                    fake._acted = True
                    if m:
                        return {"role": "assistant", "content": "Reading.",
                                "tool_calls": [{"function": {"name": "read_file",
                                                             "arguments": {"path": m.group(0)}}}]}
                    return {"role": "assistant", "content": "DONE: done."}
                if not getattr(fake, "_wrote", False):
                    fake._wrote = True
                    return {"role": "assistant", "content": "Writing.",
                            "tool_calls": [{"function": {"name": "write_file",
                                                         "arguments": {"path": "summary.txt",
                                                                       "content": "alpha.txt"}}}]}
                return {"role": "assistant", "content": "DONE: done."}
            return {"role": "assistant", "content": "DONE: done."}

        r = planner.run_planned("summarize", cfg, chat_fn=fake)
        check("run done", r.status == "done", r.status)
        step_prompts = [p for p in prompts if "stored" in p or "store" in p]
        joined = "\n".join(prompts)
        check("stored-at line present in a step prompt",
              "step-02.md" in joined, joined[:400])
        check("old phrasing gone",
              "Your full output will be stored at" not in joined)
        check("line says the harness does the storing",
              "harness will store" in joined.lower(), joined[:800])
        check("line says do not write there",
              "do not write" in joined.lower(), joined[:800])
        check("prompt says the step's own output file does not exist yet",
              "does not exist yet" in joined.lower(), joined[:800])
        check("prompt says do not try to read the step's own output file",
              "do not try to read it" in joined.lower(), joined[:800])
    finally:
        shutil.rmtree(d)


if __name__ == "__main__":
    for fn in [t_task_file_reaches_args, t_positional_task_unchanged,
               t_run_mode_needs_no_task, t_stored_at_line_is_not_a_write_instruction]:
        fn()
    print("\nAll papercut tests passed.")
