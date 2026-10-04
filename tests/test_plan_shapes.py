#!/usr/bin/env python3
"""Tests for comprehensive plan-shape exemplars (issue #5, variant shape).

Field failure, 2026-10-04 closing replay: the planner split Priya's index
into seven per-file steps with no covers check anywhere. The completeness
dodge from issue #5 survived by changing shape: PR #10's lint rule sees
per-item clauses concentrated in one step, not spread across steps. The
prompt's example shelf taught only three shapes, two of them the same
list-then-compile, and nothing taught per-item processing done right, so
the model improvised the split and the completeness check was split away
with the work. Ansel's call: fill the knowledge gap with more
comprehensive examples of plan shapes. These tests pin the expanded
shelf: every exemplar parses as a real plan, a per-item shape whose
final step still carries covers against the listing step, a
read-and-compute shape, and an inventory shape that turns a vague ask
into a named durable deliverable.

Run: python3 tests/test_plan_shapes.py
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness import gherkin, planner


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        raise AssertionError(name)


def example_plans():
    """Parse every exemplar in _EXAMPLE_PLANS into (task_line, steps)."""
    out = []
    for chunk in re.split(r"\nTask: ", "\n" + planner._EXAMPLE_PLANS):
        chunk = chunk.strip()
        if not chunk.startswith(("Task: ", "Feature:")):
            head = chunk.split("\n", 1)[0]
            if "Feature:" not in chunk:
                continue
            chunk = chunk[chunk.index("Feature:"):]
            task_line = head
        else:
            task_line, _, rest = chunk.partition("\n")
            chunk = rest
        # Keep only Gherkin-shaped lines: the closing prose note after the
        # last exemplar is prompt text, not plan text.
        chunk = "\n".join(
            ln for ln in chunk.splitlines()
            if not ln.strip() or ln.startswith((" ", "\t", "Feature:")))
        plan, err = gherkin.parse_feature(chunk.strip())
        assert plan is not None, f"exemplar does not parse: {err}"
        out.append((task_line, planner.to_plan_steps(plan)))
    return out


def t_every_exemplar_parses():
    plans = example_plans()
    check("the shelf holds at least six exemplar plans", len(plans) >= 6,
          str(len(plans)))
    for task_line, steps in plans:
        check(f"exemplar has at least two steps: {task_line[:40]}",
              len(steps) >= 2, str(len(steps)))


def t_per_item_shape_keeps_covers_on_the_final_step():
    plans = example_plans()
    found = None
    for task_line, steps in plans:
        if len(steps) >= 4 and any(
                "covers the files from step 1" in s.done_when
                for s in steps[-1:]):
            found = (task_line, steps)
            break
    check("a per-item exemplar exists (4+ steps, covers at the end)",
          found is not None)
    task_line, steps = found
    middle = steps[1:-1]
    check("per-item middle steps gather without writing the deliverable",
          all("covers" not in s.done_when for s in middle))
    check("the final step is the one carrying covers",
          "covers the files from step 1" in steps[-1].done_when,
          steps[-1].done_when)


def t_read_and_compute_shape_present():
    plans = example_plans()
    found = [steps for _, steps in plans
             if any("computed" in s.done_when for s in steps)]
    check("a read-and-compute exemplar exists", len(found) >= 1)
    steps = found[0]
    check("the compute exemplar reads named files first",
          '"' in steps[0].instruction and "read" in steps[0].instruction.lower(),
          steps[0].instruction)


def t_inventory_shape_names_a_durable_deliverable():
    plans = example_plans()
    found = None
    for task_line, steps in plans:
        if "inventory" in task_line.lower():
            found = steps
            break
    check("an inventory exemplar exists", found is not None)
    last = found[-1]
    check("inventory deliverable carries indexes",
          "indexes the files from step 1" in last.done_when,
          last.done_when)
    check("inventory deliverable step carries an intent",
          bool(last.intent), last.instruction)


def t_lesson_note_present_in_prompt():
    p = planner.build_planner_prompt("index the files", ["list_dir"])
    check("prompt states the split lesson",
          "never splits away the completeness check" in p)
    check("per-item exemplar reaches the prompt", "catalog.md" in p)
    check("inventory exemplar reaches the prompt", "seed-inventory.txt" in p)


def main():
    t_every_exemplar_parses()
    t_per_item_shape_keeps_covers_on_the_final_step()
    t_read_and_compute_shape_present()
    t_inventory_shape_names_a_durable_deliverable()
    t_lesson_note_present_in_prompt()
    print("\nAll plan-shape exemplar tests passed.")


if __name__ == "__main__":
    main()
