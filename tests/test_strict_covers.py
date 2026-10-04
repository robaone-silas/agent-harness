#!/usr/bin/env python3
"""Tests for strict covers under exhaustive cues (issue #17, part 2).

Field failure, 2026-10-04 final replay: Priya's index mentioned five of
six source files, omitting zeta-travel.txt, and the covers verifier
passed it at its at-least-half floor even though the task's contract was
completeness ("every file", "do not omit"). The floor is right for
summaries and strategy documents, which legitimately sample. So covers
is now strict only when completeness is the stated contract: when the
task or the covers step's Intent carries exhaustive cues (every file,
all files, do not omit, each entry/file, no omissions), the verifier
requires ALL source items mentioned. Otherwise the at-least-half floor
is unchanged. The failure detail already names the missing items, so
the existing verify retry feeds them back with no new machinery.

Run: python3 tests/test_strict_covers.py
"""
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness import verify
from harness.tools import Jail


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        raise AssertionError(name)


ITEMS = ["alpha-policy.txt", "beta-hr.txt", "gamma-finance.txt",
         "delta-legal.txt", "epsilon-ops.txt", "zeta-travel.txt"]
FIVE_OF_SIX = ("alpha-policy.txt beta-hr.txt gamma-finance.txt "
               "delta-legal.txt epsilon-ops.txt")
ALL_SIX = FIVE_OF_SIX + " zeta-travel.txt"

EXHAUSTIVE_TASK = ("Read every file in this workspace, then create "
                   "audit-index.md listing every file with its exact "
                   "filename. Do not omit any file.")
SUMMARY_TASK = "Write a short strategy summary of the documents in this folder."


def run_covers(content, task, intent=""):
    d = tempfile.mkdtemp(prefix="strict-covers-")
    try:
        ctx = verify.VerifyContext(jail=Jail(d), step_id=2, task=task,
                                   intent=intent, prior_items={1: list(ITEMS)})
        Path(d, "audit-index.md").write_text(content)
        res = verify.verify_step(
            ['"audit-index.md" covers the files from step 1'], ctx)
        return res
    finally:
        shutil.rmtree(d)


def t_five_of_six_fails_under_exhaustive_task():
    res = run_covers(FIVE_OF_SIX, EXHAUSTIVE_TASK)
    check("5 of 6 fails under an exhaustive task", not res.ok,
          str([c.detail for c in res.checks]))
    detail = res.checks[0].detail
    check("detail names the missing file", "zeta-travel.txt" in detail, detail)
    check("detail reports the count", "5 of 6" in detail, detail)


def t_five_of_six_passes_under_summary_task():
    res = run_covers(FIVE_OF_SIX, SUMMARY_TASK)
    check("5 of 6 passes under a summary task (floor unchanged)", res.ok,
          str([c.detail for c in res.checks]))


def t_full_coverage_passes_under_exhaustive_task():
    res = run_covers(ALL_SIX, EXHAUSTIVE_TASK)
    check("6 of 6 passes under an exhaustive task", res.ok,
          str([c.detail for c in res.checks]))


def t_each_exhaustive_cue_makes_covers_strict():
    cue_tasks = [
        "Index every file in this folder.",
        "Index all files in this folder.",
        "Index all the files in this folder.",
        "Create an index of the files. Do not omit any of them.",
        "Create an index with no omissions.",
        "Create an index with one entry for each file.",
        "Create an index with one entry for each entry in the listing.",
    ]
    for task in cue_tasks:
        res = run_covers(FIVE_OF_SIX, task)
        check(f"cue makes 5 of 6 fail: {task[:44]}", not res.ok,
              str([c.detail for c in res.checks]))


def t_intent_cue_makes_covers_strict_when_task_is_neutral():
    res = run_covers(FIVE_OF_SIX, "Create audit-index.md from the files in step 1.",
                     intent="listing every file without omitting any file")
    check("intent cue (every file / without omitting) makes 5 of 6 fail",
          not res.ok, str([c.detail for c in res.checks]))
    res = run_covers(FIVE_OF_SIX, "Create audit-index.md from the files in step 1.",
                     intent="each entry names the file and what it is for, no omissions")
    check("intent cue (each entry / no omissions) makes 5 of 6 fail",
          not res.ok, str([c.detail for c in res.checks]))


def t_floor_unchanged_without_cues():
    res = run_covers("alpha-policy.txt beta-hr.txt gamma-finance.txt", SUMMARY_TASK)
    check("half coverage still passes without cues", res.ok,
          str([c.detail for c in res.checks]))
    res = run_covers("alpha-policy.txt beta-hr.txt", SUMMARY_TASK)
    check("below-half coverage still fails without cues", not res.ok,
          str([c.detail for c in res.checks]))


def main():
    t_five_of_six_fails_under_exhaustive_task()
    t_five_of_six_passes_under_summary_task()
    t_full_coverage_passes_under_exhaustive_task()
    t_each_exhaustive_cue_makes_covers_strict()
    t_intent_cue_makes_covers_strict_when_task_is_neutral()
    t_floor_unchanged_without_cues()
    print("\nAll strict-covers tests passed.")


if __name__ == "__main__":
    main()
