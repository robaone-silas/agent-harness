#!/usr/bin/env python3
"""Tests for the append_file tool (v0.8.1).

Field failure behind it (index task, run 1, 2026-10-03): the plan's
steps said "add it to index.md", one step per file, but the toolset had
no verb for adding. write_file replaces the whole file, so step 2's
"add" silently destroyed step 1's entry; the finished index held one
entry of six and no check could see the loss. The gap was vocabulary:
plans say add, the tools could only replace. append_file appends
exactly the content given (creating the file, and parent folders, when
missing), so an accumulating file survives the steps that build it.

Run: python3 tests/test_append.py
"""
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness import loop, planner
from harness.config import Config
from harness.tools import Jail, Refusal, make_tools


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        raise AssertionError(name)


def make_registry():
    d = tempfile.mkdtemp(prefix="append-test-")
    reg = make_tools(Jail(d), (), 30, 2000)
    return reg, d


def t_append_creates_a_missing_file():
    reg, d = make_registry()
    try:
        out = reg["append_file"]["func"]({"path": "notes.txt", "content": "first\n"})
        check("result reports an append", out.startswith("appended"), out)
        check("file created with the content",
              Path(d, "notes.txt").read_text() == "first\n")
    finally:
        shutil.rmtree(d)


def t_append_preserves_what_is_there():
    # The run-1 regression, pinned: three steps each "add" an entry to
    # the same index; all three entries must survive, byte for byte.
    reg, d = make_registry()
    try:
        reg["write_file"]["func"]({"path": "index.md", "content": "entry A\n"})
        reg["append_file"]["func"]({"path": "index.md", "content": "entry B\n"})
        reg["append_file"]["func"]({"path": "index.md", "content": "entry C\n"})
        check("all entries accumulate in order",
              Path(d, "index.md").read_text() == "entry A\nentry B\nentry C\n",
              repr(Path(d, "index.md").read_text()))
    finally:
        shutil.rmtree(d)


def t_append_is_verbatim_no_magic_separator():
    reg, d = make_registry()
    try:
        reg["write_file"]["func"]({"path": "x.txt", "content": "abc"})
        reg["append_file"]["func"]({"path": "x.txt", "content": "def"})
        check("no separator inserted", Path(d, "x.txt").read_text() == "abcdef",
              repr(Path(d, "x.txt").read_text()))
    finally:
        shutil.rmtree(d)


def t_append_creates_parent_folders():
    reg, d = make_registry()
    try:
        reg["append_file"]["func"]({"path": "sub/dir/log.txt", "content": "line\n"})
        check("nested file created",
              Path(d, "sub", "dir", "log.txt").read_text() == "line\n")
    finally:
        shutil.rmtree(d)


def t_append_is_jailed():
    reg, d = make_registry()
    try:
        try:
            reg["append_file"]["func"]({"path": "../escape.txt", "content": "x"})
            check("escape refused", False, "no exception raised")
        except Refusal:
            check("escape refused", True)
        check("nothing written outside", not Path(d).parent.joinpath("escape.txt").exists())
    finally:
        shutil.rmtree(d)


def t_append_registered_and_described_for_the_model():
    reg, d = make_registry()
    try:
        check("append_file registered", "append_file" in reg, str(sorted(reg)))
        desc = reg["append_file"]["description"].lower()
        check("description names the verb", "append" in desc, desc)
        check("description contrasts with write_file",
              "write_file" in desc, desc)
    finally:
        shutil.rmtree(d)


def t_append_is_not_a_discovery_tool():
    # Discovery runs are read-only; an append primitive must never be
    # available there.
    check("append_file not in READ_ONLY_TOOLS",
          "append_file" not in planner.READ_ONLY_TOOLS,
          str(planner.READ_ONLY_TOOLS))


def t_append_through_the_loop_accumulates():
    d = tempfile.mkdtemp(prefix="append-loop-")
    try:
        cfg = Config(workspace=d, max_steps=8, step_max_steps=6,
                     max_consecutive_errors=3)
        calls = [
            {"role": "assistant", "content": "Adding A.",
             "tool_calls": [{"function": {"name": "append_file",
                                          "arguments": {"path": "index.md",
                                                        "content": "A\n"}}}]},
            {"role": "assistant", "content": "Adding B.",
             "tool_calls": [{"function": {"name": "append_file",
                                          "arguments": {"path": "index.md",
                                                        "content": "B\n"}}}]},
        ]

        def fake(messages, tool_defs):
            if calls:
                return calls.pop(0)
            return {"role": "assistant", "content": "DONE: both added."}

        r = loop.run("add two entries", cfg, chat_fn=fake)
        check("loop run done", r.status == "done", r.status)
        check("both appends landed",
              Path(d, "index.md").read_text() == "A\nB\n",
              repr(Path(d, "index.md").read_text()))
    finally:
        shutil.rmtree(d)


if __name__ == "__main__":
    for fn in [t_append_creates_a_missing_file, t_append_preserves_what_is_there,
               t_append_is_verbatim_no_magic_separator,
               t_append_creates_parent_folders, t_append_is_jailed,
               t_append_registered_and_described_for_the_model,
               t_append_is_not_a_discovery_tool,
               t_append_through_the_loop_accumulates]:
        fn()
    print("\nAll append_file tests passed.")
