"""Unit tests for the prototype graph checker.

The graph checker (harness/graphcheck.py) supplements planlint: where
lint checks plan text rule by rule, the graph checker builds the plan's
data-flow graph, nodes with declared input and output fields, edges
where one node's output feeds another's input, and checks the graph's
structure. A node can only succeed when it can produce what the next
node needs, so at plan time that means: every consumed field has a
producer, step references resolve to earlier nodes, and a listing
clause draws its file list from a node that actually produces one.

The checker is plan-time and structural. It cannot catch a runtime
failure in a well-formed plan (issue #22: Daniel Okafor's plan named
the right deliverable; the executor wrote the content to the wrong
path). One test below pins that boundary on purpose.

Run: python3 tests/test_graphcheck.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness import graphcheck, gherkin
from harness.planner import PlanStep, to_plan_steps

PRIYA_TASK = (
    "Read every file in this workspace, then create audit-index.md listing "
    "every file with its exact filename and a one-line description of what "
    "it actually contains. Do not omit any file."
)

# The shape the closing replay approved: list, then one read-and-write
# step carrying the indexes clause.
HEALTHY_PLAN = """Feature: File Audit Index
  Scenario: Step 1 - List all files
    Intent: know exactly which files the audit index must cover
    When I list the files in the folder
    Then the output lists all files in the workspace
  Scenario: Step 2 - Read files and write the index
    Intent: create audit-index.md listing every file with a one-line description
    When I read every file from step 1 and write audit-index.md
    Then "audit-index.md" indexes the files from step 1
"""

FILE_HANDOFF_PLAN = """Feature: Totals
  Scenario: Step 1 - Compute totals
    When I write totals.csv with the summed values
    Then "totals.csv" exists
  Scenario: Step 2 - Report from totals
    When I read totals.csv and write report.md summarizing it
    Then "report.md" exists
"""


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name
          + (f" -- {info}" if info and not cond else ""))
    assert cond, f"{name}: {info}"


def _steps(text):
    plan, err = gherkin.parse_feature(text)
    assert plan is not None, err
    return to_plan_steps(plan)


def _by_code(findings, code):
    return [f for f in findings if f.code == code]


def t_graph_structure_healthy_plan():
    steps = _steps(HEALTHY_PLAN)
    g = graphcheck.build_graph(PRIYA_TASK, steps)
    check("graph has one node per step", len(g.nodes) == 2,
          str(len(g.nodes)))
    n1, n2 = g.nodes
    check("listing node outputs the file list",
          "files from step 1" in n1.outputs, str(n1.outputs))
    check("compile node consumes the file list",
          "files from step 1" in n2.inputs, str(n2.inputs))
    check("compile node outputs the index",
          "audit-index.md" in n2.outputs, str(n2.outputs))
    edges = {(e.producer, e.consumer, e.field) for e in g.edges}
    check("listing edge runs step 1 -> step 2",
          (1, 2, "files from step 1") in edges, str(edges))
    d = g.as_dict()
    check("graph serializes nodes and edges",
          len(d["nodes"]) == 2 and len(d["edges"]) >= 1, str(d))


def t_healthy_plan_has_no_findings():
    steps = _steps(HEALTHY_PLAN)
    fs = graphcheck.check_plan(PRIYA_TASK, steps)
    check("healthy plan: no graph findings", fs == [],
          f"{[(f.code, f.detail) for f in fs]}")


def t_file_handoff_edge():
    steps = _steps(FILE_HANDOFF_PLAN)
    g = graphcheck.build_graph("Compute totals, then write report.md.", steps)
    edges = {(e.producer, e.consumer, e.field, e.kind) for e in g.edges}
    check("file edge step 1 -> step 2 for totals.csv",
          (1, 2, "totals.csv", "file") in edges, str(edges))
    fs = graphcheck.check_plan(
        "Compute totals, then write report.md.", steps)
    check("file handoff plan: no findings", fs == [],
          f"{[(f.code, f.detail) for f in fs]}")


def t_unresolved_step_reference():
    steps = [PlanStep(id=1, instruction="I use the results from step 2",
                      done_when="the output exists"),
             PlanStep(id=2, instruction="I write out.txt",
                      done_when='"out.txt" exists')]
    fs = graphcheck.check_plan("Write out.txt.", steps)
    hits = _by_code(fs, "unresolved_step_reference")
    check("forward reference fires once", len(hits) == 1,
          f"{[(f.code, f.detail) for f in fs]}")
    check("forward reference is an error", hits[0].level == "error",
          hits[0].level)
    check("forward reference is on step 1", hits[0].step_id == 1,
          str(hits[0].step_id))
    steps2 = [PlanStep(id=1, instruction="I check the files from step 9",
                       done_when="the output exists")]
    hits2 = _by_code(graphcheck.check_plan("Check files.", steps2),
                     "unresolved_step_reference")
    check("nonexistent step reference fires", len(hits2) == 1,
          f"{[(f.code, f.detail) for f in hits2]}")


def t_listing_clause_source_not_a_listing():
    steps = [PlanStep(id=1, instruction="I read values.csv carefully",
                      done_when='"values.csv" exists'),
             PlanStep(id=2, instruction="I write audit-index.md",
                      done_when='"audit-index.md" indexes the files from step 1')]
    fs = graphcheck.check_plan(PRIYA_TASK, steps)
    hits = _by_code(fs, "covers_source_not_a_listing")
    check("indexes from a non-listing step fires once", len(hits) == 1,
          f"{[(f.code, f.detail) for f in fs]}")
    check("listing source finding is an error", hits[0].level == "error",
          hits[0].level)
    check("listing source finding is on step 2", hits[0].step_id == 2,
          str(hits[0].step_id))


def t_unsatisfied_input_workspace_aware():
    steps = [PlanStep(id=1, instruction="I read missing-data.csv and write summary.md",
                      done_when='"summary.md" exists')]
    fs = graphcheck.check_plan(
        "Summarize the data into summary.md.", steps,
        workspace_files={"values.csv", "notes.md"})
    hits = _by_code(fs, "unsatisfied_input")
    check("reading a file nobody produces and the workspace lacks fires",
          len(hits) == 1, f"{[(f.code, f.detail) for f in fs]}")
    check("unsatisfied input is a warning", hits[0].level == "warn",
          hits[0].level)
    ok_steps = [PlanStep(id=1, instruction="I read values.csv and write summary.md",
                         done_when='"summary.md" exists')]
    fs2 = graphcheck.check_plan(
        "Summarize the data into summary.md.", ok_steps,
        workspace_files={"values.csv", "notes.md"})
    check("reading a workspace file is satisfied",
          _by_code(fs2, "unsatisfied_input") == [],
          f"{[(f.code, f.detail) for f in fs2]}")
    fs3 = graphcheck.check_plan(
        "Summarize the data into summary.md.", steps)
    check("without a workspace set the check stays off",
          _by_code(fs3, "unsatisfied_input") == [],
          f"{[(f.code, f.detail) for f in fs3]}")


def t_consumed_before_produced():
    steps = [PlanStep(id=1, instruction="I read summary.txt and write notes.md",
                      done_when='"notes.md" exists'),
             PlanStep(id=2, instruction="I write summary.txt",
                      done_when='"summary.txt" exists')]
    fs = graphcheck.check_plan(
        "Take notes, then write the summary.", steps,
        workspace_files={"values.csv"})
    hits = _by_code(fs, "consumed_before_produced")
    check("consuming a later step's output fires once", len(hits) == 1,
          f"{[(f.code, f.detail) for f in fs]}")
    check("consumed-before-produced is a warning",
          hits[0].level == "warn", hits[0].level)
    check("not also reported unsatisfied",
          _by_code(fs, "unsatisfied_input") == [],
          f"{[(f.code, f.detail) for f in fs]}")


def t_task_output_never_produced():
    steps = [PlanStep(id=1, instruction="I write notes.txt with the findings",
                      done_when='"notes.txt" exists')]
    fs = graphcheck.check_plan(
        "Write the findings to report.md.", steps,
        workspace_files={"values.csv"})
    hits = _by_code(fs, "task_output_never_produced")
    check("task-named deliverable nobody produces fires once",
          len(hits) == 1, f"{[(f.code, f.detail) for f in fs]}")
    check("task output finding is a warning", hits[0].level == "warn",
          hits[0].level)
    maya_steps = [PlanStep(
        id=1,
        instruction="I read values.csv and replace the contents of draft_summary.txt",
        done_when='"draft_summary.txt" contains exactly "Total: 42"')]
    fs2 = graphcheck.check_plan(
        "Calculate the sum in values.csv and replace the contents of "
        "draft_summary.txt with exactly 'Total: 42'.",
        maya_steps, workspace_files={"values.csv", "draft_summary.txt"})
    check("overwriting a workspace file the task names is produced, not missing",
          _by_code(fs2, "task_output_never_produced") == [],
          f"{[(f.code, f.detail) for f in fs2]}")


def t_well_formed_plan_runtime_failure_is_out_of_scope():
    # Daniel Okafor's shape (issue #22): the plan names the deliverable
    # correctly; the run failed at execution time, not in the graph.
    # The checker must stay silent here: it validates plan structure,
    # and claiming otherwise would oversell it.
    steps = [PlanStep(id=1, instruction="I list the files in the workspace",
                      done_when="the output lists the files"),
             PlanStep(id=2, instruction="I write organization-strategy.md from the file names in step 1",
                      done_when='"organization-strategy.md" contains "##"')]
    fs = graphcheck.check_plan(
        "Suggest an organization strategy for these documents based only "
        "on the filenames. Write it to organization-strategy.md.", steps)
    check("well-formed graph: checker silent (runtime failures are not its claim)",
          fs == [], f"{[(f.code, f.detail) for f in fs]}")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("t_") and callable(v)]
    for fn in fns:
        fn()
    print("\nAll graphcheck tests passed.")
