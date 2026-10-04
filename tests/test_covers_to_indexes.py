"""Tests for the covers-to-indexes lint push.

Ansel's direction (2026-10-04): push describe-each and list-type
compile steps toward the indexes primitive. Teaching alone did not
invoke it (the live Priya plan chose covers), so the push travels
through the channel that has worked before: a retryable planlint
ERROR whose feedback names the replacement clause.

The rule fires on a step that carries a covers clause when the plan
shape is an entry-per-file compilation drawn directly on the listing
step: the task or the step's language is describe-each or list-type
(index, inventory, catalog, list), the step's When references only
the covers source step, and the step produces the target. It stays
silent when the task forbids reading file contents (a strategy from
filenames only), when the covers step assembles earlier gather steps'
work (the song catalog shape), and of course when the plan already
uses indexes.

Run: python3 tests/test_covers_to_indexes.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness import planlint
from harness.planner import PlanStep

CODE = "covers_instead_of_indexes"

PRIYA_TASK = (
    "Read every file in this workspace, then create audit-index.md listing "
    "every file with its exact filename and a one-line description of what "
    "it actually contains. Do not omit any file."
)

DANIEL_TASK = (
    "Suggest an organization strategy for these documents based only on "
    "the filenames. Do not read the contents of the files. Do not move, "
    "rename, delete, or modify any existing file. Write the organization "
    "strategy to organization-strategy.md."
)


def _steps(pairs):
    return [PlanStep(id=i + 1, instruction=w, done_when=t,
                     intent=it)
            for i, (w, t, it) in enumerate(pairs)]


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name
          + (f" -- {info}" if info and not cond else ""))
    assert cond, f"{name}: {info}"


def _hits(task, steps):
    return [f for f in planlint.lint_plan(task, steps) if f.code == CODE]


def t_priya_shape_fires():
    steps = _steps([
        ("I list the files in the folder",
         "the output lists all files in the workspace", ""),
        ("I read every file from step 1 and write audit-index.md with "
         "the filename and a one-line description of its content",
         '"audit-index.md" covers the files from step 1',
         "list every file with its exact filename and a one-line "
         "description of what it actually contains"),
    ])
    hits = _hits(PRIYA_TASK, steps)
    check("Priya shape fires once", len(hits) == 1,
          f"{[(f.code, f.detail) for f in hits]}")
    check("the push is a retryable error", hits[0].level == "error",
          hits[0].level)
    check("the push lands on the compile step", hits[0].step_id == 2,
          str(hits[0].step_id))
    check("feedback names the replacement clause",
          '"audit-index.md" indexes the files from step 1'
          in hits[0].detail, hits[0].detail)


def t_list_type_task_with_terse_when_fires():
    steps = _steps([
        ("I list the files in the folder",
         "the output lists the files", ""),
        ("I compile the index of the files from step 1",
         '"recipe-index.md" covers the files from step 1', ""),
    ])
    hits = _hits("Make an index of the recipes in this folder", steps)
    check("list-type task with a direct compile step fires",
          len(hits) == 1, f"{[(f.code, f.detail) for f in hits]}")


def t_inventory_task_fires():
    steps = _steps([
        ("I list the papers in the folder",
         "the output lists the papers", ""),
        ("I read each paper from step 1 and write house-inventory.txt "
         "with what each one is for",
         '"house-inventory.txt" covers the files from step 1', ""),
    ])
    hits = _hits("Make a list of my house papers and what each one is "
                 "for.", steps)
    check("inventory (Tomas) shape fires", len(hits) == 1,
          f"{[(f.code, f.detail) for f in hits]}")


def t_no_read_task_never_fires():
    steps = _steps([
        ("I list the files in the folder",
         "the output lists the files", ""),
        ("I write organization-strategy.md from the file names in "
         "step 1",
         '"organization-strategy.md" covers the files from step 1', ""),
    ])
    hits = _hits(DANIEL_TASK, steps)
    check("a task forbidding content reads is never pushed to indexes",
          hits == [], f"{[(f.code, f.detail) for f in hits]}")


def t_assembly_step_does_not_fire():
    steps = _steps([
        ("I list the songs in the folder",
         "the output lists the song files", ""),
        ("I read the first three songs from step 1 and note each "
         "song's length",
         "the output gives a length for each song read", ""),
        ("I read the remaining songs from step 1 and note each "
         "song's length",
         "the output gives a length for each song read", ""),
        ("I write catalog.md with one entry per song from the notes "
         "in steps 2 and 3",
         '"catalog.md" covers the files from step 1', ""),
    ])
    hits = _hits("Build a catalog of the songs in this folder, one "
                 "entry per song with its length", steps)
    check("the assembly step over gather steps stays on covers",
          hits == [], f"{[(f.code, f.detail) for f in hits]}")


def t_synthesis_report_does_not_fire():
    steps = _steps([
        ("I list the documents in the folder",
         "the output lists the documents", ""),
        ("I read the documents from step 1 and write report.md "
         "summarizing the quarter",
         '"report.md" covers the files from step 1', ""),
    ])
    hits = _hits("Write a report on these documents summarizing the "
                 "quarter.", steps)
    check("a synthesis report (no describe-each, no list-type task) "
          "stays on covers", hits == [],
          f"{[(f.code, f.detail) for f in hits]}")


def t_already_indexes_is_silent():
    steps = _steps([
        ("I list the files in the folder",
         "the output lists all files in the workspace", ""),
        ("I compile the index of the files from step 1",
         '"audit-index.md" indexes the files from step 1', ""),
    ])
    hits = _hits(PRIYA_TASK, steps)
    check("a plan already using indexes gets no push", hits == [],
          f"{[(f.code, f.detail) for f in hits]}")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("t_") and callable(v)]
    for fn in fns:
        fn()
    print("\nAll covers-to-indexes tests passed.")
