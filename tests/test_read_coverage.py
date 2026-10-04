#!/usr/bin/env python3
"""Tests for the read-coverage gate stage (issue #17).

Field failure, 2026-10-04 final replay: Priya's compile step read five
of six source files, never read zeta-travel.txt, wrote a five-entry
index, and the run reported done. The grounding gate asked only whether
a prior step's record had been touched; the covers verifier asked only
whether the deliverable mentioned at least half the source items. No
stage compared the files the plan named against the files actually
read. The gate now has a coverage stage: when a step carries a covers
clause, the listing step's source items are compared against the files
read so far in the run (cumulative across steps, because step scope is
soft: an earlier gather step's reads count), and an unread source file
draws a bounded pushback naming it. Mentions are cheap; reads are the
evidence.

Run: python3 tests/test_read_coverage.py
"""
import re
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness import gherkin, planner
from harness.config import Config

LETTERS = ["a.txt", "b.txt", "c.txt"]

PLAN_DIRECT = """Feature: Index the letters
  Scenario: Step 1 - List the files
    When I list the files in the folder
    Then the output lists the files
  Scenario: Step 2 - Write the index
    When I read each file from step 1 and write index.md with one entry per file
    Then "index.md" covers the files from step 1
"""

PLAN_GATHER_THEN_WRITE = """Feature: Catalog the letters
  Scenario: Step 1 - List the files
    When I list the files in the folder
    Then the output lists the files
  Scenario: Step 2 - Read the first group
    When I read a.txt and b.txt from step 1 and note their contents
    Then the output notes the contents read
  Scenario: Step 3 - Read the last file
    When I read c.txt from step 1 and note its contents
    Then the output notes the contents read
  Scenario: Step 4 - Write the catalog
    When I write catalog.md with one entry per file from the notes in steps 2 and 3
    Then "catalog.md" covers the files from step 1
"""


def make_cfg(d, **kw):
    base = dict(workspace=d, max_steps=12, step_max_steps=6,
                max_consecutive_errors=3)
    base.update(kw)
    return Config(**base)


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        raise AssertionError(name)


def tool(name, args):
    return {"role": "assistant", "content": "",
            "tool_calls": [{"function": {"name": name, "arguments": args}}]}


def done(text="DONE: ok."):
    return {"role": "assistant", "content": text}


def record_of(task_msg, n):
    m = re.search(rf"\.harness/runs/\S+/step-0{n}\.md", task_msg)
    assert m, f"no step-{n} record path in prompt"
    return m.group(0)


def parse(text):
    plan, err = gherkin.parse_feature(text)
    assert plan is not None, err
    return planner.to_plan_steps(plan)


def run_scenario(plan_text, scripts):
    """scripts: {step_id: callable(phase, task_msg) -> response}."""
    d = tempfile.mkdtemp(prefix="read-coverage-")
    for name in LETTERS:
        Path(d, name).write_text(f"contents of {name}")
    state = {"task_seen": None, "phase": 0}
    seen = {"pushback": []}

    def fake(messages, tool_defs):
        if tool_defs is None:
            return done("DONE: summary.")
        for msg in messages:
            content = msg.get("content") or ""
            if "have not been read" in content and content not in seen["pushback"]:
                seen["pushback"].append(content)
        task_msg = messages[1]["content"] if len(messages) > 1 else ""
        if task_msg != state["task_seen"]:
            state["task_seen"] = task_msg
            state["phase"] = 0
        phase = state["phase"]
        state["phase"] += 1
        m = re.search(r"You are executing step (\d+)", task_msg)
        sid = int(m.group(1)) if m else 0
        fn = scripts.get(sid)
        if fn is None:
            return done("DONE: nothing scripted.")
        return fn(phase, task_msg)

    cfg = make_cfg(d)
    result = planner.execute_plan("index the letters", parse(plan_text),
                                  cfg, chat_fn=fake)
    return result, seen, d


def t_unread_source_draws_pushback_and_is_fixed():
    def step1(phase, task_msg):
        return [tool("list_dir", {"path": "."}), done("DONE: listed.")][min(phase, 1)]

    def step2(phase, task_msg):
        script = [
            tool("read_file", {"path": record_of(task_msg, 1)}),
            tool("read_file", {"path": "a.txt"}),
            tool("read_file", {"path": "b.txt"}),
            tool("write_file", {"path": "index.md",
                                "content": "a.txt: contents of a.txt\nb.txt: contents of b.txt\n"}),
            done("DONE: index written."),
            tool("read_file", {"path": "c.txt"}),
            tool("write_file", {"path": "index.md",
                                "content": "a.txt: contents of a.txt\nb.txt: contents of b.txt\nc.txt: contents of c.txt\n"}),
            done("DONE: index completed."),
        ]
        return script[min(phase, len(script) - 1)]

    result, seen, d = run_scenario(PLAN_DIRECT, {1: step1, 2: step2})
    try:
        named = [p for p in seen["pushback"] if "c.txt" in p]
        check("the coverage pushback names the unread file", len(named) >= 1,
              f"pushbacks seen: {len(seen['pushback'])}")
        check("run completes after the pushback", result.status == "done",
              result.status)
        text = Path(d, "index.md").read_text()
        check("the final index covers every file",
              all(n in text for n in LETTERS), text)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def t_gathered_reads_count_toward_coverage():
    def step1(phase, task_msg):
        return [tool("list_dir", {"path": "."}), done("DONE: listed.")][min(phase, 1)]

    def step2(phase, task_msg):
        script = [
            tool("read_file", {"path": record_of(task_msg, 1)}),
            tool("read_file", {"path": "a.txt"}),
            tool("read_file", {"path": "b.txt"}),
            done("DONE: noted."),
        ]
        return script[min(phase, len(script) - 1)]

    def step3(phase, task_msg):
        script = [
            tool("read_file", {"path": record_of(task_msg, 2)}),
            tool("read_file", {"path": "c.txt"}),
            done("DONE: noted."),
        ]
        return script[min(phase, len(script) - 1)]

    def step4(phase, task_msg):
        script = [
            tool("read_file", {"path": record_of(task_msg, 1)}),
            tool("write_file", {"path": "catalog.md",
                                "content": "a.txt: contents of a.txt\nb.txt: contents of b.txt\nc.txt: contents of c.txt\n"}),
            done("DONE: catalog written."),
        ]
        return script[min(phase, len(script) - 1)]

    result, seen, d = run_scenario(
        PLAN_GATHER_THEN_WRITE, {1: step1, 2: step2, 3: step3, 4: step4})
    try:
        check("no coverage pushback when earlier steps did the reading",
              len(seen["pushback"]) == 0,
              f"pushbacks: {seen['pushback']}")
        check("gather-then-write run completes", result.status == "done",
              result.status)
        text = Path(d, "catalog.md").read_text()
        check("the catalog covers every file",
              all(n in text for n in LETTERS), text)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def main():
    t_unread_source_draws_pushback_and_is_fixed()
    t_gathered_reads_count_toward_coverage()
    print("\nAll read-coverage gate tests passed.")


if __name__ == "__main__":
    main()
