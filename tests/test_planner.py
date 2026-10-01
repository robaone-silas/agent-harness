#!/usr/bin/env python3
"""Tests for plan-then-execute mode. Scripted fake model, no Ollama needed.

Run: python3 tests/test_planner.py
"""
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness import gherkin, planner
from harness.config import Config

PLAN_GHERKIN = """Feature: Do things
  Scenario: Step 1 - Write the file
    When Write a.txt containing x
    Then a.txt exists
  Scenario: Step 2 - Read it back
    When Run: cat a.txt
    Then output shows x
"""


def make_cfg(**kw):
    d = tempfile.mkdtemp(prefix="planner-test-")
    base = dict(workspace=d, max_steps=12, step_max_steps=6,
                max_consecutive_errors=3)
    base.update(kw)
    return Config(**base), d


def scripted(responses):
    calls = {"n": 0}
    it = iter(responses)

    def fake(messages, tool_defs):
        calls["n"] += 1
        try:
            return next(it)
        except StopIteration:
            raise AssertionError("fake model ran out of scripted responses")
    fake.calls = calls
    return fake


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        raise AssertionError(name)


def t_plan_valid():
    cfg, d = make_cfg()
    fake = scripted([{"role": "assistant", "content": PLAN_GHERKIN}])
    text, steps = planner.request_plan("do things", ["exec", "write_file"], fake, cfg)
    check("plan valid", text is not None and len(steps) == 2,
          steps if text is None else "")
    check("plan step fields", steps[0].instruction.startswith("Write a.txt"))
    check("plan text is gherkin", text.strip().startswith("Feature:"))
    check("plan one call", fake.calls["n"] == 1)
    shutil.rmtree(d)


def t_plan_retry():
    cfg, d = make_cfg()
    fake = scripted([
        {"role": "assistant", "content": "Sure, here is my plan: eventually..."},
        {"role": "assistant", "content": "```gherkin\n" + PLAN_GHERKIN + "\n```"},
    ])
    text, steps = planner.request_plan("do things", ["exec"], fake, cfg)
    check("plan retry works", text is not None and len(steps) == 2,
          steps if text is None else "")
    check("plan retry took 2 calls", fake.calls["n"] == 2)
    shutil.rmtree(d)


def t_plan_failed():
    cfg, d = make_cfg()
    fake = scripted([
        {"role": "assistant", "content": "no gherkin here"},
        {"role": "assistant", "content": "still no gherkin"},
    ])
    text, err = planner.request_plan("do things", ["exec"], fake, cfg)
    check("plan failed", text is None and err, err)
    shutil.rmtree(d)


def t_gherkin_rejects():
    bad_no_then = "Feature: x\n  Scenario: s\n    When do it\n"
    plan, err = gherkin.parse_feature(bad_no_then)
    check("reject missing Then", plan is None and "Then" in err, err)
    bad_no_when = "Feature: x\n  Scenario: s\n    Then it is done\n"
    plan, err = gherkin.parse_feature(bad_no_when)
    check("reject missing When", plan is None and "When" in err, err)
    bad_ph = ("Feature: x\n  Scenario: s\n    When write <thing> to f.txt\n"
              "    Then f.txt exists\n")
    plan, err = gherkin.parse_feature(bad_ph)
    check("reject placeholder", plan is None and "placeholder" in err, err)
    plan, err = gherkin.parse_feature("Feature: nothing here\n")
    check("reject no scenario", plan is None and "Scenario" in err, err)
    many = "Feature: x\n" + "".join(
        f"  Scenario: s{i}\n    When do {i}\n    Then done {i}\n" for i in range(9))
    plan, err = gherkin.parse_feature(many)
    check("reject too many", plan is None and "1-8" in err, err)


def t_planned_run_happy():
    cfg, d = make_cfg()
    fake = scripted([
        {"role": "assistant", "content": PLAN_GHERKIN},
        {"role": "assistant", "content": "Writing.",
         "tool_calls": [{"function": {"name": "write_file",
                                      "arguments": {"path": "a.txt", "content": "x"}}}]},
        {"role": "assistant", "content": "DONE: wrote a.txt."},
        {"role": "assistant", "content": "Running.",
         "tool_calls": [{"function": {"name": "exec",
                                      "arguments": {"command": "cat a.txt"}}}]},
        {"role": "assistant", "content": "DONE: output shows x."},
        {"role": "assistant", "content": "DONE: both steps complete."},
    ])
    events = []
    r = planner.run_planned("do the thing", cfg, chat_fn=fake,
                            on_event=lambda k, *a: events.append(k))
    check("planned status", r.status == "done", r.status)
    check("both steps done", [s.status for s in r.steps] == ["done", "done"])
    check("sequenced calls", fake.calls["n"] == 6, str(fake.calls["n"]))
    check("plan event", "plan" in events)
    check("step events", events.count("step_done") == 2)
    check("file written", Path(d, "a.txt").read_text() == "x")
    shutil.rmtree(d)


def t_step_failure_aborts():
    cfg, d = make_cfg()
    bad = {"role": "assistant", "content": "Trying.",
           "tool_calls": [{"function": {"name": "nope", "arguments": {}}}]}
    fake = scripted([
        {"role": "assistant", "content": PLAN_GHERKIN},
        bad, bad, bad,  # 3 consecutive errors -> sub-run error_limit
        {"role": "assistant", "content": "DONE: stopped early."},
    ])
    r = planner.run_planned("do the thing", cfg, chat_fn=fake)
    check("step_failed status", r.status == "step_failed", r.status)
    check("step1 failed", r.steps[0].status == "failed")
    check("step2 never started", r.steps[1].status == "pending")
    check("no extra calls", fake.calls["n"] == 6, str(fake.calls["n"]))
    shutil.rmtree(d)


def t_scoped_prompt_only_current_step():
    cfg, d = make_cfg()
    seen = []
    wrote = {"n": 0}

    def fake(messages, tool_defs):
        if tool_defs is None and "Break the task" in messages[0]["content"]:
            return {"role": "assistant", "content": PLAN_GHERKIN}
        if tool_defs is None:  # summary call
            return {"role": "assistant", "content": "DONE: ok."}
        content = messages[-1]["content"]
        if "You are executing step" in content:
            seen.append(content)
        # v0.7 verifies the Then in code: actually create the file.
        if "Write a.txt" in content and wrote["n"] == 0:
            wrote["n"] += 1
            return {"role": "assistant", "content": "Writing.",
                    "tool_calls": [{"function": {"name": "write_file",
                                                 "arguments": {"path": "a.txt",
                                                               "content": "x"}}}]}
        return {"role": "assistant", "content": "DONE: step done."}

    r = planner.run_planned("overall goal here", cfg, chat_fn=fake)
    check("planned status", r.status == "done", r.status)
    check("two scoped prompts", len(seen) == 2, str(len(seen)))
    main_part = seen[1].split("Results of previous steps")[0]
    check("step2 prompt scopes to step 2",
          "Run: cat a.txt" in main_part
          and "Write a.txt containing x" not in main_part, main_part[:200])
    check("step2 sees step1 result", "context only" in seen[1])
    shutil.rmtree(d)


if __name__ == "__main__":
    for fn in [t_plan_valid, t_plan_retry, t_plan_failed, t_gherkin_rejects,
               t_planned_run_happy, t_step_failure_aborts, t_scoped_prompt_only_current_step]:
        fn()
    print("\nAll planner tests passed.")
