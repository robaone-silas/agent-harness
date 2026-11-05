#!/usr/bin/env python3
"""Tests for plan history: save successful plans, query by similarity.

A saved plan is an example for a new plan, never a plan to execute
verbatim. These tests pin the three variations the feature was built
for: no saved plans, a matching saved plan, and a non-matching saved
plan, plus the surrounding rules: only verified plans save, the cap,
deduplication, and a corrupt index never breaking planning.

Run: python3 tests/test_plan_history.py
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness import plan_history as plan_history_module

INDEX_PLAN = """Feature: Recipe index
  Scenario: Step 1 - List the recipes
    When I list the files in the folder
    Then the output lists the recipe files
  Scenario: Step 2 - Write the index
    Intent: know exactly which recipes the index must cover
    When I write recipe-index.md indexing the listed recipes
    Then "recipe-index.md" indexes the files from step 1
"""

ARCHIVE_PLAN = """Feature: Archive meeting notes
  Scenario: Step 1 - List the notes
    When I list the files in the folder
    Then the output lists the note files
  Scenario: Step 2 - Create the archive folder
    When I create the folder "archive"
    Then "archive" exists
"""

GARDEN_PLAN = """Feature: Garden watering
  Scenario: Step 1 - Water the plants
    When I water the garden plants
    Then the garden plants are watered
"""

INDEX_TASK = "Make an index of the recipes in this folder"
NEW_INDEX_TASK = "Create an index of the songs in this folder"
UNRELATED_TASK = "Water the garden plants every morning"


def check(name, condition, detail=""):
    print(("PASS " if condition else "FAIL ") + name + (f" -- {detail}" if detail and not condition else ""))
    if not condition:
        raise AssertionError(name)


def make_workspace():
    temp_workspace = tempfile.mkdtemp(prefix="plan-history-")
    return temp_workspace


def cleanup_workspace(temp_workspace):
    shutil.rmtree(temp_workspace)


def history_directory(temp_workspace):
    return Path(temp_workspace) / ".harness" / "plans"


def t_no_saved_plans_returns_empty():
    temp_workspace = make_workspace()
    try:
        matches = plan_history_module.find_similar(temp_workspace, NEW_INDEX_TASK)
        check("no saved plans returns an empty list", matches == [], repr(matches))
    finally:
        cleanup_workspace(temp_workspace)


def t_missing_workspace_returns_empty():
    temp_workspace = make_workspace()
    missing_workspace = Path(temp_workspace) / "does-not-exist"
    try:
        matches = plan_history_module.find_similar(missing_workspace, NEW_INDEX_TASK)
        check("missing workspace returns an empty list", matches == [], repr(matches))
    finally:
        cleanup_workspace(temp_workspace)


def t_matching_plan_ranks_first_above_threshold():
    temp_workspace = make_workspace()
    try:
        plan_history_module.save_plan(temp_workspace, INDEX_PLAN, task_text=INDEX_TASK, status="done", run_id="run-index")
        plan_history_module.save_plan(temp_workspace, GARDEN_PLAN, task_text=UNRELATED_TASK, status="done", run_id="run-garden")
        matches = plan_history_module.find_similar(temp_workspace, NEW_INDEX_TASK)
        check("matching plan is returned", len(matches) == 1, repr(matches))
        check("matching plan is the index plan", matches[0][0]["title"] == "Recipe index", repr(matches))
        check("matching plan scores at or above threshold",
              matches[0][1] >= plan_history_module.SIMILARITY_THRESHOLD, repr(matches))
    finally:
        cleanup_workspace(temp_workspace)


def t_nonmatching_plan_is_not_returned():
    temp_workspace = make_workspace()
    try:
        plan_history_module.save_plan(temp_workspace, GARDEN_PLAN, task_text=UNRELATED_TASK, status="done", run_id="run-garden")
        matches = plan_history_module.find_similar(temp_workspace, NEW_INDEX_TASK)
        check("non-matching plan is not returned", matches == [], repr(matches))
    finally:
        cleanup_workspace(temp_workspace)


def t_save_writes_feature_file_and_index_entry():
    temp_workspace = make_workspace()
    try:
        record = plan_history_module.save_plan(temp_workspace, INDEX_PLAN, task_text=INDEX_TASK, status="done", run_id="run-index")
        check("save returns a record", record is not None, repr(record))
        saved_file = history_directory(temp_workspace) / record["file_name"]
        check("saved feature file exists", saved_file.is_file())
        check("saved feature file holds the plan text",
              saved_file.read_text() == INDEX_PLAN, saved_file.read_text()[:100])
        index_path = history_directory(temp_workspace) / "index.json"
        index_records = json.loads(index_path.read_text())
        check("index holds one record", len(index_records) == 1, repr(index_records))
        check("index record carries the title", index_records[0]["title"] == "Recipe index")
        check("index record carries the deliverable",
              "recipe-index.md" in index_records[0]["deliverable_names"], repr(index_records[0]))
        check("index record carries the run id", index_records[0]["run_id"] == "run-index")
        check("index record carries the status", index_records[0]["status"] == "done")
    finally:
        cleanup_workspace(temp_workspace)


def t_unparseable_plan_is_not_saved():
    temp_workspace = make_workspace()
    try:
        record = plan_history_module.save_plan(temp_workspace, "this is not a plan", task_text="do a thing", status="done", run_id="run-bad")
        check("unparseable plan returns no record", record is None, repr(record))
        check("unparseable plan creates no history directory",
              not history_directory(temp_workspace).exists())
    finally:
        cleanup_workspace(temp_workspace)


def t_deduplication_saves_second_and_removes_old():
    temp_workspace = make_workspace()
    try:
        first_record = plan_history_module.save_plan(temp_workspace, INDEX_PLAN, task_text=INDEX_TASK, status="done", run_id="run-first")
        second_plan = INDEX_PLAN.replace("recipe-index.md", "recipe-index-two.md")
        second_record = plan_history_module.save_plan(temp_workspace, second_plan, task_text=INDEX_TASK, status="done", run_id="run-second")
        index_records = json.loads((history_directory(temp_workspace) / "index.json").read_text())
        check("deduplication leaves one record", len(index_records) == 1, repr(index_records))
        check("deduplication keeps the second record", index_records[0]["run_id"] == "run-second", repr(index_records))
        check("old feature file is removed",
              not (history_directory(temp_workspace) / first_record["file_name"]).exists())
        check("new feature file exists",
              (history_directory(temp_workspace) / second_record["file_name"]).is_file())
    finally:
        cleanup_workspace(temp_workspace)


def t_cap_fifty_drops_oldest():
    temp_workspace = make_workspace()
    try:
        first_record = None
        for plan_number in range(51):
            task_text = f"Do distinct task number {plan_number} with unique word zebra{plan_number}"
            plan_text = INDEX_PLAN.replace("Recipe index", f"Distinct plan {plan_number}")
            record = plan_history_module.save_plan(temp_workspace, plan_text, task_text=task_text, status="done", run_id=f"run-{plan_number}")
            if plan_number == 0:
                first_record = record
        index_records = json.loads((history_directory(temp_workspace) / "index.json").read_text())
        check("cap keeps at most fifty records", len(index_records) == 50, str(len(index_records)))
        check("oldest record is dropped",
              all(record_item["run_id"] != "run-0" for record_item in index_records))
        check("oldest feature file is removed",
              not (history_directory(temp_workspace) / first_record["file_name"]).exists())
        check("newest record is kept",
              any(record_item["run_id"] == "run-50" for record_item in index_records))
    finally:
        cleanup_workspace(temp_workspace)


def t_corrupt_index_is_treated_as_empty():
    temp_workspace = make_workspace()
    try:
        history_directory(temp_workspace).mkdir(parents=True)
        (history_directory(temp_workspace) / "index.json").write_text("this is not json {")
        matches = plan_history_module.find_similar(temp_workspace, NEW_INDEX_TASK)
        check("corrupt index is treated as empty", matches == [], repr(matches))
        record = plan_history_module.save_plan(temp_workspace, INDEX_PLAN, task_text=INDEX_TASK, status="done", run_id="run-index")
        check("save after a corrupt index still works", record is not None, repr(record))
        index_records = json.loads((history_directory(temp_workspace) / "index.json").read_text())
        check("save after a corrupt index rewrites a valid index",
              len(index_records) == 1, repr(index_records))
    finally:
        cleanup_workspace(temp_workspace)


def t_query_uses_title_and_intents_when_task_is_unknown():
    temp_workspace = make_workspace()
    try:
        plan_history_module.save_plan(temp_workspace, INDEX_PLAN, task_text="", status="done", run_id="run-index")
        matches = plan_history_module.find_similar(temp_workspace, NEW_INDEX_TASK)
        check("plan saved without a task still matches by title and intent",
              len(matches) == 1 and matches[0][0]["title"] == "Recipe index", repr(matches))
    finally:
        cleanup_workspace(temp_workspace)


if __name__ == "__main__":
    for test_function in [t_no_saved_plans_returns_empty,
                          t_missing_workspace_returns_empty,
                          t_matching_plan_ranks_first_above_threshold,
                          t_nonmatching_plan_is_not_returned,
                          t_save_writes_feature_file_and_index_entry,
                          t_unparseable_plan_is_not_saved,
                          t_deduplication_saves_second_and_removes_old,
                          t_cap_fifty_drops_oldest,
                          t_corrupt_index_is_treated_as_empty,
                          t_query_uses_title_and_intents_when_task_is_unknown]:
        test_function()
    print("\nAll plan history tests passed.")
