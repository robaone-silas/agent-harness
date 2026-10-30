#!/usr/bin/env python3
"""Tests for issue #49: a truncated read must name the next page.

The failure: step 4's record nested earlier records, so its own payload
(the write_file line for parts-order-list.txt, carrying
"keybed assembly: 1") sat past read_file's 2000-char truncation. The
truncated result said only how many chars were cut, never where the
rest was reachable, so the next step repeated the identical read five
times, hit the loop breaker, and the run failed with a correct
deliverable already in place.

The fix under test: a truncated read_file result names the line offset
of the next page, and reading at that offset returns the payload. The
same pointer covers the line-limit page: when the 200-line page ends
before the file does, the result says so and names the next offset.
A short file reads exactly as before, with no pointer added.

Run: python3 tests/test_issue49_read_paging.py
"""
import re
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness.tools import Jail, make_tools


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        raise AssertionError(name)


def make_registry():
    d = tempfile.mkdtemp(prefix="issue49-test-")
    jail = Jail(d)
    reg = make_tools(jail, (), 30, 2000)
    return reg, d


def nested_record_like_step04():
    """A record shaped like the 2026-10-10 step-04.md: nested earlier
    records push the step's own write_file payload past char 2000."""
    lines = ["# Step 4", "", "## Instruction", "Write the compiled parts order list", ""]
    lines += ["### Tool calls", ""]
    # Nested earlier records: filler lines that stand in for the
    # embedded step-02.md (which itself embedded step-01.md).
    for i in range(90):
        lines.append(f"nested prior record line {i:02d} "
                     f"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
    lines += ["#### write_file {\"content\": \"Parts Order List:\\n"
              "- keybed assembly: 1\", \"path\": \"parts-order-list.txt\"}",
              "", "Result:", "", "```", "wrote 38 bytes", "```", ""]
    lines += ["### Answer", "", "DONE: compiled and wrote the list.", ""]
    text = "\n".join(lines)
    assert len(text) > 2000, len(text)
    assert text.find("keybed assembly: 1") > 2000, text.find("keybed assembly: 1")
    return text


def t_truncated_read_names_next_offset_and_next_page_has_payload():
    reg, d = make_registry()
    try:
        Path(d, "step-04.md").write_text(nested_record_like_step04())
        first = reg["read_file"]["func"]({"path": "step-04.md"})
        check("first page truncates before the payload",
              "keybed assembly: 1" not in first and "truncated" in first, first[-200:])
        m = re.search(r"offset=(\d+)", first)
        check("truncated result names a next offset", m is not None, first[-200:])
        check("named offset advances past the first page", int(m.group(1)) > 0, first[-200:])
        # Follow the named offsets, as the next step would: each page
        # is a different call, so no repeat loop, and the walk ends
        # at the payload instead of at the loop breaker.
        seen, offset, found = [first], int(m.group(1)), False
        for _ in range(10):
            page = reg["read_file"]["func"](
                {"path": "step-04.md", "offset": offset})
            check("each page is a new call, not a repeat", page not in seen, page[:100])
            seen.append(page)
            if "keybed assembly: 1" in page:
                found = True
                break
            m = re.search(r"offset=(\d+)", page)
            check("each cut page names its own next offset", m is not None, page[-200:])
            check("offsets keep advancing", int(m.group(1)) > offset, page[-200:])
            offset = int(m.group(1))
        check("following the named offsets reaches the payload", found)
    finally:
        shutil.rmtree(d)


def t_line_page_names_next_offset():
    reg, d = make_registry()
    try:
        body = "\n".join(f"line {i:03d}" for i in range(250))
        Path(d, "long.txt").write_text(body)
        first = reg["read_file"]["func"]({"path": "long.txt"})
        check("first page stops at the line limit", "line 199" in first and "line 200" not in first,
              first[-200:])
        m = re.search(r"offset=(\d+)", first)
        check("line-limited page names a next offset", m is not None, first[-200:])
        check("named offset is the first unseen line", int(m.group(1)) == 200, first[-200:])
        second = reg["read_file"]["func"](
            {"path": "long.txt", "offset": int(m.group(1))})
        check("page at the named offset returns the tail",
              "line 200" in second and "line 249" in second, second[:200])
    finally:
        shutil.rmtree(d)


def t_short_file_unchanged():
    reg, d = make_registry()
    try:
        Path(d, "small.txt").write_text("hello\nworld\n")
        out = reg["read_file"]["func"]({"path": "small.txt"})
        check("short file reads in full", out == "hello\nworld", repr(out))
        check("short file carries no paging pointer", "offset=" not in out, repr(out))
    finally:
        shutil.rmtree(d)


if __name__ == "__main__":
    for fn in [t_truncated_read_names_next_offset_and_next_page_has_payload,
               t_line_page_names_next_offset,
               t_short_file_unchanged]:
        fn()
    print("\nAll issue-49 read-paging tests passed.")
