#!/usr/bin/env python3
"""Tests for the duplicate-deliverable warning (issue #49, plan-time gap).

Field failure, daily persona runs 2026-10-10 and 2026-10-19: Marcus
Bennett's parts-order plan split compile and write across two steps
that both target parts-order-list.txt. Nothing at plan time flagged
that two steps target the same deliverable, or that the later step
adds no work the earlier step does not already claim. On 2026-10-10
the redundant final write step looped reading the nested step
record to error_limit and failed a run whose deliverable was correct.
On 2026-10-19 the same shape did worse: step 4 copied the step
record's own text into the deliverable, overwriting the correct list
with an 877-byte record dump that omits the keybed, and the run
still reported done.

The truncation facet (PR #52) and the duplicate-success error
budget facet (PR #60) are fixed already. What is still missing is
the plan-time flag: a planlint WARN, duplicate_deliverable, when a
later step produces a deliverable file an earlier step already
produced, so the redundancy is visible at approval. Warnings never
block and never trigger a planner retry; a second step that
genuinely extends the same file (draft, then append) is legitimate,
and the warning only asks the approver to check that the later step
adds work instead of copying the earlier step's output back through
the step record.

Run: python3 tests/test_issue49_duplicate_deliverable.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness import gherkin, planlint, planner

# Daily run 2026-10-19, Marcus Bennett: steps 3 and 4 both target
# parts-order-list.txt with the identical Then. Step 3 compiled and
# wrote the list; step 4 read the step-03 record and overwrote the
# file with the record's own text.
OCT19_TASK = ("Make a parts order list for my current repair jobs: "
              "what I need to order, based on what each job needs "
              "and what is already in stock. Write it in a file I "
              "can keep, and do not change the original records.")

OCT19_PLAN = """Feature: Parts Order List Generation
  Scenario: Step 1 - Read repair job records
    Intent: know what each repair job needs
    When I read "repair-mackie-mixer.txt" and "repair-yamaha-keyboard.txt"
    Then the output shows the content of the two repair files
  Scenario: Step 2 - Read parts inventory
    Intent: know what is already in stock
    When I read "parts-inventory.csv"
    Then the output shows the content of the parts inventory
  Scenario: Step 3 - Compile the parts order list
    Intent: work out what to order from the job needs and the stock
    When I compile the parts needed for the repairs and write the parts order list to parts-order-list.txt
    Then "parts-order-list.txt" contains the calculated list of parts to order
  Scenario: Step 4 - Write the parts order list
    Intent: write the compiled parts order list to the file to keep
    When I write the compiled parts order list to parts-order-list.txt
    Then "parts-order-list.txt" contains the calculated list of parts to order
"""

# Daily run 2026-10-10, Marcus Bennett: step 4 compiled the list
# (contains Then) and step 5 wrote it (exists Then), same file.
OCT10_PLAN = """Feature: Parts Order List Generation
  Scenario: Step 1 - List the files
    Intent: know which files hold the jobs and the stock
    When I list the files in the workspace
    Then the output lists all files in the workspace
  Scenario: Step 2 - Read repair job records
    Intent: know what each repair job needs
    When I read "repair-mackie-mixer.txt" and "repair-yamaha-keyboard.txt"
    Then the output shows the content of the two repair files
  Scenario: Step 3 - Read parts inventory
    Intent: know what is already in stock
    When I read "parts-inventory.csv"
    Then the output shows the content of the parts inventory
  Scenario: Step 4 - Compile the parts order list
    Intent: work out what to order from the job needs and the stock
    When I compile the parts needed for the repairs and write the parts order list to parts-order-list.txt
    Then "parts-order-list.txt" contains the calculated parts needed for repairs
  Scenario: Step 5 - Write the parts order list
    Intent: write the compiled parts order list to the file to keep
    When I write the compiled parts order list to parts-order-list.txt
    Then "parts-order-list.txt" exists
"""


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name
          + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        raise AssertionError(name)


def parse(text):
    plan, err = gherkin.parse_feature(text)
    assert plan is not None, err
    return planner.to_plan_steps(plan)


def duplicates_for(task, plan_text):
    findings = planlint.lint_plan(task, parse(plan_text))
    return [f for f in findings if f.code == "duplicate_deliverable"]


def t_oct19_identical_thens_warn_on_step_4():
    ws = duplicates_for(OCT19_TASK, OCT19_PLAN)
    check("exactly one duplicate-deliverable warning", len(ws) == 1,
          str([(f.step_id, f.detail) for f in ws]))
    check("the warning is on the later step", ws[0].step_id == 4,
          str(ws[0].step_id))
    check("the warning is warn level, never an error",
          ws[0].level == "warn", ws[0].level)
    check("the warning names the earlier step",
          "step 3" in ws[0].detail, ws[0].detail)
    check("the warning names the deliverable",
          "parts-order-list.txt" in ws[0].detail, ws[0].detail)


def t_oct10_compile_then_write_warns_on_step_5():
    ws = duplicates_for(OCT19_TASK, OCT10_PLAN)
    check("compile-then-write warns on step 5",
          len(ws) == 1 and ws[0].step_id == 5,
          str([(f.step_id, f.detail) for f in ws]))


def t_distinct_deliverables_stay_clean():
    plan = """Feature: Two outputs
  Scenario: Step 1 - Write the draft
    When I write the draft to draft.txt
    Then "draft.txt" exists
  Scenario: Step 2 - Write the final
    When I compile the final version and write it to final.txt
    Then "final.txt" contains the final version
"""
    ws = duplicates_for("Write a draft and a final version.", plan)
    check("distinct deliverables do not warn", ws == [],
          str([(f.step_id, f.detail) for f in ws]))


def t_check_only_later_step_does_not_warn():
    plan = """Feature: Write then check
  Scenario: Step 1 - Write the list
    When I compile the list and write it to order-list.txt
    Then "order-list.txt" contains the list
  Scenario: Step 2 - Check the list
    When I check that order-list.txt lists every part to order
    Then "order-list.txt" exists
"""
    ws = duplicates_for("Write a list and check it.", plan)
    check("a later step that only checks does not warn", ws == [],
          str([(f.step_id, f.detail) for f in ws]))


def t_three_producing_steps_warn_on_each_later_step():
    plan = """Feature: Three writes, one file
  Scenario: Step 1 - Write the list
    When I write the list to order-list.txt
    Then "order-list.txt" exists
  Scenario: Step 2 - Rewrite the list
    When I compile the list again and write it to order-list.txt
    Then "order-list.txt" contains the list
  Scenario: Step 3 - Rewrite once more
    When I write the final list to order-list.txt
    Then "order-list.txt" contains the final list
"""
    ws = duplicates_for("Write the list three times.", plan)
    check("three producing steps warn on steps 2 and 3",
          [f.step_id for f in ws] == [2, 3],
          str([(f.step_id, f.detail) for f in ws]))


def t_single_producing_step_does_not_warn():
    plan = """Feature: One write
  Scenario: Step 1 - Write the note
    When I write the note to note.txt
    Then "note.txt" contains the note
"""
    ws = duplicates_for("Write a note in a file.", plan)
    check("a lone producing step does not warn", ws == [],
          str([(f.step_id, f.detail) for f in ws]))


def main():
    t_oct19_identical_thens_warn_on_step_4()
    t_oct10_compile_then_write_warns_on_step_5()
    t_distinct_deliverables_stay_clean()
    t_check_only_later_step_does_not_warn()
    t_three_producing_steps_warn_on_each_later_step()
    t_single_producing_step_does_not_warn()
    print("\nAll duplicate-deliverable warning tests passed.")


if __name__ == "__main__":
    main()
