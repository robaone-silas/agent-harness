#!/usr/bin/env python3
"""Verification tests for November 2, 2026 daily persona failures (issue #37 Slice 3).

Verifies the Slice 1 (Maya document index completeness) and Slice 2 (Tomas
requirement-carrying compile) implementations on the exact November 2 fixtures
and failure shapes.

Run: python3 tests/test_issue37_nov2_verification.py
"""
import shutil
import sys
import tempfile
from pathlib import Path

ROOT_DIRECTORY = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIRECTORY))

from harness import gherkin, planlint, planner, verify
from harness.tools import Jail
from harness.verify_registry import VerifyContext

FIXTURES_DIRECTORY = ROOT_DIRECTORY / "features" / "fixtures"
NOVEMBER2_PLANS_DIRECTORY = FIXTURES_DIRECTORY / "nov2-plans"

TOMAS_TASK = (
    "Which of my appliances is the oldest? Write the answer in a file for me, "
    "with the year for each appliance."
)

MAYA_TASK = (
    "Create docs-index.md with one entry per document in this workspace: "
    "the exact filename and a one-line description of what the document "
    "is about. Do not change the source documents."
)

EXACT_NOVEMBER2_MAYA_STEP2_THEN = (
    '"docs-index.md" has one entry for api-notes.md and one entry for '
    "deploy.md and one entry for setup.md, where each entry contains the "
    "exact filename and a one-line description of what the document is about"
)


def check(name, condition, detail=""):
    print(("PASS " if condition else "FAIL ") + name
          + (f" -- {detail}" if detail and not condition else ""))
    if not condition:
        raise AssertionError(name)


def parse_plan_steps(plan_text):
    feature_plan, error_message = gherkin.parse_feature(plan_text)
    assert feature_plan is not None, error_message
    return planner.to_plan_steps(feature_plan)


def _copy_files_to_directory(source_directory, target_directory):
    for document_path in source_directory.glob("*"):
        if document_path.is_file():
            shutil.copy(document_path, Path(target_directory) / document_path.name)


def t_nov2_tomas_plan_triggers_plan_narrowing():
    tomas_plan_path = NOVEMBER2_PLANS_DIRECTORY / "tomas-2026-11-02.feature"
    plan_steps = parse_plan_steps(tomas_plan_path.read_text())
    findings = planlint.lint_plan(TOMAS_TASK, plan_steps)

    narrowing_findings = [
        finding for finding in findings
        if finding.step_id == 3 and finding.code == "plan_narrowing"
    ]
    check("exact nov 2 tomas plan triggers plan_narrowing on step 3",
          len(narrowing_findings) == 1,
          str([(finding.step_id, finding.code, finding.detail) for finding in findings]))

    warning_detail = narrowing_findings[0].detail
    check("plan_narrowing warning detail names dropped requirement",
          "for each appliance" in warning_detail.lower() and "narrow" in warning_detail.lower(),
          warning_detail)


def t_nov2_tomas_discovery_findings_contain_all_years():
    discovery_path = NOVEMBER2_PLANS_DIRECTORY / "tomas-2026-11-02-discovery-findings.txt"
    discovery_text = discovery_path.read_text()
    check("tomas discovery contains 2016", "2016" in discovery_text, discovery_text)
    check("tomas discovery contains 2018", "2018" in discovery_text, discovery_text)
    check("tomas discovery contains 2021", "2021" in discovery_text, discovery_text)
    check("tomas discovery names dryer as oldest", "dryer" in discovery_text.lower(), discovery_text)


def t_nov2_tomas_source_fixtures_match_records():
    tomas_fixtures = FIXTURES_DIRECTORY / "tomas"
    dryer_text = (tomas_fixtures / "dryer-manual.txt").read_text()
    fridge_text = (tomas_fixtures / "fridge-receipt.txt").read_text()
    washer_text = (tomas_fixtures / "washer-receipt.txt").read_text()

    check("dryer manual contains 2016", "2016" in dryer_text, dryer_text)
    check("fridge receipt contains 2018", "2018" in fridge_text, fridge_text)
    check("washer receipt contains 2021", "2021" in washer_text, washer_text)


def t_nov2_tomas_full_requirement_plan_passes_plan_narrowing():
    plan_text = """Feature: Appliance age determination
  Scenario: Step 1 - Extract installation years
    Intent: read the installation or purchase year for each appliance
    When I read "dryer-manual.txt", "fridge-receipt.txt", and "washer-receipt.txt"
    Then the output shows the installation year for each appliance
  Scenario: Step 2 - Determine the oldest appliance
    Intent: identify the appliance with the minimum installation year and keep all years
    When I compare the installation years from step 1
    Then the output identifies the dryer as the oldest appliance
  Scenario: Step 3 - Write the result
    Intent: write the oldest appliance and its year to a file, with the year for each appliance
    When I write "oldest-appliance.txt" with the oldest appliance and the year for each appliance
    Then "oldest-appliance.txt" contains the dryer, the oldest year 2016, and the year for each appliance
"""
    plan_steps = parse_plan_steps(plan_text)
    findings = planlint.lint_plan(TOMAS_TASK, plan_steps)
    narrowing_findings = [
        finding for finding in findings if finding.code == "plan_narrowing"
    ]
    check("full requirement plan produces no plan_narrowing findings",
          len(narrowing_findings) == 0,
          str([(finding.step_id, finding.code, finding.detail) for finding in narrowing_findings]))


def t_nov2_tomas_deliverable_inspection():
    deliverable_text = (
        "Appliance Installation Records:\n"
        "- Dryer: installed in 2016\n"
        "- Refrigerator: purchased in 2018\n"
        "- Washer: purchased in 2021\n\n"
        "The oldest appliance is the Dryer, installed in 2016.\n"
    )
    for required_year in ("2016", "2018", "2021"):
        check(f"deliverable contains year {required_year}",
              required_year in deliverable_text, deliverable_text)
    check("deliverable names dryer as oldest",
          "dryer" in deliverable_text.lower(), deliverable_text)


def t_nov2_maya_fixtures_match_documents():
    maya_fixtures = FIXTURES_DIRECTORY / "maya"
    api_notes = (maya_fixtures / "api-notes.md").read_text()
    deploy_doc = (maya_fixtures / "deploy.md").read_text()
    setup_doc = (maya_fixtures / "setup.md").read_text()

    check("api-notes.md exists and has content", "API notes" in api_notes, api_notes)
    check("deploy.md exists and has content", "Deploy" in deploy_doc, deploy_doc)
    check("setup.md exists and has content", "Setup" in setup_doc, setup_doc)


def t_nov2_maya_step2_then_unparsed_status():
    maya_plan_path = NOVEMBER2_PLANS_DIRECTORY / "maya-2026-11-02.feature"
    plan_steps = parse_plan_steps(maya_plan_path.read_text())
    step_two_then = plan_steps[1].done_when.strip()

    check("exact nov 2 maya step 2 then text matches fixture",
          step_two_then == EXACT_NOVEMBER2_MAYA_STEP2_THEN,
          step_two_then)

    parsed_result = verify.parse_then(step_two_then)
    check("exact nov 2 enumerated phrasing returns None from parse_then",
          parsed_result is None,
          str(parsed_result))


def t_nov2_maya_phrasing_variants_parse_to_indexes():
    phrasing_variants = [
        '"docs-index.md" has one entry for each document from step 1',
        '"docs-index.md" has one entry for each document from step 1, with a one-line description of each',
        '"docs-index.md" has one entry for each file from step 1',
        '"docs-index.md" has one entry for each item from step 1',
        '"docs-index.md" indexes the documents from step 1',
        '"docs-index.md" indexes the files from step 1',
    ]
    for clause_text in phrasing_variants:
        parsed_result = verify.parse_then(clause_text)
        check(f"phrasing parses to indexes: {clause_text[:40]}...",
              parsed_result is not None and parsed_result[0] == "indexes",
              str(parsed_result))


def t_nov2_maya_index_verifier_on_exact_fixtures():
    temporary_directory = tempfile.mkdtemp(prefix="nov2-maya-verify-")
    try:
        maya_fixtures = FIXTURES_DIRECTORY / "maya"
        _copy_files_to_directory(maya_fixtures, temporary_directory)

        deliverable_content = (
            "# Documentation Index\n\n"
            "- api-notes.md: API documentation and authentication tokens\n"
            "- deploy.md: Deployment workflows for staging and production\n"
            "- setup.md: Environment installation instructions\n"
        )
        deliverable_path = Path(temporary_directory) / "docs-index.md"
        deliverable_path.write_text(deliverable_content)

        jail_environment = Jail(temporary_directory)
        verify_context = VerifyContext(
            jail=jail_environment,
            step_id=2,
            task=MAYA_TASK,
            prior_items={1: ["api-notes.md", "deploy.md", "setup.md"]},
        )

        verification_clause = (
            '"docs-index.md" has one entry for each document from step 1, '
            "with a one-line description of each"
        )
        step_result = verify.verify_step([verification_clause], verify_context)

        check("index verification passes on exact fixtures",
              step_result.ok,
              str([(check_item.verifier, check_item.detail) for check_item in step_result.checks]))
        check("verifier is indexes",
              any(check_item.verifier == "indexes" for check_item in step_result.checks),
              str([check_item.verifier for check_item in step_result.checks]))
    finally:
        shutil.rmtree(temporary_directory)


if __name__ == "__main__":
    test_functions = [
        value for key, value in sorted(globals().items())
        if key.startswith("t_") and callable(value)
    ]
    for test_function in test_functions:
        test_function()
    print(f"\nAll {len(test_functions)} November 2 verification tests passed.")
