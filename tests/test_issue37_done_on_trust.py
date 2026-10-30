"""Tests for the issue #37 trust-only completion status.

A run whose deliverable rests entirely on attested Then clauses must
never again report plain done. When every step finished but at least
one step that writes a promised deliverable was checked only on trust,
the run reports done_on_trust and still exits 0: the work completed,
but its content was never machine-checked. A prose Then on a read step
names no promised deliverable and does not taint the run, and a waived
step keeps its own separate flag.

Run: python3 tests/test_issue37_done_on_trust.py
"""
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness import planner, verify
from harness.config import Config

import run as run_cli


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name
          + (f" -- {info}" if info and not cond else ""))
    assert cond, f"{name}: {info}"


def make_cfg(d, **kw):
    base = dict(workspace=d, max_steps=12, step_max_steps=6,
                max_consecutive_errors=3)
    base.update(kw)
    return Config(**base)


def fake_step(**kw):
    step = planner.PlanStep(id=kw.pop("id"), instruction=kw.pop("instruction"),
                            done_when=kw.pop("done_when"))
    for k, v in kw.items():
        setattr(step, k, v)
    return step


def t_trust_only_steps_unit():
    # A done step, all checks attested, naming a promised deliverable.
    steps = [
        fake_step(id=1, instruction="Read the repair files",
                  done_when="the output shows the content of the repair files",
                  status="done",
                  verify=[{"then": "the output shows the content",
                           "verifier": "attest", "ok": True, "detail": ""}]),
        fake_step(id=2, instruction="Write the work list",
                  done_when='"work_list.txt" is complete',
                  status="done",
                  verify=[{"then": '"work_list.txt" is complete',
                           "verifier": "attest", "ok": True, "detail": ""}]),
    ]
    got = verify.trust_only_steps(steps)
    check("attested deliverable step listed",
          got == [{"step": 2, "deliverables": ["work_list.txt"]}], str(got))

    # Mixed checks: one machine check anywhere means not trust-only.
    mixed = fake_step(id=1, instruction="Write the list",
                      done_when='"out.txt" exists',
                      status="done",
                      verify=[{"then": '"out.txt" exists', "verifier": "exists",
                               "ok": True, "detail": ""},
                              {"then": '"out.txt" is complete',
                               "verifier": "attest", "ok": True, "detail": ""}])
    check("mixed step not trust-only",
          verify.trust_only_steps([mixed]) == [])

    # No checks at all: nothing was attested, nothing to label.
    empty = fake_step(id=1, instruction="Do something", done_when="",
                      status="done", verify=[])
    check("checkless step not trust-only",
          verify.trust_only_steps([empty]) == [])

    # A waived step keeps its own flag and never counts as trust-only,
    # even when its recorded checks are all attested.
    waived = fake_step(id=1, instruction="Write the list",
                       done_when='"out.txt" is complete',
                       status="done", verify_waived=True,
                       verify=[{"then": '"out.txt" is complete',
                                "verifier": "attest", "ok": True, "detail": ""}])
    check("waived step not trust-only",
          verify.trust_only_steps([waived]) == [])

    # A failed step cannot taint a run that never completed.
    failed = fake_step(id=1, instruction="Write the list",
                       done_when='"out.txt" is complete',
                       status="failed",
                       verify=[{"then": '"out.txt" is complete',
                                "verifier": "attest", "ok": True, "detail": ""}])
    check("failed step not trust-only",
          verify.trust_only_steps([failed]) == [])


class StepWriter:
    """Fake model: writes each step's file once, then answers DONE.

    work maps step id to (path, content). Summary calls (tool_defs
    None) are recorded so tests can inspect the final summary prompt.
    """

    def __init__(self, work):
        self.work = work
        self.wrote = set()
        self.summary_prompts = []

    def __call__(self, messages, tool_defs):
        if tool_defs is None:
            self.summary_prompts.append(messages)
            return {"role": "assistant", "content": "DONE: run summarized."}
        step_id = None
        for m in reversed(messages):
            content = m.get("content") or ""
            if "executing step " in content:
                try:
                    step_id = int(content.split("executing step ")[1]
                                  .split(" ")[0])
                except (ValueError, IndexError):
                    pass
                break
        if step_id in self.work and step_id not in self.wrote:
            self.wrote.add(step_id)
            path, text = self.work[step_id]
            return {"role": "assistant", "content": "Writing.",
                    "tool_calls": [{"function": {
                        "name": "write_file",
                        "arguments": {"path": path, "content": text}}}]}
        return {"role": "assistant", "content": "DONE: step complete."}


def _run_plan(steps, work, task="compile the work list"):
    d = tempfile.mkdtemp(prefix="trust-")
    try:
        fake = StepWriter(work)
        r = planner.execute_plan(task, steps, make_cfg(d), chat_fn=fake)
        return d, r, fake
    except Exception:
        shutil.rmtree(d)
        raise


def t_attested_compile_reports_done_on_trust():
    # The 2026-10-25 Marcus shape, reduced: the compile step writes the
    # deliverable and its only Then is prose, so nothing checks content.
    d = tempfile.mkdtemp(prefix="trust-")
    try:
        step = planner.PlanStep(
            id=1, instruction="Write the parts order list",
            done_when='"parts-order-list.txt" is complete and correct')
        fake = StepWriter({1: ("parts-order-list.txt",
                               "Parts Order List\nkeybed assembly: 1\n")})
        r = planner.execute_plan("make the parts order list", [step],
                                 make_cfg(d), chat_fn=fake)
        check("attested compile is done_on_trust, not done",
              r.status == "done_on_trust", r.status)
        check("trust step recorded with its deliverable",
              r.trust_only == [{"step": 1,
                                "deliverables": ["parts-order-list.txt"]}],
              str(r.trust_only))
        check("deliverable still stands",
              Path(d, "parts-order-list.txt").read_text().startswith(
                  "Parts Order List"))
        check("summary prompt names the trust",
              any("on trust" in (m.get("content") or "")
                  for msgs in fake.summary_prompts for m in msgs),
              str(fake.summary_prompts))
    finally:
        shutil.rmtree(d)


def t_verified_run_still_reports_done():
    d = tempfile.mkdtemp(prefix="trust-")
    try:
        step = planner.PlanStep(
            id=1, instruction="Write the work list",
            done_when='"work_list.txt" exists')
        fake = StepWriter({1: ("work_list.txt", "Work List\n")})
        r = planner.execute_plan("compile the work list", [step],
                                 make_cfg(d), chat_fn=fake)
        check("verified run still plain done", r.status == "done", r.status)
        check("no trust steps on a verified run", r.trust_only == [],
              str(r.trust_only))
        check("summary prompt carries no trust note",
              not any("on trust" in (m.get("content") or "")
                      for msgs in fake.summary_prompts for m in msgs))
    finally:
        shutil.rmtree(d)


def t_mixed_run_names_only_the_trust_step():
    d = tempfile.mkdtemp(prefix="trust-")
    try:
        steps = [
            planner.PlanStep(id=1, instruction="Write the verified list",
                             done_when='"verified.txt" exists'),
            planner.PlanStep(id=2, instruction="Write the compiled list",
                             done_when='"compiled.txt" is complete'),
        ]
        fake = StepWriter({1: ("verified.txt", "v\n"),
                           2: ("compiled.txt", "c\n")})
        r = planner.execute_plan("write two lists", steps,
                                 make_cfg(d), chat_fn=fake)
        check("mixed run is done_on_trust", r.status == "done_on_trust",
              r.status)
        check("only the attested step is named",
              r.trust_only == [{"step": 2, "deliverables": ["compiled.txt"]}],
              str(r.trust_only))
    finally:
        shutil.rmtree(d)


def t_attested_read_step_does_not_taint_verified_run():
    # A prose Then on a read step names no promised deliverable, so it
    # must not turn a verified deliverable into done_on_trust.
    d = tempfile.mkdtemp(prefix="trust-")
    try:
        steps = [
            planner.PlanStep(id=1, instruction="Read the repair files",
                             done_when="the output shows the content of "
                                       "the repair files"),
            planner.PlanStep(id=2, instruction="Write the work list",
                             done_when='"work_list.txt" exists'),
        ]
        fake = StepWriter({2: ("work_list.txt", "Work List\n")})
        r = planner.execute_plan("compile the work list", steps,
                                 make_cfg(d), chat_fn=fake)
        check("verified deliverable stays plain done",
              r.status == "done", r.status)
        check("read prose adds no trust step", r.trust_only == [],
              str(r.trust_only))
    finally:
        shutil.rmtree(d)


def t_exit_code_mapping():
    check("done exits 0", run_cli._exit_code("done") == 0)
    check("done_on_trust exits 0", run_cli._exit_code("done_on_trust") == 0)
    check("step_failed exits 1", run_cli._exit_code("step_failed") == 1)
    check("plan_failed exits 1", run_cli._exit_code("plan_failed") == 1)
    check("model_error exits 1", run_cli._exit_code("model_error") == 1)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("t_") and callable(v)]
    for fn in fns:
        fn()
    print("ALL PASSED")
