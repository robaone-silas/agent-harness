#!/usr/bin/env python3
"""Tests for Then preservation in to_plan_steps (issue #7).

Field failure, 2026-10-03 persona testing: Priya Nair's index plan put six
Then clauses on its compile step, 349 characters combined. to_plan_steps
truncated the joined done_when at 300 characters, ending mid-filename
("audit-). The saved Gherkin was intact, but the truncated internal
representation is what approval badges, plan lint, executor prompts, and
verification consume, so later clauses could be corrupted or lost without
anyone seeing it happen. Conversion from a parsed plan to PlanStep must
preserve every Then clause exactly.

Run: python3 tests/test_done_when.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness import gherkin, planner, verify

PRIYA_THENS = [
    '"audit-index.md" contains an entry for "alpha-policy.txt"',
    '"audit-index.md" contains an entry for "beta-invoice.csv"',
    '"audit-index.md" contains an entry for "delta-meeting.txt"',
    '"audit-index.md" contains an entry for "epsilon-reading.md"',
    '"audit-index.md" contains an entry for "gamma-recipe.txt"',
    '"audit-index.md" contains an entry for "zeta-travel.txt"',
]

PRIYA_PLAN = """Feature: Create file audit index
  Scenario: Step 1 - List all files
    Intent: know exactly which files must be included in the audit index
    When I list the files in the workspace
    Then the output lists all files in the workspace
  Scenario: Step 2 - Create the audit index
    Intent: create audit-index.md listing every file with its exact filename and a one-line description of what it actually contains, without omitting any file
    When I read each file from step 1 and write audit-index.md with the filename and a one-line description of its content
""" + "".join(f"    Then {t}\n" for t in PRIYA_THENS)

# A step whose Thens are all machine-checkable and total over 300 chars:
# none of them may be lost or damaged by conversion.
CHECKABLE_THENS = [
    '"report.md" contains "quarterly revenue summary"',
    '"report.md" contains " regional breakdown table"',
    '"report.md" contains "headcount by department"',
    '"report.md" contains "capital expenditure notes"',
    '"report.md" contains "risk register highlights"',
    '"report.md" contains "appendix of source figures"',
]

CHECKABLE_PLAN = """Feature: Assemble the quarterly report
  Scenario: Step 1 - Gather the figures
    When I read the finance files in the workspace
    Then the output lists the finance files
  Scenario: Step 2 - Write the report
    When I write report.md from the gathered figures
""" + "".join(f"    Then {t}\n" for t in CHECKABLE_THENS)


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        raise AssertionError(name)


def t_priya_clauses_survive_conversion():
    plan, err = gherkin.parse_feature(PRIYA_PLAN)
    check("priya plan parses", plan is not None and err == "", err)
    steps = planner.to_plan_steps(plan)
    joined = "\n".join(PRIYA_THENS)
    check("fixture really exceeds 300 chars", len(joined) > 300, str(len(joined)))
    check("done_when preserves the joined Thens exactly",
          steps[1].done_when == joined,
          f"got {len(steps[1].done_when)} chars ending {steps[1].done_when[-30:]!r}")
    parts = [t.strip() for t in steps[1].done_when.split("\n") if t.strip()]
    check("splitting done_when returns all six clauses", parts == PRIYA_THENS,
          f"got {len(parts)} clauses")
    check("the sixth clause survives whole",
          steps[1].done_when.endswith('"zeta-travel.txt"'),
          steps[1].done_when[-30:])


def t_checkable_clauses_still_parse_after_conversion():
    plan, err = gherkin.parse_feature(CHECKABLE_PLAN)
    check("checkable plan parses", plan is not None and err == "", err)
    steps = planner.to_plan_steps(plan)
    parts = [t.strip() for t in steps[1].done_when.split("\n") if t.strip()]
    check("all checkable clauses survive", parts == CHECKABLE_THENS,
          f"got {len(parts)} of {len(CHECKABLE_THENS)}")
    for t in CHECKABLE_THENS:
        check(f"clause still machine-checkable: {t[:40]}...",
              verify.parse_then(t) is not None and verify.parse_then(t)[0] == "contains",
              str(verify.parse_then(t)))
    for t in parts:
        check(f"converted clause still parses: {t[:40]}...",
              verify.parse_then(t) is not None,
              f"{t!r} -> {verify.parse_then(t)}")


def main():
    t_priya_clauses_survive_conversion()
    t_checkable_clauses_still_parse_after_conversion()
    print("\nAll done_when preservation tests passed.")


if __name__ == "__main__":
    main()
