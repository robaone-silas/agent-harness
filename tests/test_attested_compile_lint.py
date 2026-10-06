#!/usr/bin/env python3
"""Tests for the attested-compile warning (issue #37, warning first).

Field failure, daily persona run 2026-10-06: Marcus Bennett's plan
read three repair records in steps 1 and 2, then step 3 compiled
them into work_list.txt under a single generic Then,
`"work_list.txt" contains the compiled work list`, which matches no
verifier and is taken on trust. The compilation dropped every
customer and every status the task explicitly required, the run
reported done, and nothing anywhere flagged that the deliverable's
completeness rested entirely on attestation. The existence floor
(issue #22) passed because the file exists; covers/indexes could
not apply because there is no listing step and filename coverage
cannot express per-record field preservation anyway.

The warning solution, implemented first per the maintainer's
direction: a planlint WARN, attested_compile, when a step writes a
deliverable out of what earlier read steps gathered and every Then
clause guarding that deliverable is attested, so content
completeness resting entirely on trust is visible at approval.
Warnings never block and never trigger a planner retry; the
stronger field-preservation check is a separate, later decision.

Run: python3 tests/test_attested_compile_lint.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness import gherkin, planlint, planner

MARCUS_TASK = ("Make a work list for my business: for each repair "
               "job, the customer, the item, and its status. Also "
               "list the music gear that needs attention before "
               "Saturday. Write it in a file I can keep, and do not "
               "change the original records.")

MARCUS_PLAN = """Feature: Business Work List Generation
  Scenario: Step 1 - Extract repair job details
    Intent: gather the customer, item, and status for each repair job
    When I read "repair-fender-amp.txt", "repair-mackie-mixer.txt", and "repair-yamaha-keyboard.txt"
    Then the output shows the content of the three repair files
  Scenario: Step 2 - Extract music gear attention list
    Intent: gather the music gear that needs attention before Saturday
    When I read "gig-gear-notes.txt"
    Then the output shows the content of the gig gear notes
  Scenario: Step 3 - Write the final work list
    Intent: write a single file containing all repair job details and music gear needing attention
    When I compile the repair job details and music gear notes and write the work list to work_list.txt
    Then "work_list.txt" contains the compiled work list
"""

TOMAS_TASK = "Make a list of my house papers and what each one is for."

TOMAS_PLAN = """Feature: House paper inventory
  Scenario: Step 1 - Read house insurance policy
    Intent: know the details of the house insurance policy for the inventory
    When I read "house-insurance-policy.txt"
    Then I know the details contained within the house insurance policy
  Scenario: Step 2 - Read property tax bill
    Intent: know the details of the property tax bill for the inventory
    When I read "property-tax-bill.txt"
    Then I know the details contained within the property tax bill
  Scenario: Step 3 - Read roof warranty
    Intent: know the details of the roof warranty for the inventory
    When I read "roof-warranty.txt"
    Then I know the details contained within the roof warranty
  Scenario: Step 4 - Read furnace receipt
    Intent: know the details of the furnace receipt for the inventory
    When I read "furnace-receipt.txt"
    Then I know the details contained within the furnace receipt
  Scenario: Step 5 - Read water heater manual
    Intent: know the details of the water heater manual for the inventory
    When I read "water-heater-manual.txt"
    Then I know the details contained within the water heater manual
  Scenario: Step 6 - Compile the house paper list
    Intent: make a list of my house papers and what each one is for
    When I compile a list of all house papers and their purposes from steps 1 through 5 and write the result to house_papers_list.txt
    Then "house_papers_list.txt" contains a list of all house papers and their purposes
"""

PRIYA_PLAN = """Feature: Create file audit index
  Scenario: Step 1 - List all files
    Intent: know exactly which files must be included in the audit index
    When I list the files in the workspace
    Then the output lists all files in the workspace
  Scenario: Step 2 - Create the audit index
    Intent: create audit-index.md listing every file with its exact filename and one-line description
    When I read each file listed in step 1 and write audit-index.md with each filename and a one-line description
    Then "audit-index.md" indexes the files from step 1
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


def warnings_for(task, plan_text):
    findings = planlint.lint_plan(task, parse(plan_text))
    return [f for f in findings if f.code == "attested_compile"]


def t_marcus_plan_warns_on_the_compile_step():
    ws = warnings_for(MARCUS_TASK, MARCUS_PLAN)
    check("exactly one attested-compile warning", len(ws) == 1,
          str([(f.step_id, f.detail) for f in ws]))
    check("the warning is on the compile step", ws[0].step_id == 3,
          str(ws[0].step_id))
    check("the warning is warn level, never an error",
          ws[0].level == "warn", ws[0].level)
    check("the warning names the deliverable",
          "work_list.txt" in ws[0].detail, ws[0].detail)


def t_tomas_plan_warns_on_the_compile_step():
    ws = warnings_for(TOMAS_TASK, TOMAS_PLAN)
    check("tomas compile step warns", len(ws) == 1
          and ws[0].step_id == 6,
          str([(f.step_id, f.code) for f in ws]))


def t_machine_checked_compile_does_not_warn():
    ws = warnings_for("Create an audit index of the files.",
                      PRIYA_PLAN)
    check("indexes-guarded compile does not warn", ws == [],
          str([(f.step_id, f.detail) for f in ws]))
    checked = MARCUS_PLAN.replace(
        'Then "work_list.txt" contains the compiled work list',
        'Then "work_list.txt" contains "Repair Job Details"')
    ws = warnings_for(MARCUS_TASK, checked)
    check("a content-bearing clause on the deliverable silences it",
          ws == [], str([(f.step_id, f.detail) for f in ws]))


def t_no_earlier_reads_no_warning():
    single = """Feature: Write a note
  Scenario: Step 1 - Write the note
    When I write the note to note.txt
    Then "note.txt" contains the note
"""
    ws = warnings_for("Write a note in a file.", single)
    check("a lone write step does not warn", ws == [],
          str([(f.step_id, f.detail) for f in ws]))
    no_reads = """Feature: Two writes
  Scenario: Step 1 - Write the draft
    When I write the draft to draft.txt
    Then "draft.txt" exists
  Scenario: Step 2 - Write the final
    When I compile the final version and write it to final.txt
    Then "final.txt" contains the final version
"""
    ws = warnings_for("Write the final version in a file.", no_reads)
    check("no earlier read steps, no warning", ws == [],
          str([(f.step_id, f.detail) for f in ws]))


def main():
    t_marcus_plan_warns_on_the_compile_step()
    t_tomas_plan_warns_on_the_compile_step()
    t_machine_checked_compile_does_not_warn()
    t_no_earlier_reads_no_warning()
    print("\nAll attested-compile warning tests passed.")


if __name__ == "__main__":
    main()
