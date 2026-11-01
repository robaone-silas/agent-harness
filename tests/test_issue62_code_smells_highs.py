#!/usr/bin/env python3
"""Tests for issue #62 (code smells, high severity only): the four high
findings in harness/planner.py, addressed on this shift.

Two of the four are analyzer false positives, and the tests lock the
behavior that proves it: _parse_step_segment_tokens and _step_refs are
pure computations over their inputs (no I/O, no mutation, no calls
out), so there is no decision/action split to make. What is real is
their nesting, so the fix flattens them with helpers and the tests
below lock every token shape the old code handled.

The other two are real but best-effort by design: a raising verify_now
inside the grounding gate, and a failing run-store write, must not fail
the run. The fix keeps that fall-through and logs the swallowed
exception instead of passing silently, and the tests prove both halves:
no raise, and a log record exists.

Run: python3 tests/test_issue62_code_smells_highs.py
"""
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness import planner
from harness.planner import PlanStep


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name
          + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        raise AssertionError(name)


def t_segment_tokens_single_and_lists():
    check("single token",
          planner._parse_step_segment_tokens(["1"]) == {1})
    check("and list",
          planner._parse_step_segment_tokens(["1", "and", "2"]) == {1, 2})
    check("comma and list",
          planner._parse_step_segment_tokens(
              ["1", ",", "2", ",", "and", "3"]) == {1, 2, 3})


def t_segment_tokens_ranges():
    check("through range",
          planner._parse_step_segment_tokens(["1", "through", "5"])
          == {1, 2, 3, 4, 5})
    check("to range",
          planner._parse_step_segment_tokens(["1", "to", "3"]) == {1, 2, 3})
    check("dash range",
          planner._parse_step_segment_tokens(["1", "-", "3"]) == {1, 2, 3})
    check("reversed range expands the same span",
          planner._parse_step_segment_tokens(["5", "through", "3"])
          == {3, 4, 5})
    check("range wider than 64 keeps only its ends",
          planner._parse_step_segment_tokens(["1", "through", "100"])
          == {1, 100})


def t_segment_tokens_edge_shapes():
    check("trailing connector adds nothing",
          planner._parse_step_segment_tokens(["1", "and"]) == {1})
    check("leading connector with no previous step",
          planner._parse_step_segment_tokens(["and", "2"]) == {2})
    check("range connector with no previous step",
          planner._parse_step_segment_tokens(["through", "2"]) == {2})
    check("empty token list",
          planner._parse_step_segment_tokens([]) == set())


def t_step_refs_shapes():
    check("singular reference",
          planner._step_refs("see step 2") == {2})
    check("singular head with a range",
          planner._step_refs("from step 1 through 5") == {1, 2, 3, 4, 5})
    check("plural list",
          planner._step_refs("steps 1, 2, and 3") == {1, 2, 3})
    check("plural dash range",
          planner._step_refs("steps 1-3") == {1, 2, 3})
    check("no reference",
          planner._step_refs("write the summary") == set())


class CapturingHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


def _capture_planner_logs():
    logger = logging.getLogger("harness.planner")
    handler = CapturingHandler()
    logger.addHandler(handler)
    old_propagate = logger.propagate
    logger.propagate = False
    return logger, handler, old_propagate


def _release_planner_logs(logger, handler, old_propagate):
    logger.removeHandler(handler)
    logger.propagate = old_propagate


def _gate_with_prior_read(verify_now):
    prior = PlanStep(id=1, instruction="gather", done_when="",
                     status="done",
                     output_path=".harness/runs/x/step-01.md")
    step = PlanStep(id=2, instruction="compile", done_when="",
                    output_path=".harness/runs/x/step-02.md")
    gate = planner._grounding_gate(step, [prior, step],
                                   verify_now=verify_now)
    read_sub_step = SimpleNamespace(
        tool="read_file",
        args={"path": ".harness/runs/x/step-01.md"})
    return gate, [read_sub_step]


def t_gate_verify_now_raising_logs_and_keeps_pushback():
    def raising_verify_now():
        raise RuntimeError("verifier blew up")

    gate, sub_steps = _gate_with_prior_read(raising_verify_now)
    logger, handler, old_propagate = _capture_planner_logs()
    try:
        result = gate(sub_steps)
    finally:
        _release_planner_logs(logger, handler, old_propagate)
    check("raising verify_now does not raise out of the gate",
          result is not None, str(result))
    check("raising verify_now keeps the pushback feedback",
          "have not changed anything since reading it" in result, result)
    check("raising verify_now is logged, not swallowed silently",
          len(handler.records) == 1
          and handler.records[0].levelno == logging.WARNING
          and "verify_now" in handler.records[0].getMessage(),
          str([record.getMessage() for record in handler.records]))


def t_gate_verify_now_passing_stays_silent():
    gate, sub_steps = _gate_with_prior_read(lambda: True)
    logger, handler, old_propagate = _capture_planner_logs()
    try:
        result = gate(sub_steps)
    finally:
        _release_planner_logs(logger, handler, old_propagate)
    check("passing verify_now silences the gate",
          result is None, str(result))
    check("passing verify_now logs nothing",
          handler.records == [],
          str([record.getMessage() for record in handler.records]))


def t_run_store_write_failure_logs_and_does_not_raise():
    class RaisingStore:
        def write_step(self, **kwargs):
            raise RuntimeError("disk is full")

    step = PlanStep(id=1, instruction="gather", done_when="")
    logger, handler, old_propagate = _capture_planner_logs()
    try:
        result = planner._write_step_to_store(
            RaisingStore(), step, [], "done")
    finally:
        _release_planner_logs(logger, handler, old_propagate)
    check("failing run-store write does not raise",
          result is None, str(result))
    check("failing run-store write is logged, not swallowed silently",
          len(handler.records) == 1
          and handler.records[0].levelno == logging.WARNING
          and "run store" in handler.records[0].getMessage(),
          str([record.getMessage() for record in handler.records]))


def main():
    t_segment_tokens_single_and_lists()
    t_segment_tokens_ranges()
    t_segment_tokens_edge_shapes()
    t_step_refs_shapes()
    t_gate_verify_now_raising_logs_and_keeps_pushback()
    t_gate_verify_now_passing_stays_silent()
    t_run_store_write_failure_logs_and_does_not_raise()
    print("\nAll issue #62 high-severity tests passed.")


if __name__ == "__main__":
    main()
