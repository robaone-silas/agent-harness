#!/usr/bin/env python3
"""Tests for the plan-time narrowing lint check (issue #37 Slice 2).

Field failure:
On 2026-10-18 and 2026-11-02, Tomas Rivera's oldest appliance runs
dropped the fridge (2018) and washer (2021) years because the plan itself
narrowed the deliverable. In the 2026-10-18 shape, Step 3's Intent promised
"with the year for each appliance", but When and Then required only
"the oldest appliance and its corresponding year". In the 2026-11-02 shape,
the task and Step 1 required "the installation year for each appliance", but
Step 3 required only "the dryer and the year 2016".

The narrow plan lint check flags when a deliverable step drops a per-item
requirement from the task or Intent in favor of a singular extremum.

Run: python3 tests/test_plan_narrowing_lint.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness import gherkin, planlint, planner

TOMAS_TASK = (
    "Which of my appliances is the oldest? Write the answer in a file for me, "
    "with the year for each appliance."
)

TOMAS_PLAN_OCT18_NARROWED = """Feature: Oldest appliance
  Scenario: Step 1 - Read the appliance records
    Intent: know the year of each appliance
    When I read "dryer-manual.txt", "fridge-receipt.txt", and "washer-receipt.txt"
    Then the output shows the year for each appliance
  Scenario: Step 2 - Find the oldest
    Intent: determine which appliance is the oldest
    When I compare the years from step 1
    Then I know the oldest appliance
  Scenario: Step 3 - Write the answer
    Intent: write the answer in a file for the user, with the year for each appliance
    When I write the oldest appliance and its year to oldest-appliance.txt
    Then "oldest-appliance.txt" contains the oldest appliance and its corresponding year
"""

TOMAS_PLAN_NOV02_NARROWED = """Feature: Appliance age determination
  Scenario: Step 1 - Extract installation years
    Intent: read the installation or purchase year for each appliance
    When I read "dryer-manual.txt", "fridge-receipt.txt", and "washer-receipt.txt"
    Then the output shows the installation year for each appliance
  Scenario: Step 2 - Determine the oldest appliance
    Intent: identify the appliance with the minimum installation year
    When I compare the installation years from step 1
    Then the output identifies the dryer as the oldest appliance
  Scenario: Step 3 - Write the result
    Intent: write the oldest appliance and its year to a file
    When I write "oldest-appliance.txt" with the oldest appliance and its year
    Then "oldest-appliance.txt" contains the dryer and the year 2016
"""

TOMAS_PLAN_FULL_REQUIREMENT = """Feature: Oldest appliance
  Scenario: Step 1 - Read the appliance records
    Intent: know the year of each appliance
    When I read "dryer-manual.txt", "fridge-receipt.txt", and "washer-receipt.txt"
    Then the output shows the year for each appliance
  Scenario: Step 2 - Find the oldest
    Intent: determine which appliance is the oldest
    When I compare the years from step 1
    Then I know the oldest appliance
  Scenario: Step 3 - Write the answer
    Intent: write the answer in a file for the user, with the year for each appliance
    When I write "oldest-appliance.txt" naming the oldest appliance and listing the year for each appliance from step 1
    Then "oldest-appliance.txt" contains the oldest appliance and the year for each appliance
"""


def check(name, condition, detail=""):
    print(("PASS " if condition else "FAIL ") + name
          + (f" -- {detail}" if detail and not condition else ""))
    if not condition:
        raise AssertionError(name)


def parse(text):
    plan, error = gherkin.parse_feature(text)
    assert plan is not None, error
    return planner.to_plan_steps(plan)


def narrowing_warnings_for(task, plan_text):
    findings = planlint.lint_plan(task, parse(plan_text))
    return [finding for finding in findings if finding.code == "plan_narrowing"]


def t_tomas_oct18_narrowed_intent_warns():
    warnings = narrowing_warnings_for(TOMAS_TASK, TOMAS_PLAN_OCT18_NARROWED)
    check("oct18 plan produces narrowing warning", len(warnings) == 1,
          str([(finding.step_id, finding.detail) for finding in warnings]))
    check("warning is on step 3", warnings[0].step_id == 3, str(warnings[0].step_id))
    check("warning is warn level", warnings[0].level == "warn", warnings[0].level)
    check("warning names intent and dropped requirement",
          "intent" in warnings[0].detail.lower() and "narrow" in warnings[0].detail.lower(),
          warnings[0].detail)
    check("warning detail matches replication regex",
          "each appliance" in warnings[0].detail.lower() or "year" in warnings[0].detail.lower(),
          warnings[0].detail)


def t_tomas_nov02_narrowed_task_warns():
    warnings = narrowing_warnings_for(TOMAS_TASK, TOMAS_PLAN_NOV02_NARROWED)
    check("nov02 plan produces narrowing warning", len(warnings) == 1,
          str([(finding.step_id, finding.detail) for finding in warnings]))
    check("nov02 warning is on step 3", warnings[0].step_id == 3, str(warnings[0].step_id))
    check("nov02 warning mentions narrowing", "narrow" in warnings[0].detail.lower(),
          warnings[0].detail)


def t_tomas_full_requirement_clean():
    warnings = narrowing_warnings_for(TOMAS_TASK, TOMAS_PLAN_FULL_REQUIREMENT)
    check("full requirement plan has no narrowing warning", warnings == [],
          str([(finding.step_id, finding.detail) for finding in warnings]))


def t_single_selection_task_clean():
    single_task = "Which of my appliances is the oldest? Write the answer to oldest.txt."
    single_plan = """Feature: Oldest appliance
  Scenario: Step 1 - Read records
    When I read "dryer-manual.txt" and "fridge-receipt.txt"
    Then the output shows the appliance years
  Scenario: Step 2 - Write oldest
    When I write the oldest appliance to oldest.txt
    Then "oldest.txt" contains the oldest appliance
"""
    warnings = narrowing_warnings_for(single_task, single_plan)
    check("task without each-item requirement does not warn", warnings == [],
          str([(finding.step_id, finding.detail) for finding in warnings]))


def t_preserves_clause_clean():
    preserves_plan = """Feature: Appliance records
  Scenario: Step 1 - Read records
    Intent: know the year of each appliance
    When I read "dryer-manual.txt" and "fridge-receipt.txt"
    Then the output shows the year for each appliance
  Scenario: Step 2 - Write records
    When I write "appliance-summary.txt" with the oldest appliance
    Then "appliance-summary.txt" preserves year from step 1
"""
    warnings = narrowing_warnings_for(TOMAS_TASK, preserves_plan)
    check("preserves clause prevents narrowing warning", warnings == [],
          str([(finding.step_id, finding.detail) for finding in warnings]))


def main():
    t_tomas_oct18_narrowed_intent_warns()
    t_tomas_nov02_narrowed_task_warns()
    t_tomas_full_requirement_clean()
    t_single_selection_task_clean()
    t_preserves_clause_clean()
    print("\nAll plan narrowing warning tests passed.")


if __name__ == "__main__":
    main()
