#!/usr/bin/env python3
"""Tests for list_dir depth (v0.8.1).

list_dir used to be shallow only: one level, and listing a subdirectory
returned bare names that are not valid paths for any other tool. The fix:
an optional `depth` argument (default 1), workspace-relative paths in the
output, a deterministic sorted walk, an entry cap, and symlink folders
listed but never followed (the jail still holds).

Run: python3 tests/test_listdir.py
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness import loop
from harness.config import Config
from harness.tools import Jail, ToolError, make_tools


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        raise AssertionError(name)


def make_registry():
    d = tempfile.mkdtemp(prefix="listdir-test-")
    jail = Jail(d)
    reg = make_tools(jail, (), 30, 2000)
    return reg, d


def build_tree(d):
    Path(d, "a.txt").write_text("a")
    Path(d, "sub").mkdir()
    Path(d, "sub", "b.txt").write_text("b")
    Path(d, "sub", "deep").mkdir()
    Path(d, "sub", "deep", "c.txt").write_text("c")


def t_default_is_shallow_and_unchanged_for_root():
    reg, d = make_registry()
    try:
        build_tree(d)
        list_dir = reg["list_dir"]["func"]
        check("default depth lists root only",
              list_dir({}) == "a.txt\nsub/", repr(list_dir({})))
        check("explicit depth 1 matches default",
              list_dir({"depth": 1}) == "a.txt\nsub/")
    finally:
        shutil.rmtree(d)


def t_depth_walks_down():
    reg, d = make_registry()
    try:
        build_tree(d)
        list_dir = reg["list_dir"]["func"]
        check("depth 2 includes subfolder contents",
              list_dir({"depth": 2}) == "a.txt\nsub/\nsub/b.txt\nsub/deep/",
              repr(list_dir({"depth": 2})))
        check("depth 3 reaches the bottom",
              list_dir({"depth": 3}) ==
              "a.txt\nsub/\nsub/b.txt\nsub/deep/\nsub/deep/c.txt",
              repr(list_dir({"depth": 3})))
        check("depth beyond tree is harmless",
              list_dir({"depth": 9}) == list_dir({"depth": 3}))
    finally:
        shutil.rmtree(d)


def t_subdir_listing_returns_usable_paths():
    reg, d = make_registry()
    try:
        build_tree(d)
        list_dir = reg["list_dir"]["func"]
        read_file = reg["read_file"]["func"]
        out = list_dir({"path": "sub"})
        check("subdir entries are workspace-relative",
              out == "sub/b.txt\nsub/deep/", repr(out))
        # Every file path the tool returns must work as a read_file argument.
        for line in list_dir({"depth": 3}).splitlines():
            if not line.endswith("/"):
                check(f"returned path readable: {line}",
                      read_file({"path": line}) in ("a", "b", "c"))
    finally:
        shutil.rmtree(d)


def t_symlink_dirs_listed_not_followed():
    reg, d = make_registry()
    outside = tempfile.mkdtemp(prefix="listdir-outside-")
    try:
        build_tree(d)
        Path(outside, "secret.txt").write_text("secret")
        try:
            os.symlink(outside, Path(d, "link"))
        except (OSError, NotImplementedError):
            print("SKIP symlink test (cannot create symlinks here)")
            return
        list_dir = reg["list_dir"]["func"]
        out = list_dir({"depth": 4})
        check("symlink dir is listed", "link/" in out.splitlines(), repr(out))
        check("symlink is not followed",
              "secret.txt" not in out and "link/secret.txt" not in out, repr(out))
    finally:
        shutil.rmtree(d)
        shutil.rmtree(outside)


def t_entry_cap_marks_truncation():
    reg, d = make_registry()
    try:
        # 520 two-letter names: 500 entries fit under the char cap too, so
        # what is exercised here is the entry cap and its marker.
        names = [a + b for a in "abcdefghijklmnopqrstuvwxyz"
                 for b in "abcdefghijklmnopqrstuvwxyz"][:520]
        for name in names:
            Path(d, name).write_text("x")
        out = reg["list_dir"]["func"]({"depth": 1})
        lines = out.splitlines()
        check("listing capped at 500 entries plus marker",
              len(lines) == 501, str(len(lines)))
        check("cap is announced, not silent",
              "truncated" in lines[-1], lines[-1])
    finally:
        shutil.rmtree(d)


def t_bad_depth_is_a_recoverable_tool_error():
    reg, d = make_registry()
    try:
        list_dir = reg["list_dir"]["func"]
        for bad in (0, -1, 99):
            try:
                list_dir({"depth": bad})
                check(f"depth {bad} rejected", False, "no error raised")
            except ToolError as e:
                check(f"depth {bad} rejected", "depth" in str(e), str(e))
    finally:
        shutil.rmtree(d)


def t_harness_dir_not_listed():
    # .harness/ is the harness's own bookkeeping (v0.8.1 run records), not
    # user data; a deep listing of the workspace must not surface it.
    reg, d = make_registry()
    try:
        build_tree(d)
        p = Path(d, ".harness", "runs", "20261002-181700")
        p.mkdir(parents=True)
        (p / "step-01.md").write_text("record")
        out = reg["list_dir"]["func"]({"depth": 4})
        check(".harness absent from deep listing", ".harness" not in out, repr(out))
        out1 = reg["list_dir"]["func"]({})
        check(".harness absent from shallow listing", ".harness" not in out1, repr(out1))
    finally:
        shutil.rmtree(d)


def t_model_can_choose_deep_via_loop():
    d = tempfile.mkdtemp(prefix="listdir-loop-")
    try:
        build_tree(d)
        cfg = Config(workspace=d, max_steps=8, max_consecutive_errors=3)
        responses = iter([
            {"role": "assistant", "content": "Listing deep.",
             "tool_calls": [{"function": {"name": "list_dir",
                                          "arguments": {"path": ".", "depth": 3}}}]},
            {"role": "assistant", "content": "DONE: listed everything."},
        ])

        def fake(messages, tool_defs):
            try:
                return next(responses)
            except StopIteration:
                return {"role": "assistant", "content": "SUMMARY: stopped."}

        r = loop.run("list everything", cfg, chat_fn=fake)
        check("loop run done", r.status == "done", r.status)
        check("model got nested paths",
              "sub/deep/c.txt" in (r.steps[0].result or ""), (r.steps[0].result or "")[:200])
    finally:
        shutil.rmtree(d)


if __name__ == "__main__":
    for fn in [t_default_is_shallow_and_unchanged_for_root, t_depth_walks_down,
               t_subdir_listing_returns_usable_paths, t_symlink_dirs_listed_not_followed,
               t_entry_cap_marks_truncation, t_bad_depth_is_a_recoverable_tool_error,
               t_harness_dir_not_listed, t_model_can_choose_deep_via_loop]:
        fn()
    print("\nAll list_dir tests passed.")
