#!/usr/bin/env python3
"""Tests for the discovery unexamined-file annotation (issue #6, step 3).

Field motivation (2026-10-03, Tomas Rivera's persona run): the Tier 2
discovery pass read four of the five files in his workspace and never
mentioned water-heater-manual.txt. The findings handed to the final
planner faithfully copied that four-file sample, and no stage compared
the sample against the workspace itself, so the final plan called four
files "all house documents". The fix is deterministic and lives at the
handoff: compare the root workspace files with the files accounted for
by the discovery plan and its records (listed, read, searched, named,
or explicitly excluded), and append an unexamined workspace files
section to the findings the final planner receives.

Tomas's saved discovery run is the regression fixture: workspace of
five files, one discovery step whose record holds four read_file
calls, and water-heater-manual.txt absent from plan and record alike.

Run: python3 tests/test_discovery_unexamined.py
"""
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness import planner
from harness.config import Config

TOMAS_FILES = {
    "house-insurance-policy.txt": "House insurance policy: renews in May 2027.",
    "property-tax-bill.txt": "Property tax bill: second installment due September.",
    "roof-warranty.txt": "Roof warranty: shingles covered through 2031.",
    "furnace-receipt.txt": "Furnace receipt: installed in March 2019.",
    "water-heater-manual.txt": "Water heater manual: flush the tank annually.",
}

TOMAS_READ = ["house-insurance-policy.txt", "property-tax-bill.txt",
              "roof-warranty.txt", "furnace-receipt.txt"]

TOMAS_RECORD = "# Step 1\n\n## Instruction\n" + "\n".join(
    f"I read `{name}`" for name in TOMAS_READ) + "\n\n### Tool calls\n" + "\n".join(
    f'#### read_file {{"path": "{name}"}}\n' for name in TOMAS_READ)


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        raise AssertionError(name)


def make_ws(files, run_id="20990101-000000", record=TOMAS_RECORD):
    d = tempfile.mkdtemp(prefix="unexamined-")
    for name, content in files.items():
        p = Path(d, name)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    if run_id and record is not None:
        run_dir = Path(d, ".harness", "runs", run_id)
        run_dir.mkdir(parents=True)
        (run_dir / "step-01.md").write_text(record)
    return d


def tomas_steps():
    return [planner.PlanStep(
        id=1,
        instruction="\n".join(f"I read `{name}`" for name in TOMAS_READ),
        done_when="\n".join(f"I know the details of {name}" for name in TOMAS_READ))]


def section_of(findings):
    idx = findings.find("Unexamined workspace files")
    return findings[idx:] if idx >= 0 else ""


def t_tomas_fixture_names_the_omitted_file():
    d = make_ws(TOMAS_FILES)
    try:
        missing = planner.unexamined_workspace_files(
            d, discovery_steps=tomas_steps(), findings=TOMAS_RECORD)
        check("Tomas fixture: exactly water-heater-manual.txt is unexamined",
              missing == ["water-heater-manual.txt"], str(missing))
    finally:
        shutil.rmtree(d)


def t_findings_text_appends_the_section():
    d = make_ws(TOMAS_FILES)
    try:
        findings = planner.discovery_findings_text(
            d, "20990101-000000", 1, discovery_steps=tomas_steps())
        section = section_of(findings)
        check("findings carry an unexamined section", bool(section), findings[-400:])
        check("section names water-heater-manual.txt",
              "water-heater-manual.txt" in section, section)
        check("section does not name accounted files as unexamined",
              all(name not in section for name in TOMAS_READ), section)
        check("findings still carry the discovery record itself",
              "house-insurance-policy.txt" in findings)
    finally:
        shutil.rmtree(d)


def t_listing_step_accounts_for_everything():
    record = ("# Step 1\n\n### Tool calls\n\n#### list_dir {\"path\": \".\"}\n\n"
              "Result:\n\n```\n" + "\n".join(sorted(TOMAS_FILES)) + "\n```\n")
    d = make_ws(TOMAS_FILES, record=record)
    try:
        steps = [planner.PlanStep(id=1, instruction="I list the files in the workspace",
                                  done_when="the files are listed")]
        missing = planner.unexamined_workspace_files(
            d, discovery_steps=steps, findings=record)
        check("a listing that names every file leaves nothing unexamined",
              missing == [], str(missing))
        findings = planner.discovery_findings_text(
            d, "20990101-000000", 1, discovery_steps=steps)
        section = section_of(findings)
        check("section reports all files accounted for",
              bool(section) and "water-heater-manual.txt" not in section, section)
    finally:
        shutil.rmtree(d)


def t_named_searched_and_excluded_files_count_as_accounted():
    d = make_ws(TOMAS_FILES)
    try:
        steps = [planner.PlanStep(
            id=1,
            instruction=("I search the files for warranty terms and read "
                         "`roof-warranty.txt`; water-heater-manual.txt is "
                         "explicitly excluded from this pass"),
            done_when="the warranty terms are known")]
        findings = ("read_file house-insurance-policy.txt\n"
                    "read_file property-tax-bill.txt\n"
                    "read_file furnace-receipt.txt\n")
        missing = planner.unexamined_workspace_files(
            d, discovery_steps=steps, findings=findings)
        check("named, searched, read, and excluded files are all accounted",
              missing == [], str(missing))
    finally:
        shutil.rmtree(d)


def t_only_root_files_count():
    files = dict(TOMAS_FILES)
    d = make_ws(files)
    try:
        Path(d, "Archive", "inner-secret.txt").parent.mkdir(parents=True, exist_ok=True)
        Path(d, "Archive", "inner-secret.txt").write_text("nested")
        missing = planner.unexamined_workspace_files(
            d, discovery_steps=tomas_steps(), findings=TOMAS_RECORD)
        check("folders and nested files are not root workspace files",
              missing == ["water-heater-manual.txt"], str(missing))
        check("missing workspace yields no unexamined files",
              planner.unexamined_workspace_files(str(Path(d, "nope"))) == [])
        check("no workspace yields no unexamined files",
              planner.unexamined_workspace_files(None) == [])
        check("no run id still yields no findings",
              planner.discovery_findings_text(d, "", 3) == "")
    finally:
        shutil.rmtree(d)


def t_plan_with_discovery_hands_the_section_to_the_final_planner():
    d = tempfile.mkdtemp(prefix="unexamined-e2e-")
    try:
        Path(d, "budget.csv").write_text("cat,jan\ngroceries,10\n")
        Path(d, "notes.txt").write_text("hello")
        Path(d, "mystery.txt").write_text("unread")
        cfg = Config(workspace=d, max_steps=12, step_max_steps=8,
                     max_consecutive_errors=3)
        seen = {"replan_prompt": None}
        state = {"task_seen": None, "phase": 0}
        discovery_plan = """Feature: Discovery pass
  Scenario: Step 1 - Read the budget
    When I read budget.csv
    Then its contents are known
"""
        final_plan = """Feature: Organize for real
  Scenario: Step 1 - List files
    When I list the files in the workspace
    Then the files are listed
  Scenario: Step 2 - Write the summary
    When I write summary.txt about budget.csv
    Then "summary.txt" exists
"""

        def fake(messages, tool_defs):
            first = messages[0]["content"] if messages else ""
            if tool_defs is None and "You are planning a read-only discovery pass" in first:
                return {"role": "assistant", "content": discovery_plan}
            if tool_defs is None and "Plan execution ended" in first:
                return {"role": "assistant", "content": "DONE: discovered."}
            if tool_defs is None and "You are a planner" in first:
                seen["replan_prompt"] = first
                return {"role": "assistant", "content": final_plan}
            task_msg = messages[1]["content"] if len(messages) > 1 else ""
            if task_msg != state["task_seen"]:
                state["task_seen"] = task_msg
                state["phase"] = 0
            phase = state["phase"]
            state["phase"] += 1
            if phase == 0:
                return {"role": "assistant", "content": "Reading.",
                        "tool_calls": [{"function": {"name": "read_file",
                                                     "arguments": {"path": "budget.csv"}}}]}
            return {"role": "assistant", "content": "DONE: read."}

        text, steps, info = planner.plan_with_discovery(
            "organize the files", cfg, chat_fn=fake)
        check("final plan returned", text is not None and len(steps) == 2)
        section = section_of(info["findings"])
        check("handoff findings name the files discovery never touched",
              "notes.txt" in section and "mystery.txt" in section,
              info["findings"][-500:])
        check("accounted file is not in the unexamined section",
              "budget.csv" not in section, section)
        check("replan prompt carries the unexamined section",
              seen["replan_prompt"] is not None
              and "Unexamined workspace files" in seen["replan_prompt"]
              and "mystery.txt" in seen["replan_prompt"])
    finally:
        shutil.rmtree(d)


if __name__ == "__main__":
    for fn in [t_tomas_fixture_names_the_omitted_file,
               t_findings_text_appends_the_section,
               t_listing_step_accounts_for_everything,
               t_named_searched_and_excluded_files_count_as_accounted,
               t_only_root_files_count,
               t_plan_with_discovery_hands_the_section_to_the_final_planner]:
        fn()
    print("\nAll discovery unexamined-file tests passed.")
