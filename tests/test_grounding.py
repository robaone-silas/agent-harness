#!/usr/bin/env python3
"""Tests for the grounding gate (v0.8.1).

Field failure: a plan step wrote its deliverable without ever reading the
previous step's stored output (the run record for step 2 showed no
read_file of step-01.md), and produced a one-line placeholder. The plan
frame tells the model to read its source first, but advice is ignorable.
The gate makes it checkable: when a step answers DONE and no tool call in
its sub-run touched a prior step's output file, the harness pushes back
once, inside the same run, naming the exact file. A second DONE (with a
reason) is accepted: some steps genuinely are independent.

loop.run takes the gate as a generic done_gate(steps) -> str | None hook;
the planner builds the plan-specific one.

Run: python3 tests/test_grounding.py
"""
import re
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness import loop, planner
from harness.config import Config

PLAN2 = """Feature: Grounding test
  Scenario: Step 1 - List the files
    When List the files in the workspace
    Then the list is recorded
  Scenario: Step 2 - Write the summary
    When Write summary.txt based on the file list
    Then "summary.txt" exists
"""

GATE_MARK = "without having read"


def make_cfg(**kw):
    d = tempfile.mkdtemp(prefix="grounding-test-")
    base = dict(workspace=d, max_steps=12, step_max_steps=8,
                max_consecutive_errors=3)
    base.update(kw)
    return Config(**base), d


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        raise AssertionError(name)


# ------------------------------------------------------------- loop-level

def t_gate_blocks_first_done_then_accepts():
    cfg, d = make_cfg()
    try:
        Path(d, "src.txt").write_text("data")
        seen_pushback = []

        def gate(steps):
            if any(s.tool == "read_file" for s in steps):
                return None
            return "HARNESS: READ src.txt FIRST"

        responses = iter([
            {"role": "assistant", "content": "DONE: finished."},
            {"role": "assistant", "content": "Reading.",
             "tool_calls": [{"function": {"name": "read_file",
                                          "arguments": {"path": "src.txt"}}}]},
            {"role": "assistant", "content": "DONE: now I have read it."},
        ])

        def fake(messages, tool_defs):
            if any(GATE_MARK_TEXT in (m.get("content") or "") for m in messages):
                seen_pushback.append(1)
            try:
                return next(responses)
            except StopIteration:
                return {"role": "assistant", "content": "SUMMARY: stopped."}

        GATE_MARK_TEXT = "READ src.txt FIRST"
        r = loop.run("do it", cfg, chat_fn=fake, done_gate=gate)
        check("gated run completes", r.status == "done", r.status)
        check("final answer is the post-read DONE",
              r.answer == "DONE: now I have read it.", r.answer)
        check("pushback was fed back", len(seen_pushback) >= 1)
        check("gate produced a nudge step",
              any(s.path == "nudge" for s in r.steps))
    finally:
        shutil.rmtree(d)


def t_gate_capped_at_two_pushbacks():
    # Gate v2 (2026-10-02): the gate is consulted on every DONE until it
    # clears, but the loop caps pushbacks at two per run, so a gate that
    # never clears still cannot imprison the run.
    cfg, d = make_cfg()
    try:
        consultations = {"n": 0}

        def gate(steps):
            consultations["n"] += 1
            if consultations["n"] <= 2:
                return "HARNESS: ALWAYS PUSH"
            return None

        responses = iter([
            {"role": "assistant", "content": "DONE: first."},
            {"role": "assistant", "content": "DONE: second."},
            {"role": "assistant", "content": "DONE: third, accepted."},
        ])

        def fake(messages, tool_defs):
            try:
                return next(responses)
            except StopIteration:
                return {"role": "assistant", "content": "SUMMARY: stopped."}

        r = loop.run("do it", cfg, chat_fn=fake, done_gate=gate)
        check("run completes after the cap", r.status == "done", r.status)
        check("answer is the third DONE",
              r.answer == "DONE: third, accepted.", r.answer)
        nudges = [s for s in r.steps if s.path == "nudge"]
        check("exactly two gate nudges", len(nudges) == 2, str(len(nudges)))
    finally:
        shutil.rmtree(d)


def t_no_gate_no_change():
    cfg, d = make_cfg()
    try:
        def fake(messages, tool_defs):
            return {"role": "assistant", "content": "DONE: immediate."}
        r = loop.run("do it", cfg, chat_fn=fake)
        check("ungated DONE accepted at once", r.status == "done" and not
              any(s.path == "nudge" for s in r.steps))
    finally:
        shutil.rmtree(d)


# ---------------------------------------------------------- planner-level

class Step2Actor:
    """Stateful fake for PLAN2. Step 1 lists; step 2 behaves per `mode`:
    'reads'   reads step-01.md (path taken from the pushback or the outline)
              before writing;
    'never'   writes and DONEs, takes the pushback, re-DONEs with a reason;
    'cat'     reads the step file via exec cat, then writes.
    Records every tool call it makes and every pushback it receives."""

    def __init__(self, d, mode):
        self.d = d
        self.mode = mode
        self.calls = []       # (tool, args-dict)
        self.pushback_seen = False
        self.step2_done_attempts = 0
        self.wrote = False
        self.read = False

    def __call__(self, messages, tool_defs):
        if tool_defs is None and "Break the task" in messages[0]["content"]:
            return {"role": "assistant", "content": PLAN2}
        if tool_defs is None:
            return {"role": "assistant", "content": "DONE: ok."}
        text = "\n".join(m.get("content") or "" for m in messages)
        task_msg = messages[1]["content"] if len(messages) > 1 else ""
        if GATE_MARK in text:
            self.pushback_seen = True
        if "You are executing step 1" in task_msg:
            if not any(c[0] == "list_dir" for c in self.calls):
                self.calls.append(("list_dir", {"path": "."}))
                return {"role": "assistant", "content": "Listing.",
                        "tool_calls": [{"function": {"name": "list_dir",
                                                     "arguments": {"path": "."}}}]}
            return {"role": "assistant", "content": "DONE: listed."}
        # Step 2.
        m = re.search(r"\.harness/runs/\S+/step-01\.md", task_msg)
        step_path = m.group(0) if m else None
        if self.mode == "reads" and not self.read and step_path:
            self.read = True
            self.calls.append(("read_file", {"path": step_path}))
            return {"role": "assistant", "content": "Reading source.",
                    "tool_calls": [{"function": {"name": "read_file",
                                                 "arguments": {"path": step_path}}}]}
        if self.mode == "cat" and not self.read and step_path:
            self.read = True
            cmd = f"cat {step_path}"
            self.calls.append(("exec", {"command": cmd}))
            return {"role": "assistant", "content": "Reading source via cat.",
                    "tool_calls": [{"function": {"name": "exec",
                                                 "arguments": {"command": cmd}}}]}
        if not self.wrote:
            self.wrote = True
            self.calls.append(("write_file", {"path": "summary.txt"}))
            return {"role": "assistant", "content": "Writing summary.",
                    "tool_calls": [{"function": {"name": "write_file",
                                                 "arguments": {"path": "summary.txt",
                                                               "content": "grounded summary"}}}]}
        self.step2_done_attempts += 1
        return {"role": "assistant", "content": "DONE: summary written."}


def run_plan2(mode):
    cfg, d = make_cfg()
    Path(d, "alpha.txt").write_text("a")
    actor = Step2Actor(d, mode)
    events = []
    r = planner.run_planned("summarize the files", cfg, chat_fn=actor,
                            on_event=lambda k, *a: events.append((k, a)))
    sub_steps = [a[1] for k, a in events if k == "step_sub"]
    return r, actor, sub_steps, d


def t_step2_that_never_reads_gets_pushed_back_and_uses_escape_hatch():
    cfg, d = make_cfg()
    try:
        Path(d, "alpha.txt").write_text("a")
        actor = Step2Actor(d, "never")
        events = []
        r = planner.run_planned("summarize the files", cfg, chat_fn=actor,
                                on_event=lambda k, *a: events.append((k, a)))
        check("run completes via escape hatch", r.status == "done", r.status)
        check("step 2 done", r.steps[1].status == "done")
        check("pushback reached the model", actor.pushback_seen)
        # Gate v2: the gate may push twice before its cap, so a model that
        # neither reads nor explains needs three DONEs (two pushes, then
        # the cap accepts). Under gate v1 this was two.
        check("step 2 needed three DONE attempts",
              actor.step2_done_attempts == 3, str(actor.step2_done_attempts))
        sub_steps = [a[1] for k, a in events if k == "step_sub"]
        check("gate nudge recorded in the sub-run",
              any(s.path == "nudge" for s in sub_steps))
        latest = (Path(d) / ".harness" / "latest.txt").read_text().strip()
        rec = (Path(d) / ".harness" / "runs" / latest / "step-02.md").read_text()
        check("record confirms step 2 never read the source",
              "read_file" not in rec, rec[:400])
    finally:
        shutil.rmtree(d)


def t_step2_that_reads_first_gets_no_pushback():
    r, actor, sub_steps, d = run_plan2("reads")
    try:
        check("run done", r.status == "done", r.status)
        check("actor read the step file",
              any(c[0] == "read_file" and "step-01.md" in str(c[1])
                  for c in actor.calls), str(actor.calls))
        nudges = [s for s in sub_steps if s.path == "nudge"]
        check("no nudges at all in a grounded run", len(nudges) == 0,
              str([(s.n, s.path) for s in sub_steps]))
    finally:
        shutil.rmtree(d)


def t_step2_reading_via_exec_cat_counts_as_grounded():
    r, actor, sub_steps, d = run_plan2("cat")
    try:
        check("run done", r.status == "done", r.status)
        nudges = [s for s in sub_steps if s.path == "nudge"]
        check("no gate nudge when source read via exec",
              len(nudges) == 0, str([(s.n, s.path) for s in sub_steps]))
    finally:
        shutil.rmtree(d)


def t_gate_second_stage_read_but_changed_nothing():
    # The exact sequence from Ansel's field record (run 20261002-192409):
    # write the stub, DONE (stage 1: no read yet), read the source, DONE
    # again with nothing changed. Gate v2 must fire its second stage there:
    # "you read it but changed nothing." Only then: rewrite, DONE, accepted.
    cfg, d = make_cfg()
    try:
        Path(d, "alpha.txt").write_text("a")
        seen_marks = set()
        state = {"task_seen": None, "phase": 0}
        STAGE2_MARK = "have not changed anything"

        def fake(messages, tool_defs):
            if tool_defs is None and "Break the task" in messages[0]["content"]:
                return {"role": "assistant", "content": PLAN2}
            if tool_defs is None:
                return {"role": "assistant", "content": "DONE: ok."}
            text = "\n".join(m.get("content") or "" for m in messages)
            if GATE_MARK in text:
                seen_marks.add("stage1")
            if STAGE2_MARK in text:
                seen_marks.add("stage2")
            task_msg = messages[1]["content"] if len(messages) > 1 else ""
            if task_msg != state["task_seen"]:
                state["task_seen"] = task_msg
                state["phase"] = 0
            phase = state["phase"]
            state["phase"] += 1
            if "You are executing step 1" in task_msg:
                if phase == 0:
                    return {"role": "assistant", "content": "Listing.",
                            "tool_calls": [{"function": {"name": "list_dir",
                                                         "arguments": {"path": "."}}}]}
                return {"role": "assistant", "content": "DONE: listed."}
            m = re.search(r"\.harness/runs/\S+/step-01\.md", task_msg)
            script = [
                {"role": "assistant", "content": "Writing stub.",
                 "tool_calls": [{"function": {"name": "write_file",
                                              "arguments": {"path": "summary.txt",
                                                            "content": "stub"}}}]},
                {"role": "assistant", "content": "DONE: written."},
                {"role": "assistant", "content": "Reading source.",
                 "tool_calls": [{"function": {"name": "read_file",
                                              "arguments": {"path": m.group(0)}}}]},
                {"role": "assistant", "content": "DONE: read it."},
                {"role": "assistant", "content": "Rewriting for real.",
                 "tool_calls": [{"function": {"name": "write_file",
                                              "arguments": {"path": "summary.txt",
                                                            "content": "real summary of alpha.txt"}}}]},
                {"role": "assistant", "content": "DONE: redone."},
            ]
            return script[min(phase, len(script) - 1)]

        r = planner.run_planned("summarize the files", cfg, chat_fn=fake)
        check("staged run completes", r.status == "done", r.status)
        check("stage 1 pushback seen", "stage1" in seen_marks, str(seen_marks))
        check("stage 2 pushback seen", "stage2" in seen_marks, str(seen_marks))
        check("final file is the rewrite",
              Path(d, "summary.txt").read_text() == "real summary of alpha.txt")
    finally:
        shutil.rmtree(d)


def t_step1_has_no_gate():
    r, actor, sub_steps, d = run_plan2("reads")
    try:
        # Nudges are already asserted zero overall; step 1 in particular has
        # no prior outputs, so a gate there would be a bug.
        check("run done (step1 ungated)", r.status == "done", r.status)
        check("step 1 status done", r.steps[0].status == "done")
    finally:
        shutil.rmtree(d)


def t_gate_texts_warn_against_reading_own_step_file():
    # Field failure #4 (2026-10-03): pushed to read a prior step's record,
    # the model read its OWN step's record instead (.harness/runs/.../
    # step-03.md), which does not exist until the step finishes. The read
    # errored, the DONE-after-error pushback fired, the model retried the
    # same read into the repeat breaker, and the turn budget ran out: the
    # step failed with its work already done on disk. Both gate stages
    # direct the model at files, so both must say plainly that the step's
    # own output file is written afterwards, does not exist yet, and is
    # not a source to read.
    done1 = planner.PlanStep(id=1, instruction="List files",
                             done_when="the list is recorded", status="done",
                             output_path=".harness/runs/X/step-01.md")
    cur = planner.PlanStep(id=2, instruction="Write summary.txt",
                           done_when='"summary.txt" exists',
                           output_path=".harness/runs/X/step-02.md")
    gate = planner._grounding_gate(cur, [done1, cur])
    check("gate built for a step with priors", gate is not None)
    stage1 = gate([])
    low1 = (stage1 or "").lower()
    check("stage 1 warns about the step's own file",
          "your own step" in low1 and "does not exist yet" in low1,
          stage1 or "")
    read = loop.Step(n=1, thought="", tool="read_file",
                     args={"path": ".harness/runs/X/step-01.md"},
                     result="contents", path="native")
    stage2 = gate([read])
    check("stage 2 fires after a bare read", stage2 is not None)
    low2 = (stage2 or "").lower()
    check("stage 2 warns about the step's own file",
          "your own step" in low2 and "does not exist yet" in low2,
          stage2 or "")


if __name__ == "__main__":
    for fn in [t_gate_blocks_first_done_then_accepts, t_gate_capped_at_two_pushbacks,
               t_no_gate_no_change,
               t_step2_that_never_reads_gets_pushed_back_and_uses_escape_hatch,
               t_step2_that_reads_first_gets_no_pushback,
               t_step2_reading_via_exec_cat_counts_as_grounded,
               t_gate_second_stage_read_but_changed_nothing,
               t_step1_has_no_gate,
               t_gate_texts_warn_against_reading_own_step_file]:
        fn()
    print("\nAll grounding gate tests passed.")
