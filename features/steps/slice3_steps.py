import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from behave import given, when, then

ROOT_DIRECTORY = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT_DIRECTORY))

from harness import gherkin, planlint, planner, verify  # noqa: E402
from harness.tools import Jail  # noqa: E402
from harness.verify_registry import VerifyContext  # noqa: E402

FIXTURES_DIRECTORY = ROOT_DIRECTORY / "features" / "fixtures"
NOVEMBER2_PLANS_DIRECTORY = FIXTURES_DIRECTORY / "nov2-plans"
RUNS_DIRECTORY = ROOT_DIRECTORY / "slice3-runs"

PERSONA_TASKS = {
    "Tomas": (
        "Which of my appliances is the oldest? Write the answer in a file for "
        "me, with the year for each appliance."
    ),
    "Maya": (
        "Create docs-index.md with one entry per document in this workspace: "
        "the exact filename and a one-line description of what the document "
        "is about. Do not change the source documents."
    ),
}

NOVEMBER2_MAYA_STEP2_THEN = (
    '"docs-index.md" has one entry for api-notes.md and one entry for '
    "deploy.md and one entry for setup.md, where each entry contains the "
    "exact filename and a one-line description of what the document is about"
)

EXPANDED_INDEX_PHRASINGS = [
    '"docs-index.md" has one entry for each document from step 1',
    '"docs-index.md" has one entry for each document from step 1, with a one-line description of each',
    '"docs-index.md" has one entry for each file from step 1',
    '"docs-index.md" has one entry for each item from step 1',
    '"docs-index.md" indexes the documents from step 1',
    '"docs-index.md" indexes the files from step 1',
]


def _parse_plan_steps(plan_text):
    feature_plan, error_message = gherkin.parse_feature(plan_text)
    assert feature_plan is not None, error_message
    return planner.to_plan_steps(feature_plan)


def _load_plan_from_file(filename):
    return _parse_plan_steps((NOVEMBER2_PLANS_DIRECTORY / filename).read_text())


def _extract_verbs_from_steps(plan_steps):
    verb_records = []
    for plan_step in plan_steps:
        lines = [line.strip() for line in (plan_step.done_when or "").split("\n") if line.strip()]
        for line in lines:
            parsed_clause = verify.parse_then(line)
            verb_name = parsed_clause[0] if parsed_clause else None
            verb_records.append((plan_step.id, verb_name, line))
    return verb_records


@given("the exact November 2 plan for {persona_name}")
def step_given_exact_november2_plan(context, persona_name):
    context.persona_name = persona_name
    context.plan_steps = _load_plan_from_file(f"{persona_name.lower()}-2026-11-02.feature")


@given('the Tomas plan "{plan_name}" from the 2026-10-18 field shape')
def step_given_tomas_plan_narrowed(context, plan_name):
    context.persona_name = "Tomas"
    context.plan_steps = _load_plan_from_file("tomas-2026-10-18-narrowed.feature")


@given("a Tomas plan that carries all appliance years into the final step")
def step_given_tomas_plan_full_requirement(context):
    context.persona_name = "Tomas"
    context.plan_steps = _load_plan_from_file("tomas-full-requirement.feature")


@when("plan lint runs on it for the {persona_name} task")
def step_when_plan_lint_runs(context, persona_name):
    context.lint_findings = planlint.lint_plan(PERSONA_TASKS[persona_name], context.plan_steps)


@then('lint reports a "{expected_code}" warning on the compile step')
def step_then_lint_reports_warning_on_compile_step(context, expected_code):
    last_step_id = context.plan_steps[-1].id
    matching_findings = [
        finding for finding in context.lint_findings
        if finding.step_id == last_step_id and finding.code == expected_code
    ]
    assert matching_findings, (
        f"Expected lint warning {expected_code} on step {last_step_id}, "
        f"found: {[(f.step_id, f.code, f.detail) for f in context.lint_findings]}"
    )


@then("the warning detail names the dropped requirement")
def step_then_warning_detail_names_dropped_requirement(context):
    narrowing_findings = [f for f in context.lint_findings if f.code == "plan_narrowing"]
    assert narrowing_findings, "No plan_narrowing findings found"
    detail_text = narrowing_findings[0].detail
    assert re.search(r"for each appliance|every year|narrow", detail_text, re.IGNORECASE), (
        f"Warning detail does not name dropped requirement: {detail_text}"
    )


@then('lint reports no "{expected_code}" finding')
def step_then_lint_reports_no_finding(context, expected_code):
    matching_findings = [f for f in context.lint_findings if f.code == expected_code]
    assert not matching_findings, (
        f"Unexpected findings for {expected_code}: "
        f"{[(f.step_id, f.code, f.detail) for f in matching_findings]}"
    )


@given('a Tomas workspace whose "{filename}" preserves all appliance years alongside the oldest')
def step_given_tomas_workspace_preserving_years(context, filename):
    workspace_directory = RUNS_DIRECTORY / "deterministic-tomas"
    shutil.rmtree(workspace_directory, ignore_errors=True)
    shutil.copytree(FIXTURES_DIRECTORY / "tomas", workspace_directory)
    deliverable_content = (
        "Appliance Installation Records:\n"
        "- Dryer: installed in 2016\n"
        "- Refrigerator: purchased in 2018\n"
        "- Washer: purchased in 2021\n\n"
        "The oldest appliance is the Dryer, installed in 2016.\n"
    )
    (workspace_directory / filename).write_text(deliverable_content)
    context.workspace_directory = workspace_directory
    context.deliverable_filename = filename


@when('the deliverable "{filename}" is inspected for completeness')
def step_when_deliverable_inspected(context, filename):
    deliverable_path = context.workspace_directory / filename
    assert deliverable_path.is_file(), f"Deliverable {filename} does not exist"
    context.deliverable_text = deliverable_path.read_text()


@then('the deliverable contains "{year_first}", "{year_second}", and "{year_third}"')
def step_then_deliverable_contains_all_years(context, year_first, year_second, year_third):
    missing_years = [y for y in (year_first, year_second, year_third) if y not in context.deliverable_text]
    assert not missing_years, f"Missing required years {missing_years} in deliverable: {context.deliverable_text!r}"


@then('the deliverable identifies "{appliance_name}" as the oldest appliance')
def step_then_deliverable_identifies_oldest(context, appliance_name):
    assert appliance_name in context.deliverable_text, (
        f"Oldest appliance {appliance_name} not found in: {context.deliverable_text!r}"
    )


@given("the expanded phrasing variants for document index completeness")
def step_given_expanded_phrasing_variants(context):
    context.phrasing_variants = EXPANDED_INDEX_PHRASINGS


@then('each variant parses to "{expected_verifier}"')
def step_then_each_variant_parses(context, expected_verifier):
    for clause_text in context.phrasing_variants:
        parsed_result = verify.parse_then(clause_text)
        assert parsed_result is not None, f"Failed to parse phrasing clause: {clause_text}"
        verifier_name = parsed_result[0]
        assert verifier_name == expected_verifier, (
            f"Expected verifier {expected_verifier} but got {verifier_name} for clause: {clause_text}"
        )


@given("the exact November 2 workspace for Maya")
def step_given_exact_november2_maya_workspace(context):
    workspace_directory = RUNS_DIRECTORY / "deterministic-maya"
    shutil.rmtree(workspace_directory, ignore_errors=True)
    shutil.copytree(FIXTURES_DIRECTORY / "maya", workspace_directory)
    context.workspace_directory = workspace_directory


@when('the indexing verifier checks "{deliverable_filename}" for the documents from step 1')
def step_when_indexing_verifier_checks(context, deliverable_filename):
    index_content = (
        "# Document Index\n\n"
        "- api-notes.md: API documentation and authentication guidelines\n"
        "- deploy.md: Deployment procedures for staging and production\n"
        "- setup.md: Environment setup and installation instructions\n"
    )
    (context.workspace_directory / deliverable_filename).write_text(index_content)
    jail_environment = Jail(str(context.workspace_directory))
    verify_context = VerifyContext(
        jail=jail_environment,
        step_id=2,
        task=PERSONA_TASKS["Maya"],
        prior_items={1: ["api-notes.md", "deploy.md", "setup.md"]},
    )
    verification_clause = (
        f'"{deliverable_filename}" has one entry for each document from step 1, '
        "with a one-line description of each"
    )
    context.verification_result = verify.verify_step([verification_clause], verify_context)


@then('verification passes and confirms "{document_one}", "{document_two}", and "{document_three}" are all indexed')
def step_then_verification_confirms_all_indexed(context, document_one, document_two, document_three):
    result = context.verification_result
    assert result.ok, f"Verification failed: {result}"
    check_records = result.checks
    assert check_records and any(r.verifier == "indexes" for r in check_records), (
        f"Expected indexes verifier, got: {[r.verifier for r in check_records]}"
    )
    content = (context.workspace_directory / "docs-index.md").read_text()
    for document_name in (document_one, document_two, document_three):
        assert document_name in content, f"Document {document_name} missing from verified deliverable: {content}"


@given("the November 2 discovery findings for Tomas")
def step_given_november2_discovery_findings(context):
    discovery_path = NOVEMBER2_PLANS_DIRECTORY / "tomas-2026-11-02-discovery-findings.txt"
    context.discovery_findings = discovery_path.read_text()


@then("discovery knew all three appliance years")
def step_then_discovery_knew_all_years(context):
    for expected_year in ("2016", "2018", "2021"):
        assert expected_year in context.discovery_findings, (
            f"Year {expected_year} missing from discovery findings: {context.discovery_findings}"
        )


@then("step 1 keeps the full requirement, one year for each appliance")
def step_then_step1_keeps_full_requirement(context):
    step_one_then = context.plan_steps[0].done_when
    assert "for each appliance" in step_one_then, f"Step 1 Then does not retain full requirement: {step_one_then}"


@then("step 2 narrows the requirement to the oldest appliance only")
def step_then_step2_narrows_requirement(context):
    step_two_then = context.plan_steps[1].done_when
    assert "for each appliance" not in step_two_then, step_two_then
    assert "oldest" in step_two_then, step_two_then


@then("step 3 triggers a plan_narrowing lint finding")
def step_then_step3_triggers_plan_narrowing(context):
    findings = planlint.lint_plan(PERSONA_TASKS["Tomas"], context.plan_steps)
    narrowing_findings = [f for f in findings if f.step_id == 3 and f.code == "plan_narrowing"]
    assert narrowing_findings, f"Step 3 did not trigger plan_narrowing: {[(f.step_id, f.code) for f in findings]}"


@then("step 2 Then is the exact November 2 wording and is flagged as an unparsed completeness clause")
def step_then_step2_exact_november2_wording(context):
    step_two = context.plan_steps[1]
    then_lines = [line.strip() for line in step_two.done_when.split("\n") if line.strip()]
    assert then_lines == [NOVEMBER2_MAYA_STEP2_THEN], then_lines
    parsed_results = [verify.parse_then(line) for line in then_lines]
    assert all(parsed is None for parsed in parsed_results), parsed_results


@given('a fresh copy of the "{fixture_name}" fixture for trial {trial_number:d}')
def step_given_fresh_copy_fixture(context, fixture_name, trial_number):
    run_directory = RUNS_DIRECTORY / f"{fixture_name}-trial-{trial_number}"
    shutil.rmtree(run_directory, ignore_errors=True)
    shutil.copytree(FIXTURES_DIRECTORY / fixture_name, run_directory / "workspace")
    context.run_directory = run_directory
    context.workspace_directory = run_directory / "workspace"
    context.original_source_names = {path.name for path in context.workspace_directory.iterdir()}


def _execute_harness(context, command_arguments, log_filename):
    execution_result = subprocess.run(
        [sys.executable, str(ROOT_DIRECTORY / "run.py"), *command_arguments, "--workspace", str(context.workspace_directory)],
        cwd=context.run_directory, capture_output=True, text=True, timeout=1800,
    )
    log_content = execution_result.stdout + "\n--- stderr ---\n" + execution_result.stderr
    (context.run_directory / log_filename).write_text(log_content)
    return execution_result


@when("the harness proposes a plan with discovery for the {persona_name} task")
def step_when_harness_proposes_plan(context, persona_name):
    execution_result = _execute_harness(context, [PERSONA_TASKS[persona_name], "--discover"], "01-plan.log")
    assert execution_result.returncode == 0, execution_result.stdout[-2000:]
    plan_feature_text = (context.run_directory / "plan.feature").read_text()
    context.plan_steps = _parse_plan_steps(plan_feature_text)


@when("the approved plan is executed")
def step_when_approved_plan_executed(context):
    context.execution_run = _execute_harness(context, ["--run", "plan.feature"], "02-run.log")


@then("the run reports done")
def step_then_run_reports_done(context):
    assert context.execution_run.returncode == 0, context.execution_run.stdout[-1500:]


@then("the deliverable step carries a machine-checked completeness clause")
def step_then_deliverable_carries_machine_completeness(context):
    verb_records = _extract_verbs_from_steps(context.plan_steps)
    (context.run_directory / "then-verbs.json").write_text(json.dumps(verb_records, indent=2))
    assert any(verb in ("indexes", "accounts_for") for _, verb, _ in verb_records), verb_records
