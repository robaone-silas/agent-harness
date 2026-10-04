#!/usr/bin/env python3
"""Tests for workspace-aware plan lint warnings (issue #6, step 5).

Field motivation (2026-10-03, Tomas Rivera's persona run): his final
plan read four of the five files in his workspace, named
water-heater-manual.txt nowhere, called the other four "all house
documents", and named no deliverable file at all. Plan lint could not
see either problem: it received the task and the steps, never the
workspace file set, and it had no rule for a missing deliverable.

The fix gives lint optional access to the workspace root file set and
adds two warnings (never errors, so neither triggers a planner retry):

  unaccounted_file    under narrow exhaustive cues (a list, inventory,
                      index, or catalogue task, or all/every/each of
                      the files), a root file mentioned nowhere in the
                      task or plan warns, unless the plan generically
                      lists the workspace or uses a covers clause.
  deliverable_clarity when list, inventory, report, or index language
                      names no output file and declares no answer-only
                      result, the plan warns that the deliverable is
                      ambiguous: a named file or an explicit
                      answer-only result, one of the two.

Tomas's saved plan (user-testing run 2026-10-03,
runs/2026-10-03/tomas-rivera/plan.feature) is the regression fixture:
it must produce exactly one of each warning.

Run: python3 tests/test_workspace_lint.py
"""
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness import gherkin, planlint, planner
from harness.config import Config
from harness.planner import PlanStep, request_plan, to_plan_steps

TOMAS_TASK = "Make a list of my house papers and what each one is for."

TOMAS_FILES = ["furnace-receipt.txt", "house-insurance-policy.txt",
               "property-tax-bill.txt", "roof-warranty.txt",
               "water-heater-manual.txt"]

# Tomas Rivera's saved plan, verbatim.
TOMAS_PLAN = """Feature: House Paper Indexing
  Scenario: Step 1 - Read all house documents
    Intent: know the content of all house papers to create a list of them and their purposes
    When I read `house-insurance-policy.txt` and `property-tax-bill.txt` and `roof-warranty.txt` and `furnace-receipt.txt`
    Then the content of the files is available
  Scenario: Step 2 - Create the list of papers and their purposes
    Intent: make a list of my house papers and what each one is for
    When I compile a list of house papers and their purposes from the files read in step 1
    Then the output is a list of house papers and their purposes
"""

FIXED_PLAN = """Feature: House Paper Indexing
  Scenario: Step 1 - List the house papers
    Intent: know exactly which house papers the list must cover
    When I list the files in the workspace
    Then the output lists the files in the workspace
  Scenario: Step 2 - Write the list
    Intent: make a list of my house papers and what each one is for
    When I read each file from step 1 and write house-papers.md with one entry per paper
    Then "house-papers.md" covers the files from step 1
"""


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name
          + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        raise AssertionError(name)


def _steps(pairs):
    return [PlanStep(id=i + 1, instruction=w, done_when=t)
            for i, (w, t) in enumerate(pairs)]


def _code(fs, code):
    return [f for f in fs if f.code == code]


def t_tomas_fixture_warns_on_both_gaps():
    plan, err = gherkin.parse_feature(TOMAS_PLAN)
    check("tomas plan parses", plan is not None, str(err))
    fs = planlint.lint_plan(TOMAS_TASK, to_plan_steps(plan),
                            workspace_files=TOMAS_FILES)
    un = _code(fs, "unaccounted_file")
    check("unaccounted_file fires once on Tomas plan", len(un) == 1,
          f"{[(f.code, f.detail) for f in fs]}")
    check("unaccounted_file is a warning", un[0].level == "warn", un[0].level)
    check("unaccounted_file names the omitted file",
          "water-heater-manual.txt" in un[0].detail, un[0].detail)
    check("unaccounted_file does not name accounted files",
          all(n not in un[0].detail for n in TOMAS_FILES
              if n != "water-heater-manual.txt"), un[0].detail)
    dl = _code(fs, "deliverable_clarity")
    check("deliverable_clarity fires once on Tomas plan", len(dl) == 1,
          f"{[(f.code, f.detail) for f in fs]}")
    check("deliverable_clarity is a warning", dl[0].level == "warn",
          dl[0].level)
    check("neither new warning is an error",
          all(f.level == "warn" for f in un + dl))


def t_fixed_plan_is_quiet():
    plan, err = gherkin.parse_feature(FIXED_PLAN)
    check("fixed plan parses", plan is not None, str(err))
    fs = planlint.lint_plan(TOMAS_TASK, to_plan_steps(plan),
                            workspace_files=TOMAS_FILES)
    check("listing plus covers plus a named deliverable: no unaccounted",
          _code(fs, "unaccounted_file") == [],
          f"{[(f.code, f.detail) for f in fs]}")
    check("named deliverable: no deliverable_clarity",
          _code(fs, "deliverable_clarity") == [],
          f"{[(f.code, f.detail) for f in fs]}")


def t_no_workspace_files_no_unaccounted():
    plan, err = gherkin.parse_feature(TOMAS_PLAN)
    assert plan is not None, err
    fs = planlint.lint_plan(TOMAS_TASK, to_plan_steps(plan))
    check("without the workspace set, lint stays workspace-blind",
          _code(fs, "unaccounted_file") == [],
          f"{[(f.code, f.detail) for f in fs]}")
    check("deliverable_clarity does not need the workspace set",
          len(_code(fs, "deliverable_clarity")) == 1,
          f"{[(f.code, f.detail) for f in fs]}")


def t_non_exhaustive_task_no_unaccounted():
    steps = _steps([
        ("I read `roof-warranty.txt` and `furnace-receipt.txt`",
         "the contents are known"),
        ("I suggest categories for the papers", "the categories are listed"),
    ])
    task = "Suggest an organization strategy for my house papers by category."
    fs = planlint.lint_plan(task, steps, workspace_files=TOMAS_FILES)
    check("category/strategy task: sampling stays acceptable, no warning",
          _code(fs, "unaccounted_file") == [],
          f"{[(f.code, f.detail) for f in fs]}")


def t_generic_listing_suppresses():
    steps = _steps([
        ("I list the files in the workspace", "the output lists the files"),
        ("I write the list to house-papers.md",
         '"house-papers.md" exists'),
    ])
    fs = planlint.lint_plan(TOMAS_TASK, steps, workspace_files=TOMAS_FILES)
    check("a generic listing step accounts for files it does not name",
          _code(fs, "unaccounted_file") == [],
          f"{[(f.code, f.detail) for f in fs]}")


def t_covers_suppresses():
    steps = _steps([
        ("I list the files in the workspace", "the output lists the files"),
        ("I write house-papers.md from the listing",
         '"house-papers.md" covers the files from step 1'),
    ])
    fs = planlint.lint_plan(TOMAS_TASK, steps, workspace_files=TOMAS_FILES)
    check("a covers clause accounts for the listing's files",
          _code(fs, "unaccounted_file") == [],
          f"{[(f.code, f.detail) for f in fs]}")


def t_mentioned_file_is_accounted():
    steps = _steps([
        ("I read `roof-warranty.txt`", "its content is known"),
        ("I note that water-heater-manual.txt is explicitly excluded",
         "the exclusion is recorded"),
        ("I compile the list to house-papers.md",
         '"house-papers.md" exists'),
    ])
    fs = planlint.lint_plan(TOMAS_TASK, steps,
                            workspace_files=["roof-warranty.txt",
                                             "water-heater-manual.txt"])
    check("named and explicitly excluded files are accounted for",
          _code(fs, "unaccounted_file") == [],
          f"{[(f.code, f.detail) for f in fs]}")


def t_each_unaccounted_file_warns():
    steps = _steps([
        ("I read `a.txt`", "its content is known"),
        ("I compile the list", "the output is the list"),
    ])
    fs = planlint.lint_plan("List every file in this folder", steps,
                            workspace_files=["a.txt", "b.txt", "c.txt"])
    un = _code(fs, "unaccounted_file")
    check("one warning per unaccounted file", len(un) == 2,
          f"{[(f.code, f.detail) for f in fs]}")
    check("warnings name b.txt and c.txt",
          any("b.txt" in f.detail for f in un)
          and any("c.txt" in f.detail for f in un),
          str([f.detail for f in un]))


def t_workspace_files_accepts_a_directory():
    d = tempfile.mkdtemp(prefix="ws-lint-")
    try:
        for name in TOMAS_FILES:
            Path(d, name).write_text("x")
        Path(d, "Archive").mkdir()
        Path(d, "Archive", "inner.txt").write_text("nested")
        plan, err = gherkin.parse_feature(TOMAS_PLAN)
        assert plan is not None, err
        fs = planlint.lint_plan(TOMAS_TASK, to_plan_steps(plan),
                                workspace_files=d)
        un = _code(fs, "unaccounted_file")
        check("a directory path yields the root file set", len(un) == 1
              and "water-heater-manual.txt" in un[0].detail,
              f"{[(f.code, f.detail) for f in fs]}")
        check("nested files are not root files",
              all("inner.txt" not in f.detail for f in un))
    finally:
        shutil.rmtree(d)


def t_answer_only_declaration_is_clear():
    steps = _steps([
        ("I read `roof-warranty.txt` and `furnace-receipt.txt`",
         "the contents are known"),
        ("I compile the list and give it as an answer-only result, "
         "creating no file", "the answer lists the papers and purposes"),
    ])
    fs = planlint.lint_plan(TOMAS_TASK, steps, workspace_files=TOMAS_FILES)
    check("an explicit answer-only result needs no output file",
          _code(fs, "deliverable_clarity") == [],
          f"{[(f.code, f.detail) for f in fs]}")


def t_report_task_without_output_warns():
    steps = _steps([
        ("I read sales.csv", "the data is known"),
        ("I summarize the quarter", "the output summarizes the quarter"),
    ])
    fs = planlint.lint_plan("Write a report on the quarterly sales data",
                            steps)
    dl = _code(fs, "deliverable_clarity")
    check("report language with no named output warns", len(dl) == 1,
          f"{[(f.code, f.detail) for f in fs]}")
    steps2 = _steps([
        ("I read sales.csv", "the data is known"),
        ("I write the report to report.md", '"report.md" exists'),
    ])
    fs2 = planlint.lint_plan("Write a report on the quarterly sales data",
                             steps2)
    check("a named report file is clear",
          _code(fs2, "deliverable_clarity") == [],
          f"{[(f.code, f.detail) for f in fs2]}")


def t_no_deliverable_language_no_warning():
    steps = _steps([
        ('I write "Hello" to hello.txt', '"hello.txt" exists'),
    ])
    fs = planlint.lint_plan('Write the text "Hello" to hello.txt', steps)
    check("no list/inventory/report/index language: no warning",
          _code(fs, "deliverable_clarity") == [],
          f"{[(f.code, f.detail) for f in fs]}")


def t_warnings_do_not_trigger_a_retry():
    d = tempfile.mkdtemp(prefix="ws-lint-retry-")
    try:
        for name in TOMAS_FILES:
            Path(d, name).write_text("x")
        cfg = Config(workspace=d)
        calls = []

        def fake_chat(messages, tools):
            calls.append(1)
            return {"content": TOMAS_PLAN}

        text, steps = request_plan(TOMAS_TASK, [], fake_chat, cfg)
        check("plan returned", text is not None and len(steps) == 2)
        check("warnings never retry the planner", len(calls) == 1,
              f"calls={len(calls)}")
        codes = [f["code"] for s in steps for f in s.lint]
        check("unaccounted_file warning reaches the approval surface",
              "unaccounted_file" in codes, str(codes))
        check("deliverable_clarity warning reaches the approval surface",
              "deliverable_clarity" in codes, str(codes))
        check("no lint errors attached",
              all(f["level"] != "error" for s in steps for f in s.lint),
              str([s.lint for s in steps]))
    finally:
        shutil.rmtree(d)


if __name__ == "__main__":
    for fn in [t_tomas_fixture_warns_on_both_gaps, t_fixed_plan_is_quiet,
               t_no_workspace_files_no_unaccounted,
               t_non_exhaustive_task_no_unaccounted,
               t_generic_listing_suppresses, t_covers_suppresses,
               t_mentioned_file_is_accounted, t_each_unaccounted_file_warns,
               t_workspace_files_accepts_a_directory,
               t_answer_only_declaration_is_clear,
               t_report_task_without_output_warns,
               t_no_deliverable_language_no_warning,
               t_warnings_do_not_trigger_a_retry]:
        fn()
    print("\nAll workspace-aware lint tests passed.")
