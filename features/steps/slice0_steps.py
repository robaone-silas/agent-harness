import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from behave import given, when, then

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from harness import gherkin, planlint, planner, verify  # noqa: E402
from harness.verify_registry import VerifyContext  # noqa: E402

FIXTURES = ROOT / "features" / "fixtures"
RUNS = ROOT / "slice0-runs"

TASKS = {
    "Tomas": "Which of my appliances is the oldest? Write the answer in a file for me, with the year for each appliance.",
    "Maya": "Make an index of all the documents in this folder, with a one-line description of each.",
}

PLANS = {
    "narrowed": """Feature: Oldest appliance
  Scenario: Step 1 - Read the appliance records
    Intent: know the year of each appliance
    When I read "dryer-manual.txt", "fridge-receipt.txt", and "washer-receipt.txt"
    Then the output shows the year for each appliance
  Scenario: Step 2 - Find the oldest
    Intent: determine which appliance is the oldest
    When I compare the years from step 1
    Then I know the oldest appliance
  Scenario: Step 3 - Write the answer
    Intent: write the answer in a file for the user, with the year for each appliance
    When I write the oldest appliance and its year to oldest-appliance.txt
    Then "oldest-appliance.txt" contains the oldest appliance and its corresponding year
""",
    "prose_index": """Feature: Docs index
  Scenario: Step 1 - List the documents
    Intent: know every document in the folder
    When I list the files in the workspace
    Then the output lists the documents
  Scenario: Step 2 - Read the documents
    Intent: know what each document is about
    When I read each document from step 1
    Then I know the topic of each document
  Scenario: Step 3 - Write the index
    Intent: write an index with a one-line description of each document
    When I write the index of the documents to docs-index.md
    Then "docs-index.md" contains a one-line description of every document
""",
}


def _steps(text):
    plan, err = gherkin.parse_feature(text)
    assert plan is not None, err
    return planner.to_plan_steps(plan)


@given('the {persona} plan "{name}" from the 2026-10-18 field shape')
@given('the {persona} plan "{name}"')
def given_plan(context, persona, name):
    context.persona = persona
    context.steps = _steps(PLANS[name])


@when("plan lint runs on it for the {persona} task")
def when_lint(context, persona):
    context.findings = planlint.lint_plan(TASKS[persona], context.steps)
    print([(f.step_id, f.level, f.code) for f in context.findings])


@then("lint reports no finding that names the dropped years")
def then_no_year_finding(context):
    hits = [f for f in context.findings if re.search(r"each appliance|every year|narrow|intent", f.detail, re.I)
            and f.code != "attested_compile"]
    assert not hits, f"unexpected narrowing finding: {hits}"


@then("the attested_compile warning is the only signal on the compile step")
def then_only_attested(context):
    last = context.steps[-1].id
    codes = sorted({f.code for f in context.findings if f.step_id == last})
    assert codes == ["attested_compile"], codes


@given('a Tomas workspace whose "{name}" holds only "{content}"')
def given_shrunk(context, name, content):
    ws = RUNS / "deterministic-tomas"
    shutil.rmtree(ws, ignore_errors=True)
    shutil.copytree(FIXTURES / "tomas", ws)
    (ws / name).write_text(content + "\n")
    context.ws = ws


@when('the step 2 Then "{clause}" is verified')
def when_verify(context, clause):
    from harness.tools import Jail
    vc = VerifyContext(jail=Jail(str(context.ws)), step_id=2, task=TASKS["Tomas"])
    context.result = verify.verify_step([clause.replace('\\"', '"')], vc)


@then("verification passes on attestation alone")
def then_attested(context):
    r = context.result
    assert r.ok, r
    assert all(c.verifier == "attest" for c in r.checks), [c.verifier for c in r.checks]


@then("no Then clause on the deliverable step parses to indexes, accounts_for, or covers")
def then_no_machine_index(context):
    parsed = [verify.parse_then(t) for t in context.steps[-1].done_when.split("\n") if t.strip()]
    assert not any(p and p[0] in ("indexes", "accounts_for", "covers") for p in parsed), parsed


@given('a fresh copy of the "{fixture}" fixture for trial {trial:d}')
def given_fresh(context, fixture, trial):
    run_dir = RUNS / f"{fixture}-trial-{trial}"
    shutil.rmtree(run_dir, ignore_errors=True)
    shutil.copytree(FIXTURES / fixture, run_dir / "workspace")
    context.run_dir = run_dir
    context.ws = run_dir / "workspace"
    context.sources = {p.name for p in context.ws.iterdir()}


def _harness(context, args, log_name):
    proc = subprocess.run([sys.executable, str(ROOT / "run.py"), *args, "--workspace", str(context.ws)],
                          cwd=context.run_dir, capture_output=True, text=True, timeout=1800)
    (context.run_dir / log_name).write_text(proc.stdout + "\n--- stderr ---\n" + proc.stderr)
    return proc


@when("the harness proposes a plan with discovery for the {persona} task")
def when_discover(context, persona):
    proc = _harness(context, [TASKS[persona], "--discover"], "01-plan.log")
    assert proc.returncode == 0, proc.stdout[-2000:]
    context.plan_text = (context.run_dir / "plan.feature").read_text()
    context.steps = _steps(context.plan_text)


@when("the approved plan is executed")
def when_execute(context):
    context.run = _harness(context, ["--run", "plan.feature"], "02-run.log")


@then("the run reports done")
def then_done(context):
    assert context.run.returncode == 0, context.run.stdout[-1500:]


@then('the deliverable contains "{a}", "{b}", and "{c}"')
def then_contains(context, a, b, c):
    new = [p for p in context.ws.iterdir() if p.is_file() and p.name not in context.sources]
    assert new, "no deliverable written"
    text = "\n".join(p.read_text(errors="replace") for p in new)
    missing = [y for y in (a, b, c) if y not in text]
    assert not missing, f"missing {missing} in {[p.name for p in new]}: {text!r}"


@then("the deliverable step carries a machine-checked completeness clause")
def then_machine_completeness(context):
    verbs = []
    for s in context.steps:
        for t in (s.done_when or "").split("\n"):
            if t.strip():
                p = verify.parse_then(t.strip())
                verbs.append((s.id, p[0] if p else None, t.strip()))
    (context.run_dir / "then-verbs.json").write_text(json.dumps(verbs, indent=2))
    assert any(v in ("indexes", "accounts_for", "covers") for _, v, _ in verbs), verbs
