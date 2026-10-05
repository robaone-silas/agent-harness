#!/usr/bin/env python3
"""Tests for v0.7 per-step Then verification: the verifier registry and the
verify-then-retry integration in the planner. Scripted fake model, no Ollama.

Run: python3 tests/test_verify.py
"""
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness import planner, verify
from harness.config import Config
from harness.tools import Jail


def make_cfg(**kw):
    d = tempfile.mkdtemp(prefix="verify-test-")
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


def ctx_for(d):
    return verify.VerifyContext(jail=Jail(d), step_id=1, task="t")


def t_exists():
    cfg, d = make_cfg()
    ctx = ctx_for(d)
    Path(d, "a.txt").write_text("x")
    r = verify.verify_step(['"a.txt" exists', '"b.txt" exists'], ctx)
    check("exists ok", r.checks[0].ok and r.checks[0].verifier == "exists")
    check("exists fail", not r.checks[1].ok and "MISSING" in r.checks[1].detail)
    check("step not ok", not r.ok)
    # jail escape fails closed, never resolves outside
    r = verify.verify_step(['"../evil" exists'], ctx)
    check("escape fails closed", not r.checks[0].ok and "escapes" in r.checks[0].detail)
    shutil.rmtree(d)


def t_contains():
    cfg, d = make_cfg()
    ctx = ctx_for(d)
    Path(d, "a.txt").write_text("hello world")
    r = verify.verify_step(['"a.txt" contains "world"', '"a.txt" contains "mars"'], ctx)
    check("contains ok", r.checks[0].ok)
    check("contains fail", not r.checks[1].ok and "does not contain" in r.checks[1].detail)
    # prose phrasing the planner actually emits
    r = verify.verify_step(['the file a.txt should contain the exact content "hello world"'], ctx)
    check("prose exact matches", r.checks[0].verifier == "contains_exactly", r.checks[0].verifier)
    shutil.rmtree(d)


def t_contains_exactly():
    cfg, d = make_cfg()
    ctx = ctx_for(d)
    Path(d, "total.txt").write_text("100.0\n")
    r = verify.verify_step(['"total.txt" contains exactly "100"'], ctx)
    c = r.checks[0]
    check("exact fail", not c.ok and c.verifier == "contains_exactly")
    check("exact names got+expected",
          "got '100.0'" in c.detail and "expected exactly '100'" in c.detail, c.detail)
    Path(d, "total.txt").write_text("100")
    r = verify.verify_step(['"total.txt" contains exactly "100"'], ctx)
    check("exact pass", r.ok)
    shutil.rmtree(d)


def t_has_lines():
    cfg, d = make_cfg()
    ctx = ctx_for(d)
    Path(d, "f.txt").write_text("a\nb\nc\n")
    r = verify.verify_step(['"f.txt" has 3 lines', '"f.txt" has 12 lines',
                            '"f.txt" contains 3 lines'], ctx)
    check("has_lines ok", r.checks[0].ok and r.checks[0].verifier == "has_lines")
    check("has_lines fail", not r.checks[1].ok and "expected 3" not in r.checks[1].detail
          and "expected 12" in r.checks[1].detail, r.checks[1].detail)
    check("contains N lines alias", r.checks[2].verifier == "has_lines", r.checks[2].verifier)
    shutil.rmtree(d)


def t_not_exists_and_attest():
    cfg, d = make_cfg()
    ctx = ctx_for(d)
    r = verify.verify_step(['"gone.txt" does not exist'], ctx)
    check("not_exists ok", r.checks[0].ok and r.checks[0].verifier == "not_exists")
    Path(d, "gone.txt").write_text("x")
    r = verify.verify_step(['"gone.txt" does not exist'], ctx)
    check("not_exists fail", not r.checks[0].ok)
    r = verify.verify_step(['the stars align'], ctx)
    check("attest fallback", r.ok and r.checks[0].verifier == "attest")
    check("match_verifier none", verify.match_verifier("the stars align") is None)
    check("match_verifier name", verify.match_verifier('"a" exists') == "exists")
    shutil.rmtree(d)


def t_verify_retry_integration():
    # Attempt 1: model claims DONE without creating the file -> verify fails ->
    # step retries with the failure as feedback -> attempt 2 writes it -> verify passes.
    cfg, d = make_cfg()
    step = planner.PlanStep(id=1, instruction="Write a.txt containing x",
                            done_when='"a.txt" contains exactly "x"')
    fake = scripted([
        {"role": "assistant", "content": "DONE: did nothing."},
        {"role": "assistant", "content": "Writing.",
         "tool_calls": [{"function": {"name": "write_file",
                                      "arguments": {"path": "a.txt", "content": "x"}}}]},
        {"role": "assistant", "content": "DONE: wrote a.txt."},
        {"role": "assistant", "content": "DONE: all good."},
    ])
    events = []
    r = planner.execute_plan("do the thing", [step], cfg, chat_fn=fake,
                             on_event=lambda k, *a: events.append(k))
    check("retry converts", r.status == "done", r.status)
    check("verify events", events.count("step_verify") == 2, str(events))
    check("file written", Path(d, "a.txt").read_text() == "x")
    check("verify record", step.verify and step.verify[0]["verifier"] == "contains_exactly"
          and step.verify[0]["ok"], str(step.verify))
    shutil.rmtree(d)


def t_verify_exhausted():
    # Verification fails on both attempts -> waived with warning, plan continues.
    cfg, d = make_cfg()
    step = planner.PlanStep(id=1, instruction="Write a.txt",
                            done_when='"a.txt" exists')
    fake = scripted([
        {"role": "assistant", "content": "DONE: nothing."},
        {"role": "assistant", "content": "DONE: still nothing."},
        {"role": "assistant", "content": "DONE: gave up."},
    ])
    events = []
    r = planner.execute_plan("do the thing", [step], cfg, chat_fn=fake,
                             on_event=lambda k, *a: events.append(k))
    check("exhausted waives at step level, plan continues",
          step.status == "done", step.status)
    check("waiver recorded", step.verify_waived is True, str(step.verify_waived))
    check("waiver event emitted", "step_verify_waived" in events, str(events))
    # Issue #22 boundary: the step waives, but the run may not report
    # done while the file the plan promised ("a.txt" exists) was never
    # created. The deliverable audit ends the run as a failure.
    check("run fails the deliverable audit",
          r.status == "step_failed" and "a.txt" in r.answer, r.status)
    check("audit event emitted", "deliverables_missing" in events,
          str(events))
    check("calls bounded", fake.calls["n"] == 2, str(fake.calls["n"]))
    shutil.rmtree(d)


def t_subrun_failure_still_hard():
    # A sub-run that itself fails (not a verification dispute) still hard-fails.
    cfg, d = make_cfg()
    step = planner.PlanStep(id=1, instruction="Write a.txt",
                            done_when='"a.txt" exists')

    def fake(messages, tool_defs):
        return {"role": "assistant", "content": "I refuse."}

    r = planner.execute_plan("do the thing", [step], cfg, chat_fn=fake)
    check("sub-run failure hard-fails",
          r.status == "step_failed" and step.status == "failed"
          and step.verify_waived is False, r.status)
    shutil.rmtree(d)


def t_attest_passes_through():
    # Unverifiable Then: no retry machinery engages, model's DONE is trusted.
    cfg, d = make_cfg()
    step = planner.PlanStep(id=1, instruction="Think",
                            done_when="the answer is clear")
    fake = scripted([
        {"role": "assistant", "content": "DONE: clear."},
        {"role": "assistant", "content": "DONE: summary."},
    ])
    events = []
    r = planner.execute_plan("do the thing", [step], cfg, chat_fn=fake,
                             on_event=lambda k, *a: events.append(k))
    check("attest passes", r.status == "done", r.status)
    check("attest recorded", step.verify[0]["verifier"] == "attest")
    check("no retry on attest", fake.calls["n"] == 2, str(fake.calls["n"]))
    shutil.rmtree(d)


def t_prose_not_literal():
    # v0.7 false-positive regressions: prose descriptions must NOT match as
    # literal assertions — they fall back to attestation.
    cfg, d = make_cfg()
    ctx = ctx_for(d)
    Path(d, "found.txt").write_text("b.txt")
    r = verify.verify_step(
        ["found.txt contains the list of filenames found"], ctx)
    check("prose contains -> attest",
          r.checks[0].verifier == "attest" and r.ok, r.checks[0].verifier)
    r = verify.verify_step(['the output contains exactly "OK"'], ctx)
    check("prose output -> attest, not a file named 'the output'",
          r.checks[0].verifier == "attest", r.checks[0].verifier)
    # mixed quotes: inner single quotes preserved inside double quotes
    Path(d, "broken.py").write_text("print('NOPE')\n")
    r = verify.verify_step(['"broken.py" contains exactly "print(\'OK\')"'], ctx)
    check("mixed quotes fail path",
          r.checks[0].verifier == "contains_exactly" and not r.checks[0].ok,
          f"{r.checks[0].verifier} {r.checks[0].detail}")
    # backtick-quoted path and value (planner emits these)
    Path(d, "total.txt").write_text("12345")
    r = verify.verify_step(["the file `total.txt` should contain exactly `12345`"], ctx)
    check("backticks",
          r.checks[0].verifier == "contains_exactly" and r.ok,
          f"{r.checks[0].verifier} {r.checks[0].detail}")
    r = verify.verify_step(["the file `total.txt` should not exist"], ctx)
    check("backtick not_exists fails correctly",
          r.checks[0].verifier == "not_exists" and not r.ok,
          f"{r.checks[0].verifier} {r.checks[0].detail}")
    shutil.rmtree(d)


if __name__ == "__main__":
    for fn in [t_exists, t_contains, t_contains_exactly, t_has_lines,
               t_not_exists_and_attest, t_prose_not_literal,
               t_verify_retry_integration,
               t_verify_exhausted, t_subrun_failure_still_hard,
               t_attest_passes_through]:
        fn()
    print("\nAll verify tests passed.")
