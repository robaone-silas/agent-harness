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

from harness import gherkin as gherkin_mod
from harness import planner
from harness import verify as verify_mod
from harness.config import Config

PLAN_OK = """Feature: Anything
  Scenario: Step 1 - Look
    When List the files
    Then the list is recorded
"""


def check(name, condition, detail=""):
    print(("PASS " if condition else "FAIL ") + name + (f" -- {detail}" if detail and not condition else ""))
    if not condition:
        raise AssertionError(name)


def t_examples_present_with_intent():
    prompt = planner.build_planner_prompt("organize my stuff", ["list_dir"])
    check("examples carry the copy-shape instruction",
          "shape, not" in prompt and "content" in prompt.split("shape, not")[1][:20], prompt[:200])
    check("recipe example present", "recipe-index.md" in prompt)
    check("completeness clause sits on the deliverable step in the example",
          '"recipe-index.md" indexes the files from step 1' in prompt)
    check("archive example present", "notes-2025-03.txt" in prompt)
    check("folder created as a folder in the example",
          '"archive" exists' in prompt)
    check("move verified by destination path in the example",
          '"archive/notes-2025-03.txt" exists' in prompt)
    check("move uses a real move command in the example",
          "mv notes-2025-03.txt archive/" in prompt)


def t_examples_come_before_task_and_digest_stays_last():
    temp_directory = tempfile.mkdtemp(prefix="examples-")
    try:
        Path(temp_directory, "real-file.txt").write_text("x")
        prompt = planner.build_planner_prompt("organize my stuff", ["list_dir"],
                                              workspace=temp_directory)
        example_index = prompt.index("recipe-index.md")
        digest_index = prompt.index("Workspace contents")
        task_index = prompt.index("Task: organize my stuff")
        is_ordered = (example_index < digest_index) and (digest_index < task_index)
        check("examples before digest before task",
              is_ordered, f"{example_index} {digest_index} {task_index}")
    finally:
        shutil.rmtree(temp_directory)


def t_request_plan_still_works_with_examples():
    temp_directory = tempfile.mkdtemp(prefix="examples-")
    try:
        config = Config(workspace=temp_directory)
        captured_data = {}

        def fake_chat(messages, tool_definitions):
            captured_data["prompt"] = messages[0]["content"]
            return {"role": "assistant", "content": PLAN_OK}

        text, steps = planner.request_plan("do a thing", ["list_dir"], fake_chat, config)
        check("plan returned", text is not None and len(steps) == 1)
        check("planner prompt the model saw has the examples",
              "recipe-index.md" in captured_data["prompt"])
    finally:
        shutil.rmtree(temp_directory)


def t_content_shaped_exemplars_use_attested_clauses():
    # Exemplars whose entries must carry specific content from the
    # files (an article's title and argument, a song's length, a
    # plant's purpose) keep the work with the executor: indexes would
    # replace the planned reading with the harness's generic one-line
    # descriptions, which cannot promise those fields. Their Thens
    # state the contract as prose instead. The recipe exemplar, a
    # plain filename and description index, keeps the indexes clause.
    prompt = planner.build_planner_prompt("organize my stuff", ["list_dir"])
    check("guide exemplar states its contract in the Then",
          '"guide.md" has one entry for each article from step 1' in prompt
          and "a one-sentence summary of its argument" in prompt)
    check("guide exemplar no longer routes through indexes",
          '"guide.md" indexes the files from step 1' not in prompt)
    check("catalog exemplar states its contract in the Then",
          '"catalog.md" has one entry for each song from step 1' in prompt)
    check("catalog exemplar no longer routes through indexes",
          '"catalog.md" indexes the files from step 1' not in prompt)
    check("seed exemplar states its contract in the Then",
          '"seed-inventory.txt" has one entry for each packet from step 1'
          in prompt)
    check("seed exemplar no longer routes through indexes",
          '"seed-inventory.txt" indexes the files from step 1' not in prompt)


def t_compile_exemplar_guards_with_preserves_and_excludes():
    # Issue #37 slice 3: the planner prompt must show a compile step
    # guarded by preserves and excludes, so a small model has a
    # concrete shape to copy. The exemplar's content is deliberately
    # unlike the repair-shop tasks that motivated #37: copy the shape,
    # not the content. Both clauses must parse to their verifiers,
    # not fall back to attestation, or the exemplar teaches trust.
    prompt = planner.build_planner_prompt("organize my stuff", ["list_dir"])
    check("compile exemplar present", "roster.txt" in prompt)
    preserves_clause = '"roster.txt" preserves volunteer, role, and shift from step 1'
    excludes_clause = '"roster.txt" excludes phone numbers from step 1'
    check("exemplar compile step carries a preserves clause",
          preserves_clause in prompt)
    check("exemplar compile step carries an excludes clause",
          excludes_clause in prompt)
    parsed_preserves = verify_mod.parse_then(preserves_clause)
    check("exemplar preserves clause parses to the preserves verifier",
          parsed_preserves is not None and parsed_preserves[0] == "preserves",
          repr(parsed_preserves))
    check("exemplar preserves clause targets the deliverable",
          parsed_preserves is not None and parsed_preserves[1] == "roster.txt",
          repr(parsed_preserves))
    parsed_excludes = verify_mod.parse_then(excludes_clause)
    check("exemplar excludes clause parses to the excludes verifier",
          parsed_excludes is not None and parsed_excludes[0] == "excludes",
          repr(parsed_excludes))
    check("exemplar names its deliverable in the compile When",
          "write roster.txt with one entry per volunteer from step 1" in prompt)
    roster_feature_start = prompt.index("Feature: Volunteer roster")
    next_task_index = prompt.index("Task: Add up the amounts", roster_feature_start)
    parsed_plan, parse_error = gherkin_mod.parse_feature(prompt[roster_feature_start:next_task_index])
    check("exemplar plan parses as valid Gherkin", parsed_plan is not None, parse_error)
    compile_step = parsed_plan.steps[1] if parsed_plan is not None else None
    check("exemplar compile step has two checkable Thens",
          compile_step is not None and len(compile_step.then) == 2,
          repr(compile_step.then if compile_step else None))


if __name__ == "__main__":
    for test_function in [t_examples_present_with_intent,
                          t_examples_come_before_task_and_digest_stays_last,
                          t_content_shaped_exemplars_use_attested_clauses,
                          t_compile_exemplar_guards_with_preserves_and_excludes,
                          t_request_plan_still_works_with_examples]:
        test_function()
    print("\nAll planner example tests passed.")
