"""Tests for the world-state override (Ansel's ruling, 2026-10-04).

The Tomas protocol question, settled: when a step's sub-run ends
without the DONE token (it narrates a summary instead, and the loop
ends it at the turn limit), the step is judged on the world, not on
the protocol. Verification runs exactly as it would after DONE; if
every clause passes, the step counts as done and the acceptance is
recorded (step.done_via_world_state, a step_done_world_state event),
never silent. If verification fails, the step hard-fails as before.
Boundaries: only a sub-run that ended at the turn limit with a real
final answer is eligible. A run that died on consecutive tool
errors, or whose final summary call itself failed, gets no
override: there is no finished work to judge, only a dead run.

Attested clauses count when they pass under the current rules
(including the issue #22 existence floor): a step ending in DONE
with an attested clause is already accepted on that basis, so the
same verification after a protocol miss stands in an identical
position. The token the model typed last adds no information
about the world.

Run: python3 tests/test_world_state_override.py
"""
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness import planner
from harness.config import Config

WORK_LIST = "Work List:\n- Fender amp: ready for pickup.\n"


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name
          + (f" -- {info}" if info and not cond else ""))
    assert cond, f"{name}: {info}"


def make_cfg(d, **kw):
    base = dict(workspace=d, max_steps=12, step_max_steps=6,
                max_consecutive_errors=3)
    base.update(kw)
    return Config(**base)


class Narrator:
    """Optionally writes a file on the first turn, then answers
    every further turn with a SUMMARY instead of DONE, so the loop
    ends the sub-run at the turn limit with a real final answer."""

    def __init__(self, write_path=None, content="",
                 summary_fails=False):
        self.write_path = write_path
        self.content = content
        self.summary_fails = summary_fails
        self._wrote = False
        self._summaries = 0

    def __call__(self, messages, tool_defs):
        if tool_defs is None:
            self._summaries += 1
            if self.summary_fails and self._summaries == 1:
                raise RuntimeError("endpoint down")
            return {"role": "assistant",
                    "content": "DONE: run summarized."}
        if self.write_path and not self._wrote:
            self._wrote = True
            return {"role": "assistant", "content": "Writing.",
                    "tool_calls": [{"function": {
                        "name": "write_file",
                        "arguments": {"path": self.write_path,
                                      "content": self.content}}}]}
        return {"role": "assistant",
                "content": "SUMMARY: the work is complete."}


def _run(step, actor, d):
    cfg = make_cfg(d)
    events = []
    r = planner.execute_plan("compile the work list", [step], cfg,
                             chat_fn=actor,
                             on_event=lambda k, *a: events.append(k))
    return r, events


def t_summary_ending_attested_clause_overrides():
    # Marcus Bennett's shape (v0.8.3 release run): the deliverable
    # is written complete, the only Then is prose (attested, its
    # file floor-checked), and the sub-run ends in SUMMARY.
    d = tempfile.mkdtemp(prefix="override-")
    try:
        step = planner.PlanStep(
            id=1, instruction="Write the work list to work_list.txt",
            done_when='"work_list.txt" contains the compiled work list')
        r, events = _run(step, Narrator("work_list.txt", WORK_LIST), d)
        check("run done on world state", r.status == "done", r.status)
        check("step done", step.status == "done", step.status)
        check("override recorded on the step",
              step.done_via_world_state is True)
        check("override event emitted",
              "step_done_world_state" in events, str(events))
        check("verification was actually run and passed",
              step.verify and all(c["ok"] for c in step.verify),
              str(step.verify))
        check("deliverable stands",
              Path(d, "work_list.txt").read_text() == WORK_LIST)
    finally:
        shutil.rmtree(d)


def t_summary_ending_machine_checked_overrides():
    # Tomas Rivera's shape: a machine-checked Then, work complete,
    # sub-run ends in SUMMARY.
    d = tempfile.mkdtemp(prefix="override-")
    try:
        step = planner.PlanStep(
            id=1, instruction="Write the inventory",
            done_when='"inventory.txt" exists')
        r, events = _run(step, Narrator("inventory.txt", "papers\n"), d)
        check("run done on world state", r.status == "done", r.status)
        check("override recorded", step.done_via_world_state is True)
    finally:
        shutil.rmtree(d)


def t_summary_ending_failing_world_still_fails():
    d = tempfile.mkdtemp(prefix="override-")
    try:
        step = planner.PlanStep(
            id=1, instruction="Write the work list to work_list.txt",
            done_when='"work_list.txt" exists')
        r, events = _run(step, Narrator(), d)
        check("run fails", r.status == "step_failed", r.status)
        check("step failed", step.status == "failed", step.status)
        check("no override recorded",
              step.done_via_world_state is False)
        check("the failed verification is on record",
              step.verify and not all(c["ok"] for c in step.verify),
              str(step.verify))
    finally:
        shutil.rmtree(d)


def t_dead_summary_call_gets_no_override():
    # The world is fine, but the sub-run's own summary call died:
    # a dead run gets no override even with the deliverable present.
    d = tempfile.mkdtemp(prefix="override-")
    try:
        step = planner.PlanStep(
            id=1, instruction="Write the work list to work_list.txt",
            done_when='"work_list.txt" exists')
        actor = Narrator("work_list.txt", WORK_LIST, summary_fails=True)
        r, events = _run(step, actor, d)
        check("run fails", r.status == "step_failed", r.status)
        check("no override recorded",
              step.done_via_world_state is False)
    finally:
        shutil.rmtree(d)


class ErrorSpam:
    """Every turn calls a tool with invalid arguments, so the loop
    ends the sub-run on the consecutive-error limit."""

    def __call__(self, messages, tool_defs):
        if tool_defs is None:
            return {"role": "assistant", "content": "DONE: summarized."}
        return {"role": "assistant", "content": "Trying.",
                "tool_calls": [{"function": {
                    "name": "write_file", "arguments": {}}}]}


def t_error_limit_gets_no_override():
    d = tempfile.mkdtemp(prefix="override-")
    try:
        Path(d, "work_list.txt").write_text(WORK_LIST)
        step = planner.PlanStep(
            id=1, instruction="Write the work list to work_list.txt",
            done_when='"work_list.txt" exists')
        r, events = _run(step, ErrorSpam(), d)
        check("run fails", r.status == "step_failed", r.status)
        check("no override recorded",
              step.done_via_world_state is False)
    finally:
        shutil.rmtree(d)


def t_done_path_unchanged():
    d = tempfile.mkdtemp(prefix="override-")
    try:
        step = planner.PlanStep(
            id=1, instruction="Write the work list to work_list.txt",
            done_when='"work_list.txt" exists')
        responses = iter([
            {"role": "assistant", "content": "Writing.",
             "tool_calls": [{"function": {
                 "name": "write_file",
                 "arguments": {"path": "work_list.txt",
                               "content": WORK_LIST}}}]},
            {"role": "assistant", "content": "DONE: written."},
            {"role": "assistant", "content": "DONE: summarized."},
        ])

        def fake(messages, tool_defs):
            return next(responses)

        cfg = make_cfg(d)
        events = []
        r = planner.execute_plan("compile the work list", [step], cfg,
                                 chat_fn=fake,
                                 on_event=lambda k, *a: events.append(k))
        check("run done the ordinary way", r.status == "done", r.status)
        check("no override on the DONE path",
              step.done_via_world_state is False)
        check("no override event on the DONE path",
              "step_done_world_state" not in events, str(events))
    finally:
        shutil.rmtree(d)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("t_") and callable(v)]
    for fn in fns:
        fn()
    print("\nAll world-state override tests passed.")
