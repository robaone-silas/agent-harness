"""Unit tests for the per-item-contains instead-of-covers lint rule.

Issue #5 (2026-10-03): Priya Nair's saved plan expressed completeness as
six unmatched per-item clauses (`"audit-index.md" contains an entry for
"<file>"`) after a listing step. None parses as a verifier, lint stayed
silent, and the deliverable's completeness rested entirely on trust.
The rule: a listing step followed by a step carrying at least two
unmatched per-item containment Then clauses against the same target,
with no covers clause for that target, is a retryable ERROR whose
feedback tells the planner to replace the per-item clauses with
`"<target>" covers the files from step N`. (Since the covers-to-
indexes push, 2026-10-04, the fully fixed endpoint for a describe-
each compile like this one is the `indexes` clause: the per-item
rule accepts either, and FIXED_PLAN below uses `indexes`.)

Run: python3 tests/test_covers_lint.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness import planlint, gherkin
from harness.planner import (PlanStep, build_planner_prompt, request_plan,
                             to_plan_steps)
from harness.config import Config

PRIYA_TASK = (
    "Read every file in this workspace, then create audit-index.md listing "
    "every file with its exact filename and a one-line description of what "
    "it actually contains. Do not omit any file."
)

# Priya Nair's saved plan, verbatim (user-testing run 2026-10-03,
# runs/2026-10-03/priya-nair/plan.feature).
PRIYA_PLAN = """Feature: Create file audit index
  Scenario: Step 1 - List all files
    Intent: know exactly which files must be included in the audit index
    When I list the files in the workspace
    Then the output lists all files in the workspace
  Scenario: Step 2 - Create the audit index
    Intent: create audit-index.md listing every file with its exact filename and a one-line description of what it actually contains, without omitting any file
    When I read each file from step 1 and write audit-index.md with the filename and a one-line description of its content
    Then "audit-index.md" contains an entry for "alpha-policy.txt"
    Then "audit-index.md" contains an entry for "beta-invoice.csv"
    Then "audit-index.md" contains an entry for "delta-meeting.txt"
    Then "audit-index.md" contains an entry for "epsilon-reading.md"
    Then "audit-index.md" contains an entry for "gamma-recipe.txt"
    Then "audit-index.md" contains an entry for "zeta-travel.txt"
"""

FIXED_PLAN = """Feature: Create file audit index
  Scenario: Step 1 - List all files
    Intent: know exactly which files must be included in the audit index
    When I list the files in the workspace
    Then the output lists all files in the workspace
  Scenario: Step 2 - Create the audit index
    Intent: create audit-index.md listing every file with its exact filename and a one-line description of what it actually contains, without omitting any file
    When I read each file from step 1 and write audit-index.md with the filename and a one-line description of its content
    Then "audit-index.md" indexes the files from step 1
"""

CODE = "per_item_instead_of_covers"


def _steps(pairs):
    return [PlanStep(id=i + 1, instruction=w, done_when=t)
            for i, (w, t) in enumerate(pairs)]


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name
          + (f" -- {info}" if info and not cond else ""))
    assert cond, f"{name}: {info}"


def _rule_findings(fs):
    return [f for f in fs if f.code == CODE]


def t_priya_fixture_fires():
    plan, err = gherkin.parse_feature(PRIYA_PLAN)
    check("priya plan parses", plan is not None, str(err))
    steps = to_plan_steps(plan)
    fs = planlint.lint_plan(PRIYA_TASK, steps)
    hits = _rule_findings(fs)
    check("rule fires once on Priya plan", len(hits) == 1,
          f"{[(f.code, f.detail) for f in fs]}")
    check("rule is a retryable error", hits[0].level == "error", hits[0].level)
    check("rule is on step 2", hits[0].step_id == 2, str(hits[0].step_id))
    check("feedback names the covers replacement",
          '"audit-index.md" covers the files from step 1' in hits[0].detail,
          hits[0].detail)


def t_priya_fixture_fixed_plan_clean():
    plan, err = gherkin.parse_feature(FIXED_PLAN)
    check("fixed plan parses", plan is not None, str(err))
    fs = planlint.lint_plan(PRIYA_TASK, to_plan_steps(plan))
    check("fixed plan: rule silent", _rule_findings(fs) == [],
          f"{[(f.code, f.detail) for f in fs]}")
    check("fixed plan: no errors",
          [f for f in fs if f.level == "error"] == [],
          f"{[(f.code, f.detail) for f in fs]}")


def t_covers_clause_alongside_suppresses():
    steps = _steps([
        ("I list the files in the workspace", "the output lists the files"),
        ("I write audit-index.md",
         '"audit-index.md" contains an entry for "a.txt"\n'
         '"audit-index.md" contains an entry for "b.txt"\n'
         '"audit-index.md" covers the files from step 1'),
    ])
    fs = planlint.lint_plan(PRIYA_TASK, steps)
    check("covers present: rule silent", _rule_findings(fs) == [],
          f"{[(f.code, f.detail) for f in fs]}")


def t_single_per_item_clause_no_fire():
    steps = _steps([
        ("I list the files in the workspace", "the output lists the files"),
        ("I write audit-index.md",
         '"audit-index.md" contains an entry for "a.txt"'),
    ])
    fs = planlint.lint_plan(PRIYA_TASK, steps)
    check("one per-item clause: rule silent", _rule_findings(fs) == [],
          f"{[(f.code, f.detail) for f in fs]}")


def t_no_listing_step_no_fire():
    steps = _steps([
        ("I read the files I need", "the output lists the files"),
        ("I write audit-index.md",
         '"audit-index.md" contains an entry for "a.txt"\n'
         '"audit-index.md" contains an entry for "b.txt"'),
    ])
    fs = planlint.lint_plan(PRIYA_TASK, steps)
    check("no listing step: rule silent", _rule_findings(fs) == [],
          f"{[(f.code, f.detail) for f in fs]}")


def t_split_targets_no_fire():
    steps = _steps([
        ("I list the files in the workspace", "the output lists the files"),
        ("I write the reports",
         '"a-index.md" contains an entry for "a.txt"\n'
         '"b-index.md" contains an entry for "b.txt"'),
    ])
    fs = planlint.lint_plan(PRIYA_TASK, steps)
    check("different targets: rule silent", _rule_findings(fs) == [],
          f"{[(f.code, f.detail) for f in fs]}")


def t_parseable_contains_no_fire():
    # Parseable per-file contains clauses are machine-checkable already;
    # the rule is only for the unmatched near-miss shape.
    steps = _steps([
        ("I list the files in the workspace", "the output lists the files"),
        ("I write audit-index.md",
         '"audit-index.md" contains "a.txt"\n'
         '"audit-index.md" contains "b.txt"'),
    ])
    fs = planlint.lint_plan(PRIYA_TASK, steps)
    check("parseable contains: rule silent", _rule_findings(fs) == [],
          f"{[(f.code, f.detail) for f in fs]}")


def t_retry_integration():
    calls = []

    def fake_chat(messages, tools):
        calls.append(messages[-1]["content"])
        return {"content": PRIYA_PLAN if len(calls) == 1 else FIXED_PLAN}

    cfg = Config()
    text, steps = request_plan(PRIYA_TASK, [], fake_chat, cfg)
    check("lint retry fires", len(calls) == 2, f"calls={len(calls)}")
    check("retry feedback carries the covers replacement",
          '"audit-index.md" covers the files from step 1' in calls[1],
          calls[1][:400])
    errs = [f for s in steps for f in s.lint if f["level"] == "error"]
    check("fixed plan has no errors", errs == [], str(errs))


def t_prompt_negative_rule():
    prompt = build_planner_prompt(PRIYA_TASK, [])
    check("prompt forbids per-file contains substitution",
          "one `contains` Then per source file" in prompt
          and "covers" in prompt,
          prompt[:200])


if __name__ == "__main__":
    for fn in [t_priya_fixture_fires, t_priya_fixture_fixed_plan_clean,
               t_covers_clause_alongside_suppresses,
               t_single_per_item_clause_no_fire, t_no_listing_step_no_fire,
               t_split_targets_no_fire, t_parseable_contains_no_fire,
               t_retry_integration, t_prompt_negative_rule]:
        fn()
    print("\nAll covers-lint tests passed.")
