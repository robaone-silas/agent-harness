#!/usr/bin/env python3
"""Tests for the coverage verifier and friends (v0.8.1).

Field failure behind this suite: a step read the previous step's stored
output (the grounding gate saw to that), then still wrote a deferral stub
as its deliverable, and verification attested it. Nothing judged the
deliverable against the source data. The covers verifier does:

    Then "summary.txt" covers the files from step 1

The harness extracts the item list deterministically from the source
step's listing-shaped tool results (list_dir / grep_files / ls), counts
how many of those names the deliverable actually mentions, and fails
below half, with the missing names as the retry feedback. A deferral
scan ("to be filled in", TODO, ...) rides along.

Also in this suite: the empty-DONE outline fallback. A step whose model
answers a bare "DONE:" leaves the next step's outline with a Result line
carrying zero content; the harness can compute a factual line itself
(tools used, lines of output) from the record it already keeps.

Run: python3 tests/test_coverage.py
"""
import re
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness import planner, verify
from harness.config import Config
from harness.loop import Step
from harness.tools import Jail


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        raise AssertionError(name)


def make_ctx(items_by_step):
    d = tempfile.mkdtemp(prefix="coverage-test-")
    ctx = verify.VerifyContext(jail=Jail(d), step_id=2, task="t",
                               prior_items=items_by_step)
    return ctx, d


ITEMS = ["alpha.txt", "beta.txt", "gamma.txt", "delta.txt"]


def run_covers(content, items=ITEMS, step=1):
    ctx, d = make_ctx({step: list(items)})
    try:
        Path(d, "summary.txt").write_text(content)
        res = verify.verify_step(
            [f'"summary.txt" covers the files from step {step}'], ctx)
        return res
    finally:
        shutil.rmtree(d)


def t_covers_passes_at_full_and_half():
    res = run_covers("alpha.txt beta.txt gamma.txt delta.txt")
    check("full coverage passes", res.ok, str([c.detail for c in res.checks]))
    check("verifier is covers", res.checks[0].verifier == "covers")
    res = run_covers("Plan: alpha.txt and beta.txt get folders.")
    check("half coverage passes", res.ok, str([c.detail for c in res.checks]))


def t_covers_fails_at_zero_with_missing_names():
    res = run_covers("Organization Strategy: This document outlines the strategy.")
    check("zero coverage fails", not res.ok)
    detail = res.checks[0].detail
    check("detail reports the count", "0 of 4" in detail, detail)
    check("detail names missing files", "alpha.txt" in detail and "delta.txt" in detail,
          detail)


def t_covers_fails_below_half():
    res = run_covers("Only alpha.txt is mentioned.")
    check("quarter coverage fails", not res.ok, str([c.detail for c in res.checks]))


def t_covers_deferral_phrase_fails_even_with_names():
    res = run_covers("alpha.txt beta.txt gamma.txt delta.txt\n(Content to be filled in.)")
    check("deferral phrase fails despite full coverage", not res.ok)
    check("detail quotes the deferral", "fill" in res.checks[0].detail.lower(),
          res.checks[0].detail)


def t_covers_no_items_fails_open_with_explanation():
    ctx, d = make_ctx({1: []})
    try:
        Path(d, "summary.txt").write_text("anything")
        res = verify.verify_step(['"summary.txt" covers the files from step 1'], ctx)
        check("no items: not failed (harness only fails what it can check)",
              res.ok, str([c.detail for c in res.checks]))
        check("no items: detail says coverage was not checked",
              "not checked" in res.checks[0].detail or "no file list" in res.checks[0].detail,
              res.checks[0].detail)
    finally:
        shutil.rmtree(d)


def t_covers_missing_file_fails():
    ctx, d = make_ctx({1: list(ITEMS)})
    try:
        res = verify.verify_step(['"summary.txt" covers the files from step 1'], ctx)
        check("missing deliverable fails", not res.ok)
    finally:
        shutil.rmtree(d)


def t_match_and_badge():
    check("covers matches for badges",
          verify.match_verifier('"summary.txt" covers the files from step 1') == "covers")
    check("covers phrasing variant matches",
          verify.match_verifier('summary.txt covers the files listed in step 2') == "covers")


def t_covers_phrasing_variants_from_the_field():
    # The planner, taught the idiom, produced its own phrasing live
    # (2026-10-03): "the contents of "organization.md" cover the files
    # from step 1" — cover, not covers, with a contents-of prefix. The
    # strict pattern missed it and the clause attested. The verifier
    # must accept the phrasings the planner actually produces.
    variants = [
        'the contents of "organization.md" cover the files from step 1',
        '"summary.txt" cover the files from step 1',
        'contents of report.md covers the files listed by step 3',
        'the content of "a.txt" covers the files from step 2',
    ]
    for v in variants:
        check(f"variant matches: {v[:44]}", verify.match_verifier(v) == "covers",
              str(verify.match_verifier(v)))
    check("prose near-miss still attests",
          verify.match_verifier(
              "the document covers the files from step 1 in spirit") is None)


def t_covers_field_phrasing_actually_verifies():
    ctx, d = make_ctx({1: list(ITEMS)})
    try:
        Path(d, "summary.txt").write_text("a stub about organizing")
        then = 'the contents of "summary.txt" cover the files from step 1'
        res = verify.verify_step([then], ctx)
        check("field phrasing is checked, not attested",
              res.checks[0].verifier == "covers", res.checks[0].verifier)
        check("field phrasing fails the stub", not res.ok)
        Path(d, "summary.txt").write_text("alpha.txt beta.txt gamma.txt delta.txt")
        res = verify.verify_step([then], ctx)
        check("field phrasing passes real coverage", res.ok)
    finally:
        shutil.rmtree(d)


def t_extract_items_from_listing_results():
    steps = [
        Step(n=1, thought="", tool="list_dir", args={"path": "."},
             result="alpha.txt\nbeta.txt\nDocs/\nTaxes/", path="native"),
        Step(n=2, thought="", tool="grep_files", args={"pattern": "x"},
             result="notes/a.txt\nnotes/b.txt", path="native"),
        Step(n=3, thought="", tool="exec", args={"command": "ls -1"},
             result="exit=0\none.txt\ntwo.txt", path="native"),
        Step(n=4, thought="", tool="exec", args={"command": "cat f.txt"},
             result="exit=0\nnot a listing line about cats", path="native"),
        Step(n=5, thought="", tool="read_file", args={"path": "x"},
             result="some prose\nmore prose", path="native"),
    ]
    items = verify.collect_step_items(steps)
    check("list_dir items extracted, dirs stripped",
          "alpha.txt" in items and "Docs" in items and "Taxes" in items, str(items))
    check("grep items extracted", "notes/a.txt" in items, str(items))
    check("exec ls items extracted, exit header skipped",
          "one.txt" in items and "two.txt" in items, str(items))
    check("exec cat result not treated as listing",
          "not a listing line about cats" not in items, str(items))
    check("read_file prose not treated as listing",
          "some prose" not in items, str(items))


# ------------------------------------------------------------- integration

PLAN_COV = """Feature: Coverage integration
  Scenario: Step 1 - List the files
    When List the files in the workspace
    Then the list is recorded
  Scenario: Step 2 - Write the summary
    When Write summary.txt covering the listed files
    Then "summary.txt" covers the files from step 1
"""


def t_stub_fails_coverage_then_retry_with_names_converts():
    d = tempfile.mkdtemp(prefix="coverage-int-")
    try:
        for name in ITEMS:
            Path(d, name).write_text("x")
        cfg = Config(workspace=d, max_steps=12, step_max_steps=8,
                     max_consecutive_errors=3)
        state = {"task_seen": None, "phase": 0}

        def fake(messages, tool_defs):
            if tool_defs is None and "Break the task" in messages[0]["content"]:
                return {"role": "assistant", "content": PLAN_COV}
            if tool_defs is None:
                return {"role": "assistant", "content": "DONE: ok."}
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
            # Step 2. First attempt: write the stub, DONE twice (gate escape).
            if "failed verification" not in task_msg:
                if phase == 0:
                    return {"role": "assistant", "content": "Writing.",
                            "tool_calls": [{"function": {"name": "write_file",
                                                         "arguments": {"path": "summary.txt",
                                                                       "content": "Summary: to be filled in."}}}]}
                return {"role": "assistant", "content": "DONE: written."}
            # Retry attempt: read the source, write a real summary, DONE.
            m = re.search(r"\.harness/runs/\S+/step-01\.md", task_msg)
            if phase == 0:
                return {"role": "assistant", "content": "Reading.",
                        "tool_calls": [{"function": {"name": "read_file",
                                                     "arguments": {"path": m.group(0)}}}]}
            if phase == 1:
                return {"role": "assistant", "content": "Writing for real.",
                        "tool_calls": [{"function": {"name": "write_file",
                                                     "arguments": {"path": "summary.txt",
                                                                   "content": "alpha.txt beta.txt gamma.txt delta.txt: one folder each."}}}]}
            return {"role": "assistant", "content": "DONE: covered."}

        verify_events = []
        r = planner.run_planned(
            "summarize the files", cfg, chat_fn=fake,
            on_event=lambda k, *a: verify_events.append(a[1])
            if k == "step_verify" else None)
        check("run completes", r.status == "done", r.status)
        step2_results = [v for v in verify_events
                         if any(c.verifier == "covers" for c in v.checks)]
        check("covers was checked twice (fail then pass)",
              len(step2_results) == 2, str(len(step2_results)))
        check("first covers check failed", not step2_results[0].ok)
        check("retry covers check passed", step2_results[1].ok)
        content = Path(d, "summary.txt").read_text()
        check("final deliverable names the files", "alpha.txt" in content)
        check("step 2 not waived (it genuinely passed)",
              r.steps[1].verify_waived is False)
    finally:
        shutil.rmtree(d)


def t_empty_done_gets_computed_outline_line():
    d = tempfile.mkdtemp(prefix="coverage-outline-")
    try:
        Path(d, "alpha.txt").write_text("x")
        cfg = Config(workspace=d, max_steps=12, step_max_steps=8,
                     max_consecutive_errors=3)
        prompts = {}

        def fake(messages, tool_defs):
            if tool_defs is None and "Break the task" in messages[0]["content"]:
                return {"role": "assistant", "content": PLAN_COV}
            if tool_defs is None:
                return {"role": "assistant", "content": "DONE: ok."}
            task_msg = messages[1]["content"] if len(messages) > 1 else ""
            for sid in (1, 2):
                if f"You are executing step {sid}" in task_msg and sid not in prompts:
                    prompts[sid] = task_msg
            if "You are executing step 1" in task_msg:
                if not getattr(fake, "_listed", False):
                    fake._listed = True
                    return {"role": "assistant", "content": "Listing.",
                            "tool_calls": [{"function": {"name": "list_dir",
                                                         "arguments": {"path": "."}}}]}
                return {"role": "assistant", "content": "DONE:"}  # bare, no body
            return {"role": "assistant", "content": "DONE: fine."}

        r = planner.run_planned("summarize", cfg, chat_fn=fake)
        check("run done", r.status == "done", r.status)
        p2 = prompts[2]
        check("outline notes the missing summary",
              "no summary given" in p2, p2[:900])
        check("outline fallback names the tool used",
              "list_dir" in p2, p2[:900])
    finally:
        shutil.rmtree(d)


if __name__ == "__main__":
    for fn in [t_covers_passes_at_full_and_half, t_covers_fails_at_zero_with_missing_names,
               t_covers_fails_below_half, t_covers_deferral_phrase_fails_even_with_names,
               t_covers_no_items_fails_open_with_explanation, t_covers_missing_file_fails,
               t_match_and_badge, t_covers_phrasing_variants_from_the_field,
               t_covers_field_phrasing_actually_verifies,
               t_extract_items_from_listing_results,
               t_stub_fails_coverage_then_retry_with_names_converts,
               t_empty_done_gets_computed_outline_line]:
        fn()
    print("\nAll coverage tests passed.")
