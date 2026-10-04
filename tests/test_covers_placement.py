"""Unit tests for the covers placement lint rule.

Issue #17 part 3 (2026-10-04): Priya Nair's final-replay plan carried its
covers clause on the read step (`"audit-index.md" covers the files from
step 1` on the step that only reads and prepares entries), while the
later writing step carried a prose Then that matched no verifier and
completed on attestation. The completeness check therefore ran before
the deliverable existed in its final form. The rule: a retryable ERROR
when a covers clause sits on a step whose When only reads or gathers
(no write, create, or run verb producing the target) while a later step
writes the same target file. Feedback: move the covers clause to the
step that writes the deliverable.

Run: python3 tests/test_covers_placement.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness import planlint, gherkin
from harness.planner import PlanStep, request_plan, to_plan_steps
from harness.config import Config

PRIYA_TASK = (
    "Read every file in this workspace, then create audit-index.md listing "
    "every file with its exact filename and a one-line description of what "
    "it actually contains. Do not omit any file."
)

# Priya Nair's final-replay plan, verbatim (user-testing run
# 2026-10-04-final-replay, results.json plan output): covers on the
# read step, prose Then on the write step.
PRIYA_FINAL_PLAN = """Feature: File Audit Index
  Scenario: Step 1 - List all files
    Intent: know exactly which files the audit index must cover
    When I list the files in the folder
    Then the output lists all files in the workspace
  Scenario: Step 2 - Read file contents for indexing
    Intent: create a one-line description of what each file contains
    When I read the content of every file from step 1 and prepare entries for audit-index.md
    Then "audit-index.md" covers the files from step 1
  Scenario: Step 3 - Write the audit index
    Intent: create audit-index.md listing every file with its exact filename and a one-line description of what it actually contains
    When I write audit-index.md with the gathered file information
    Then "audit-index.md" contains entries for all files listed in step 1
"""

# The same plan with the covers clause moved to the writing step.
FIXED_PLAN = """Feature: File Audit Index
  Scenario: Step 1 - List all files
    Intent: know exactly which files the audit index must cover
    When I list the files in the folder
    Then the output lists all files in the workspace
  Scenario: Step 2 - Read file contents for indexing
    Intent: create a one-line description of what each file contains
    When I read the content of every file from step 1 and prepare entries for audit-index.md
    Then "audit-index.md" contains entries for all files listed in step 1
  Scenario: Step 3 - Write the audit index
    Intent: create audit-index.md listing every file with its exact filename and a one-line description of what it actually contains
    When I write audit-index.md with the gathered file information
    Then "audit-index.md" covers the files from step 1
"""

CODE = "covers_on_read_only_step"


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
    plan, err = gherkin.parse_feature(PRIYA_FINAL_PLAN)
    check("priya final plan parses", plan is not None, str(err))
    steps = to_plan_steps(plan)
    fs = planlint.lint_plan(PRIYA_TASK, steps)
    hits = _rule_findings(fs)
    check("rule fires once on Priya final plan", len(hits) == 1,
          f"{[(f.code, f.detail) for f in fs]}")
    check("rule is a retryable error", hits[0].level == "error",
          hits[0].level)
    check("rule is on step 2 (the read step)", hits[0].step_id == 2,
          str(hits[0].step_id))
    check("feedback names the writing step",
          "step 3" in hits[0].detail, hits[0].detail)
    check("feedback says to move the covers clause",
          "Move" in hits[0].detail
          and '"audit-index.md" covers the files from step 1'
          in hits[0].detail,
          hits[0].detail)


def t_fixed_plan_clean():
    plan, err = gherkin.parse_feature(FIXED_PLAN)
    check("fixed plan parses", plan is not None, str(err))
    fs = planlint.lint_plan(PRIYA_TASK, to_plan_steps(plan))
    check("moved covers: rule silent", _rule_findings(fs) == [],
          f"{[(f.code, f.detail) for f in fs]}")
    check("moved covers: no errors",
          [f for f in fs if f.level == "error"] == [],
          f"{[(f.code, f.detail) for f in fs]}")


def t_covers_on_writing_step_no_fire():
    steps = _steps([
        ("I list the files in the workspace", "the output lists the files"),
        ("I read each file and write audit-index.md",
         '"audit-index.md" covers the files from step 1'),
        ("I write audit-index.md again with corrections",
         '"audit-index.md" exists'),
    ])
    fs = planlint.lint_plan(PRIYA_TASK, steps)
    check("covers on a writing step: rule silent",
          _rule_findings(fs) == [],
          f"{[(f.code, f.detail) for f in fs]}")


def t_covers_on_run_step_no_fire():
    steps = _steps([
        ("I list the files in the workspace", "the output lists the files"),
        ("I run the indexer for audit-index.md",
         '"audit-index.md" covers the files from step 1'),
        ("I write audit-index.md with corrections",
         '"audit-index.md" exists'),
    ])
    fs = planlint.lint_plan(PRIYA_TASK, steps)
    check("covers on a run step: rule silent", _rule_findings(fs) == [],
          f"{[(f.code, f.detail) for f in fs]}")


def t_no_later_writer_no_fire():
    steps = _steps([
        ("I list the files in the workspace", "the output lists the files"),
        ("I read the content of every file from step 1 and prepare "
         "entries for audit-index.md",
         '"audit-index.md" covers the files from step 1'),
    ])
    fs = planlint.lint_plan(PRIYA_TASK, steps)
    check("no later writer: rule silent", _rule_findings(fs) == [],
          f"{[(f.code, f.detail) for f in fs]}")


def t_later_step_writes_other_target_no_fire():
    steps = _steps([
        ("I list the files in the workspace", "the output lists the files"),
        ("I read the content of every file from step 1 and prepare "
         "entries for audit-index.md",
         '"audit-index.md" covers the files from step 1'),
        ("I write summary.md with the gathered information",
         '"summary.md" exists'),
    ])
    fs = planlint.lint_plan(PRIYA_TASK, steps)
    check("later writer of another target: rule silent",
          _rule_findings(fs) == [],
          f"{[(f.code, f.detail) for f in fs]}")


def t_later_step_only_reads_target_no_fire():
    steps = _steps([
        ("I list the files in the workspace", "the output lists the files"),
        ("I read the content of every file from step 1 and prepare "
         "entries for audit-index.md",
         '"audit-index.md" covers the files from step 1'),
        ("I read audit-index.md to check it",
         '"audit-index.md" exists'),
    ])
    fs = planlint.lint_plan(PRIYA_TASK, steps)
    check("later step only reads target: rule silent",
          _rule_findings(fs) == [],
          f"{[(f.code, f.detail) for f in fs]}")


def t_read_step_not_naming_target_still_fires():
    steps = _steps([
        ("I list the files in the workspace", "the output lists the files"),
        ("I read the content of every file from step 1",
         '"audit-index.md" covers the files from step 1'),
        ("I write audit-index.md with the gathered information",
         '"audit-index.md" exists'),
    ])
    fs = planlint.lint_plan(PRIYA_TASK, steps)
    hits = _rule_findings(fs)
    check("read step not naming target: rule fires", len(hits) == 1,
          f"{[(f.code, f.detail) for f in fs]}")
    check("finding is on step 2", hits[0].step_id == 2,
          str(hits[0].step_id))


def t_no_covers_clause_no_fire():
    steps = _steps([
        ("I list the files in the workspace", "the output lists the files"),
        ("I read the content of every file from step 1",
         "the entries are prepared"),
        ("I write audit-index.md with the gathered information",
         '"audit-index.md" exists'),
    ])
    fs = planlint.lint_plan(PRIYA_TASK, steps)
    check("no covers clause: rule silent", _rule_findings(fs) == [],
          f"{[(f.code, f.detail) for f in fs]}")


def t_retry_integration():
    calls = []

    def fake_chat(messages, tools):
        calls.append(messages[-1]["content"])
        return {"content": PRIYA_FINAL_PLAN if len(calls) == 1
                else FIXED_PLAN}

    cfg = Config()
    text, steps = request_plan(PRIYA_TASK, [], fake_chat, cfg)
    check("lint retry fires", len(calls) == 2, f"calls={len(calls)}")
    check("retry feedback carries the move instruction",
          '"audit-index.md" covers the files from step 1' in calls[1]
          and "step 3" in calls[1],
          calls[1][:600])
    errs = [f for s in steps for f in s.lint if f["level"] == "error"]
    check("fixed plan has no errors", errs == [], str(errs))


if __name__ == "__main__":
    for fn in [t_priya_fixture_fires, t_fixed_plan_clean,
               t_covers_on_writing_step_no_fire,
               t_covers_on_run_step_no_fire, t_no_later_writer_no_fire,
               t_later_step_writes_other_target_no_fire,
               t_later_step_only_reads_target_no_fire,
               t_read_step_not_naming_target_still_fires,
               t_no_covers_clause_no_fire, t_retry_integration]:
        fn()
    print("\nAll covers-placement tests passed.")
