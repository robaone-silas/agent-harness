#!/usr/bin/env python3
"""Tests for presenting a saved plan in the planner prompt.

The saved plan is an example only. The planner still calls the model
and returns the model's new plan, never the saved plan verbatim, and
review still happens in run.py. These tests capture the prompt the
model sees, using a stub chat function, the way the exemplar tests do.

Run: python3 tests/test_plan_history_prompt.py
"""
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness import plan_history as plan_history_module
from harness import planner
from harness.config import Config

SAVED_INDEX_PLAN = """Feature: Recipe index
  Scenario: Step 1 - List the recipes
    When I list the files in the folder
    Then the output lists the recipe files
  Scenario: Step 2 - Write the index
    Intent: know exactly which recipes the index must cover
    When I write recipe-index.md indexing the listed recipes
    Then "recipe-index.md" indexes the files from step 1
"""

GARDEN_PLAN = """Feature: Garden watering
  Scenario: Step 1 - Water the plants
    When I water the garden plants
    Then the garden plants are watered
"""

MODEL_SONG_PLAN = """Feature: Song index
  Scenario: Step 1 - List the songs
    When I list the files in the folder
    Then the output lists the song files
  Scenario: Step 2 - Write the index
    When I write song-index.md indexing the listed songs
    Then "song-index.md" indexes the files from step 1
"""

SECTION_HEADER = "A similar plan from an earlier successful run (example only):"


def check(name, condition, detail=""):
    print(("PASS " if condition else "FAIL ") + name + (f" -- {detail}" if detail and not condition else ""))
    if not condition:
        raise AssertionError(name)


def make_workspace():
    return tempfile.mkdtemp(prefix="plan-history-prompt-")


def capture_prompt(task_text, temp_workspace):
    captured_prompt = {}

    def stub_chat(messages, tool_definitions):
        captured_prompt["prompt"] = messages[0]["content"]
        return {"role": "assistant", "content": MODEL_SONG_PLAN}

    config = Config(workspace=temp_workspace)
    returned_text, returned_steps = planner.request_plan(task_text, ["list_dir"], stub_chat, config)
    return captured_prompt["prompt"], returned_text, returned_steps


def t_no_history_prompt_has_no_past_plan_section():
    temp_workspace = make_workspace()
    try:
        prompt, returned_text, _ = capture_prompt("Create an index of the songs in this folder", temp_workspace)
        check("no history means no past plan section", SECTION_HEADER not in prompt)
        check("model plan is still returned", returned_text is not None and "Song index" in returned_text)
    finally:
        shutil.rmtree(temp_workspace)


def t_matching_history_prompt_includes_past_plan_section():
    temp_workspace = make_workspace()
    try:
        plan_history_module.save_plan(
            temp_workspace, SAVED_INDEX_PLAN,
            task_text="Make an index of the recipes in this folder",
            status="done", run_id="run-index")
        prompt, returned_text, _ = capture_prompt("Create an index of the songs in this folder", temp_workspace)
        check("matching history adds exactly one past plan section",
              prompt.count(SECTION_HEADER) == 1, str(prompt.count(SECTION_HEADER)))
        check("past plan section carries the saved plan text",
              SAVED_INDEX_PLAN.strip() in prompt)
        check("past plan section carries the adapt-or-ignore instruction",
              "Copy the shape, not the content." in prompt
              and "If the example does not fit this request, ignore it." in prompt)
        check("planner returns the model plan, not the saved plan verbatim",
              returned_text is not None and returned_text.strip() != SAVED_INDEX_PLAN.strip()
              and "song-index.md" in returned_text, repr(returned_text))
    finally:
        shutil.rmtree(temp_workspace)


def t_nonmatching_history_prompt_has_no_past_plan_section():
    temp_workspace = make_workspace()
    try:
        plan_history_module.save_plan(
            temp_workspace, GARDEN_PLAN,
            task_text="Water the garden plants every morning",
            status="done", run_id="run-garden")
        prompt, _, _ = capture_prompt("Create an index of the songs in this folder", temp_workspace)
        check("non-matching history adds no past plan section", SECTION_HEADER not in prompt)
    finally:
        shutil.rmtree(temp_workspace)


def t_build_prompt_renders_section_only_with_a_past_plan():
    prompt_with_past = planner.build_planner_prompt(
        "Create an index of the songs in this folder", ["list_dir"],
        past_plan_text=SAVED_INDEX_PLAN)
    prompt_without_past = planner.build_planner_prompt(
        "Create an index of the songs in this folder", ["list_dir"])
    check("build prompt with a past plan renders the section",
          SECTION_HEADER in prompt_with_past and SAVED_INDEX_PLAN.strip() in prompt_with_past)
    check("build prompt without a past plan renders no section",
          SECTION_HEADER not in prompt_without_past)


if __name__ == "__main__":
    for test_function in [t_no_history_prompt_has_no_past_plan_section,
                          t_matching_history_prompt_includes_past_plan_section,
                          t_nonmatching_history_prompt_has_no_past_plan_section,
                          t_build_prompt_renders_section_only_with_a_past_plan]:
        test_function()
    print("\nAll plan history prompt tests passed.")
