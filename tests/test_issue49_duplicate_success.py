#!/usr/bin/env python3
"""Issue 49, reopened facet (daily run 2026-10-31): a step that repeats
an already-successful read must not die at error_limit.

On 2026-10-31, step 4 read step-02.md in full (1490 chars, under the
2000-char read limit, no truncation), then repeated that exact read
four more times: once answered with the repeat-success nudge, then
three times with LOOP_BREAKER errors. Those three errors reached
max_consecutive_errors, the sub-run ended at error_limit, and step 5
never ran, so no deliverable was created. The repeated call never ran
the tool again, so it was never a tool error: it should not consume
the consecutive-error budget, and the model should keep its remaining
turns to take the different action the breaker itself suggests.

A repeat of a call that actually failed is different, and still
counts: a failed tool repeated is a failing loop, not a stuck success.

Run: python3 tests/test_issue49_duplicate_success.py
"""
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness import loop
from harness.config import Config


def make_cfg(**kw):
    d = tempfile.mkdtemp(prefix="harness-test-")
    base = dict(workspace=d, max_steps=12, max_consecutive_errors=3)
    base.update(kw)
    return Config(**base), d


def scripted(responses):
    it = iter(responses)

    def fake(messages, tool_defs):
        try:
            return next(it)
        except StopIteration:
            return {"role": "assistant", "content": "SUMMARY: stopped."}
    return fake


def read_call(path):
    return {"role": "assistant", "content": "Reading.",
            "tool_calls": [{"function": {"name": "read_file",
                                         "arguments": {"path": path}}}]}


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        raise AssertionError(name)


def t_repeat_success_then_recovery():
    cfg, d = make_cfg()
    Path(d, "a.txt").write_text("alpha")
    Path(d, "b.txt").write_text("beta")
    fake = scripted([
        read_call("a.txt"),   # succeeds
        read_call("a.txt"),   # repeat-success nudge
        read_call("a.txt"),   # loop breaker
        read_call("a.txt"),   # loop breaker
        read_call("a.txt"),   # loop breaker: the third would have tripped error_limit
        read_call("b.txt"),   # the different action: succeeds, resets the error count
        {"role": "assistant", "content": "DONE: read both files."},
    ])
    r = loop.run("read a then b", cfg, chat_fn=fake)
    check("repeat-success loop recovers to done", r.status == "done", r.status)
    check("recovery read ran", Path(d, "b.txt").read_text() == "beta")
    check("no error_limit", r.status != "error_limit", r.status)
    shutil.rmtree(d)


def t_repeat_failure_still_trips_error_limit():
    cfg, d = make_cfg()
    fake = scripted([
        read_call("missing.txt"),  # real tool error
        read_call("missing.txt"),  # repeat of a failed call: still counts
        read_call("missing.txt"),  # third consecutive error trips the limit
    ])
    r = loop.run("read a missing file", cfg, chat_fn=fake)
    check("repeat-failure still hits error_limit", r.status == "error_limit", r.status)
    shutil.rmtree(d)


if __name__ == "__main__":
    for fn in [t_repeat_success_then_recovery,
               t_repeat_failure_still_trips_error_limit]:
        fn()
    print("\nAll issue-49 duplicate-success tests passed.")
