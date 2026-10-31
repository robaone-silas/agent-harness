#!/usr/bin/env python3
"""Tests for the exemplar plans in the planner prompt (v0.8.1).

Field motivation (2026-10-03): with the workspace digest in place, the
planner grounded its categories in the real files but its mechanics
were still weak, and its judgment about where an idiom belongs was off:
it proposed moving files into "a new file named Finance" (folders
conflated with files), and it put the covers clause on the reading
step instead of the deliverable step. Two short exemplar plans teach
the shapes: list-then-derive with covers on the deliverable, and
folders created as folders with moves verified by the destination
path. Example content is deliberately unlike real tasks (recipes,
meeting notes) so the shape transfers without the content being
copied: copy the shape, not the content.

Run: python3 tests/test_examples.py
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


def t_examples_present_with_intent():
    p = planner.build_planner_prompt("organize my stuff", ["list_dir"])
    check("examples carry the copy-shape instruction",
          "shape, not" in p and "content" in p.split("shape, not")[1][:20], p[:200])
    check("recipe example present", "recipe-index.md" in p)
    check("completeness clause sits on the deliverable step in the example",
          '"recipe-index.md" indexes the files from step 1' in p)
    check("archive example present", "notes-2025-03.txt" in p)
    check("folder created as a folder in the example",
          '"archive" exists' in p)
    check("move verified by destination path in the example",
          '"archive/notes-2025-03.txt" exists' in p)
    check("move uses a real move command in the example",
          "mv notes-2025-03.txt archive/" in p)


def t_examples_come_before_task_and_digest_stays_last():
    d = tempfile.mkdtemp(prefix="examples-")
    try:
        Path(d, "real-file.txt").write_text("x")
        p = planner.build_planner_prompt("organize my stuff", ["list_dir"],
                                         workspace=d)
        i_ex = p.index("recipe-index.md")
        i_digest = p.index("Workspace contents")
        i_task = p.index("Task: organize my stuff")
        is_ordered = (i_ex < i_digest) and (i_digest < i_task)
        check("examples before digest before task",
              is_ordered, f"{i_ex} {i_digest} {i_task}")
    finally:
        shutil.rmtree(d)


def t_request_plan_still_works_with_examples():
    d = tempfile.mkdtemp(prefix="examples-")
    try:
        cfg = Config(workspace=d)
        seen = {}

        def fake(messages, tool_defs):
            seen["prompt"] = messages[0]["content"]
            return {"role": "assistant", "content": PLAN_OK}

        text, steps = planner.request_plan("do a thing", ["list_dir"], fake, cfg)
        check("plan returned", text is not None and len(steps) == 1)
        check("planner prompt the model saw has the examples",
              "recipe-index.md" in seen["prompt"])
    finally:
        shutil.rmtree(d)


def t_content_shaped_exemplars_use_attested_clauses():
    # Exemplars whose entries must carry specific content from the
    # files (an article's title and argument, a song's length, a
    # plant's purpose) keep the work with the executor: indexes would
    # replace the planned reading with the harness's generic one-line
    # descriptions, which cannot promise those fields. Their Thens
    # state the contract as prose instead. The recipe exemplar, a
    # plain filename and description index, keeps the indexes clause.
    p = planner.build_planner_prompt("organize my stuff", ["list_dir"])
    check("guide exemplar states its contract in the Then",
          '"guide.md" has one entry for each article from step 1' in p
          and "a one-sentence summary of its argument" in p)
    check("guide exemplar no longer routes through indexes",
          '"guide.md" indexes the files from step 1' not in p)
    check("catalog exemplar states its contract in the Then",
          '"catalog.md" has one entry for each song from step 1' in p)
    check("catalog exemplar no longer routes through indexes",
          '"catalog.md" indexes the files from step 1' not in p)
    check("seed exemplar states its contract in the Then",
          '"seed-inventory.txt" has one entry for each packet from step 1'
          in p)
    check("seed exemplar no longer routes through indexes",
          '"seed-inventory.txt" indexes the files from step 1' not in p)


if __name__ == "__main__":
    for fn in [t_examples_present_with_intent,
               t_examples_come_before_task_and_digest_stays_last,
               t_content_shaped_exemplars_use_attested_clauses,
               t_request_plan_still_works_with_examples]:
        fn()
    print("\nAll planner example tests passed.")
