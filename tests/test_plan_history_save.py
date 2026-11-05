#!/usr/bin/env python3
"""Tests for saving only fully verified runs to plan history.

The gate lives in plan_history.save_successful_plan, which run.py
calls after a run finishes. Only a final status of `done` saves;
`done_on_trust`, failed, and unparseable runs save nothing, and a
history write failure never raises, so a bookkeeping failure can
never fail a successful run.

Run: python3 tests/test_plan_history_save.py
"""
import logging
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import run as run_cli
from harness import gherkin as gherkin_module
from harness import plan_history as plan_history_module
from harness.plan_model import PlanStep

INDEX_PLAN = """Feature: Recipe index
  Scenario: Step 1 - List the recipes
    When I list the files in the folder
    Then the output lists the recipe files
  Scenario: Step 2 - Write the index
    When I write recipe-index.md indexing the listed recipes
    Then "recipe-index.md" indexes the files from step 1
"""


def check(name, condition, detail=""):
    print(("PASS " if condition else "FAIL ") + name + (f" -- {detail}" if detail and not condition else ""))
    if not condition:
        raise AssertionError(name)


def history_index(temp_workspace):
    index_path = Path(temp_workspace) / ".harness" / "plans" / "index.json"
    return index_path.exists()


def t_done_run_saves():
    temp_workspace = tempfile.mkdtemp(prefix="plan-history-save-")
    try:
        record = plan_history_module.save_successful_plan(
            temp_workspace, INDEX_PLAN, task_text="Make an index", status="done", run_id="run-done")
        check("done run saves a record", record is not None, repr(record))
        check("done run writes an index", history_index(temp_workspace))
    finally:
        shutil.rmtree(temp_workspace)


def t_done_on_trust_run_does_not_save():
    temp_workspace = tempfile.mkdtemp(prefix="plan-history-save-")
    try:
        record = plan_history_module.save_successful_plan(
            temp_workspace, INDEX_PLAN, task_text="Make an index", status="done_on_trust", run_id="run-trust")
        check("done_on_trust run saves nothing", record is None, repr(record))
        check("done_on_trust run writes no index", not history_index(temp_workspace))
    finally:
        shutil.rmtree(temp_workspace)


def t_failed_run_does_not_save():
    temp_workspace = tempfile.mkdtemp(prefix="plan-history-save-")
    try:
        record = plan_history_module.save_successful_plan(
            temp_workspace, INDEX_PLAN, task_text="Make an index", status="step_failed", run_id="run-failed")
        check("failed run saves nothing", record is None, repr(record))
        check("failed run writes no index", not history_index(temp_workspace))
    finally:
        shutil.rmtree(temp_workspace)


def t_history_write_failure_never_raises():
    temp_workspace = tempfile.mkdtemp(prefix="plan-history-save-")
    original_save = plan_history_module.save_plan
    try:
        def failing_save(*args, **kwargs):
            raise OSError("history disk is full")

        plan_history_module.save_plan = failing_save
        record = plan_history_module.save_successful_plan(
            temp_workspace, INDEX_PLAN, task_text="Make an index", status="done", run_id="run-done")
        check("history write failure returns no record instead of raising",
              record is None, repr(record))
    finally:
        plan_history_module.save_plan = original_save
        shutil.rmtree(temp_workspace)


def t_save_successful_plan_passes_timestamp():
    temp_workspace = tempfile.mkdtemp(prefix="plan-history-save-")
    try:
        given_now = datetime(2026, 11, 5, 2, 30, 0)
        record = plan_history_module.save_successful_plan(
            temp_workspace, INDEX_PLAN, task_text="Make an index", status="done",
            run_id="run-stamp", now=given_now)
        check("save_successful_plan with a given timestamp returns a record",
              record is not None, repr(record))
        check("save_successful_plan passes the timestamp through",
              record is not None and record["saved_at"] == "2026-11-05T02:30:00", repr(record))
    finally:
        shutil.rmtree(temp_workspace)


def t_show_past_plan_logs_lookup_failure():
    captured_records = []

    class CapturingHandler(logging.Handler):
        def emit(self, captured_record):
            captured_records.append(captured_record)

    capturing_handler = CapturingHandler()
    run_logger = logging.getLogger("run")
    previous_level = run_logger.level
    run_logger.setLevel(logging.DEBUG)
    run_logger.addHandler(capturing_handler)
    original_best_match = plan_history_module.best_match
    try:
        def failing_best_match(*args, **kwargs):
            raise RuntimeError("history lookup is down")

        plan_history_module.best_match = failing_best_match
        fake_config = SimpleNamespace(workspace="/tmp/does-not-matter")
        run_cli._show_past_plan("Make an index", fake_config)
        check("past plan lookup failure does not raise", True)
        check("past plan lookup failure is logged at debug",
              len(captured_records) == 1 and captured_records[0].levelno == logging.DEBUG,
              repr(captured_records))
        check("past plan lookup failure log carries the exception",
              len(captured_records) == 1 and captured_records[0].exc_info is not None
              and captured_records[0].exc_info[0] is RuntimeError, repr(captured_records))
    finally:
        plan_history_module.best_match = original_best_match
        run_logger.removeHandler(capturing_handler)
        run_logger.setLevel(previous_level)


def t_auto_reconstruction_preserves_steps():
    executed_steps = [
        PlanStep(id=1, instruction="List the files in the folder",
                 done_when="the output lists the recipe files",
                 title="List the recipes", intent="know exactly which recipes the index must cover"),
        PlanStep(id=2, instruction="Write recipe-index.md indexing the listed recipes",
                 done_when='"recipe-index.md" indexes the files from step 1',
                 title="Write the index"),
    ]
    rendered_text = run_cli._render_executed_plan("Make an index of the recipes", executed_steps)
    parsed_plan, parse_error = gherkin_module.parse_feature(rendered_text)
    check("auto reconstruction parses as valid Gherkin", parsed_plan is not None, parse_error)
    if parsed_plan is None:
        return
    check("auto reconstruction keeps both steps", len(parsed_plan.steps) == 2)
    check("auto reconstruction keeps the step intent",
          parsed_plan.steps[0].intent == "know exactly which recipes the index must cover")
    check("auto reconstruction keeps the indexes Then",
          '"recipe-index.md" indexes the files from step 1' in parsed_plan.steps[1].then)


def t_auto_reconstruction_uses_first_line_of_multiline_task():
    executed_steps = [
        PlanStep(id=1, instruction="List the files in the folder",
                 done_when="the output lists the recipe files",
                 title="List the recipes", intent="know exactly which recipes the index must cover"),
        PlanStep(id=2, instruction="Write recipe-index.md indexing the listed recipes",
                 done_when='"recipe-index.md" indexes the files from step 1',
                 title="Write the index"),
    ]
    rendered_text = run_cli._render_executed_plan(
        "Make an index of the recipes\nfrom the folder\nfor the team", executed_steps)
    parsed_plan, parse_error = gherkin_module.parse_feature(rendered_text)
    check("multiline task reconstruction parses as valid Gherkin",
          parsed_plan is not None, parse_error)
    check("multiline task reconstruction uses the first line as the title",
          parsed_plan is not None and parsed_plan.title == "Make an index of the recipes",
          parsed_plan.title if parsed_plan is not None else parse_error)


if __name__ == "__main__":
    for test_function in [t_done_run_saves,
                          t_done_on_trust_run_does_not_save,
                          t_failed_run_does_not_save,
                          t_history_write_failure_never_raises,
                          t_save_successful_plan_passes_timestamp,
                          t_show_past_plan_logs_lookup_failure,
                          t_auto_reconstruction_preserves_steps,
                          t_auto_reconstruction_uses_first_line_of_multiline_task]:
        test_function()
    print("\nAll plan history save tests passed.")
