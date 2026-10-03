#!/usr/bin/env python3
"""Tests for per-step Intent lines (v0.8.1).

Field failure behind it (index task, 2026-10-03): the task asked for
"every file with its name and a one-line description of what it
actually contains", and the plan silently traded that for "add the
content": six steps appended raw file contents, four of the six
entries carried no filename at all, and no harness layer could see
the loss, because a step's purpose existed nowhere as data. Rules in
the planner prompt are advice; the Gherkin format is enforced. So
intent gets a slot in the format: an optional `Intent:` line per
scenario, parsed, rendered, carried on PlanStep, shown to the
executor in its step prompt, taught to the planner by rule and by an
exemplar whose task carries a qualitative spec, and watched by a
lint warning when the task's spec words survive in no step at all.

Run: python3 tests/test_intent.py
"""
import re
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness import gherkin, planner, planlint
from harness.config import Config

PLAN_INTENT = """Feature: Reading guide
  Scenario: Step 1 - List the articles
    Intent: know exactly which articles the guide must cover
    When I list the files in the folder
    Then the output lists the article files
  Scenario: Step 2 - Write the guide
    Intent: each entry gives the article's title and a one-sentence summary of its argument
    When I write guide.md with one entry per article from step 1
    Then "guide.md" covers the files from step 1
"""

PLAN_PLAIN = """Feature: Plain plan
  Scenario: Step 1 - List files
    When List the files in the workspace
    Then the list is recorded
"""


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        raise AssertionError(name)


# ------------------------------------------------------------- parser

def t_intent_is_parsed_per_scenario():
    plan, err = gherkin.parse_feature(PLAN_INTENT)
    check("plan parses", plan is not None, err)
    check("step 1 intent captured",
          plan.steps[0].intent == "know exactly which articles the guide must cover",
          repr(plan.steps[0].intent))
    check("step 2 intent captured",
          plan.steps[1].intent.startswith("each entry gives"), repr(plan.steps[1].intent))


def t_intent_is_optional():
    plan, err = gherkin.parse_feature(PLAN_PLAIN)
    check("plan without intent parses", plan is not None, err)
    check("intent defaults to empty", plan.steps[0].intent == "")


def t_intent_survives_render_round_trip():
    plan, err = gherkin.parse_feature(PLAN_INTENT)
    text = gherkin.render_feature(plan.title, plan.steps)
    check("render includes the Intent line",
          "Intent: each entry gives the article's title" in text, text)
    plan2, err2 = gherkin.parse_feature(text)
    check("render re-parses", plan2 is not None, err2)
    check("intent identical after round trip",
          plan2.steps[1].intent == plan.steps[1].intent)


def t_intent_validation():
    dup = PLAN_INTENT.replace("    When I list",
                              "    Intent: a second one\n    When I list")
    plan, err = gherkin.parse_feature(dup)
    check("duplicate Intent in one scenario rejected",
          plan is None and "Intent" in err, err)
    empty = PLAN_INTENT.replace(
        "Intent: know exactly which articles the guide must cover", "Intent:")
    plan, err = gherkin.parse_feature(empty)
    check("empty Intent rejected", plan is None and "Intent" in err, err)
    ph = PLAN_INTENT.replace("the guide must cover", "the <thing> must cover")
    plan, err = gherkin.parse_feature(ph)
    check("placeholder in Intent rejected", plan is None, err)


def t_plan_steps_carry_intent():
    plan, err = gherkin.parse_feature(PLAN_INTENT)
    steps = planner.to_plan_steps(plan)
    check("PlanStep.intent carried",
          steps[1].intent.startswith("each entry gives"), repr(steps[1].intent))
    check("PlanStep.intent defaults empty",
          planner.to_plan_steps(gherkin.parse_feature(PLAN_PLAIN)[0])[0].intent == "")


# ------------------------------------------------------------- prompt

def t_planner_prompt_teaches_intent():
    p = planner.build_planner_prompt("index these files with descriptions",
                                     ["list_dir", "write_file"])
    check("format skeleton shows Intent", "Intent:" in p, p[:400])
    check("rule says a description is not the content",
          "a description is not the content" in p, p[:600])
    check("exemplar with a qualitative spec present",
          "one-sentence summary" in p, p[:600])
    check("exemplar's Intent lines present in the prompt",
          p.count("Intent:") >= 3, str(p.count("Intent:")))


# --------------------------------------------------------- step frame

def t_step_prompt_surfaces_intent():
    d = tempfile.mkdtemp(prefix="intent-frame-")
    try:
        Path(d, "alpha.txt").write_text("alpha body")
        cfg = Config(workspace=d, max_steps=12, step_max_steps=8,
                     max_consecutive_errors=3)
        prompts = []

        def fake(messages, tool_defs):
            if tool_defs is None and "Break the task" in messages[0]["content"]:
                return {"role": "assistant", "content": PLAN_INTENT}
            if tool_defs is None:
                return {"role": "assistant", "content": "DONE: ok."}
            task_msg = messages[1]["content"] if len(messages) > 1 else ""
            if "You are executing step" in task_msg and task_msg not in prompts:
                prompts.append(task_msg)
            if "You are executing step 1" in task_msg:
                if not getattr(fake, "_listed", False):
                    fake._listed = True
                    return {"role": "assistant", "content": "Listing.",
                            "tool_calls": [{"function": {"name": "list_dir",
                                                         "arguments": {"path": "."}}}]}
                return {"role": "assistant", "content": "DONE: listed."}
            if "You are executing step 2" in task_msg:
                m = re.search(r"\.harness/runs/\S+/step-01\.md", task_msg)
                if not getattr(fake, "_acted", False):
                    fake._acted = True
                    if m:
                        return {"role": "assistant", "content": "Reading.",
                                "tool_calls": [{"function": {"name": "read_file",
                                                             "arguments": {"path": m.group(0)}}}]}
                if not getattr(fake, "_wrote", False):
                    fake._wrote = True
                    return {"role": "assistant", "content": "Writing.",
                            "tool_calls": [{"function": {"name": "write_file",
                                                         "arguments": {"path": "guide.md",
                                                                       "content": "alpha.txt: summary"}}}]}
                return {"role": "assistant", "content": "DONE: done."}
            return {"role": "assistant", "content": "DONE: done."}

        r = planner.run_planned("make the guide", cfg, chat_fn=fake)
        check("run completes", r.status == "done", r.status)
        step2 = next((p for p in prompts if "executing step 2" in p), "")
        check("step 2 prompt carries its intent",
              "one-sentence summary of its argument" in step2, step2[:600])
        check("intent labelled as intent in the prompt",
              "Intent" in step2, step2[:600])
        step1 = next((p for p in prompts if "executing step 1" in p), "")
        check("step 1 prompt carries its own intent",
              "know exactly which articles" in step1, step1[:600])
    finally:
        shutil.rmtree(d)


# ---------------------------------------------------------------- lint

TASK_SPEC = ("Look at the files in this workspace and build an index of "
             "them, listing every file with its name and a one-line "
             "description of what it actually contains.")


def _steps(plan_text):
    plan, err = gherkin.parse_feature(plan_text)
    assert plan is not None, err
    return planner.to_plan_steps(plan)


def t_lint_flags_lost_intent():
    # Today's plan, verbatim shape: no step mentions "description".
    tiled = """Feature: Build an index of workspace files
  Scenario: Step 1 - Index belize file
    When I read the content of "belize-trip-ideas.txt" and add it to index.md
    Then "index.md" contains the file "belize-trip-ideas.txt" and its content
"""
    findings = planlint.lint_plan(TASK_SPEC, _steps(tiled))
    drift = [f for f in findings if f.code == "intent_drift"]
    check("one intent_drift finding", len(drift) == 1,
          str([f.as_dict() for f in findings]))
    check("finding is a warning", drift and drift[0].level == "warn")
    check("finding names the lost word",
          drift and "description" in drift[0].detail, drift[0].detail if drift else "")


def t_lint_quiet_when_intent_preserved():
    findings = planlint.lint_plan(TASK_SPEC, _steps(PLAN_INTENT.replace(
        "one-sentence summary of its argument",
        "one-line description of what it contains")))
    check("no intent_drift when a step carries the spec",
          not any(f.code == "intent_drift" for f in findings),
          str([f.as_dict() for f in findings]))
    plain_task = "organize the files in this folder by category"
    findings2 = planlint.lint_plan(plain_task, _steps(PLAN_PLAIN))
    check("no intent_drift when the task has no spec words",
          not any(f.code == "intent_drift" for f in findings2))


if __name__ == "__main__":
    for fn in [t_intent_is_parsed_per_scenario, t_intent_is_optional,
               t_intent_survives_render_round_trip, t_intent_validation,
               t_plan_steps_carry_intent, t_planner_prompt_teaches_intent,
               t_step_prompt_surfaces_intent, t_lint_flags_lost_intent,
               t_lint_quiet_when_intent_preserved]:
        fn()
    print("\nAll intent tests passed.")
