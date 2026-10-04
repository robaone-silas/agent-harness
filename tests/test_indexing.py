"""Tests for the indexing primitive.

Indexing as a harness primitive: when a step's Then carries
`"<target>" indexes the files from step N`, the harness builds the
index itself instead of asking the executor to read every file and
compile the document inside one sub-run (the shape that kept failing
in the field: reads eating the turn budget, run-ahead steps, omitted
files). The harness enumerates step N's recorded items, reads each
file, makes one small model call per file for its one-line
description, and writes the target with one entry per file.
Completeness is by construction, and the `indexes` verifier then
checks the target strictly: every source item must appear.

If the primitive cannot complete (a source file unreadable, a
description call returning nothing), execution falls back to the
normal model sub-run for the step. The primitive is an accelerator,
never a gate.

Run: python3 tests/test_indexing.py
"""
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness import indexing, planner, verify as _verify
from harness.config import Config
from harness.planner import to_plan_steps
from harness import gherkin
from harness.tools import Jail

FILES = {
    "a.txt": "Alpha file about apples and orchards.\n",
    "b.txt": "Beta file about boats and harbors.\n",
    "c.txt": "Gamma file about gardens and herbs.\n",
}

PLAN = """Feature: Index the files
  Scenario: Step 1 - List the files
    When I list the files in the folder
    Then the output lists the files
  Scenario: Step 2 - Build the index
    When I compile the index of the files from step 1
    Then "audit-index.md" indexes the files from step 1
"""


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name
          + (f" -- {detail}" if detail and not cond else ""))
    assert cond, f"{name}: {detail}"


def _workspace():
    d = tempfile.mkdtemp(prefix="indexing-test-")
    for name, content in FILES.items():
        Path(d, name).write_text(content)
    return d


def t_clean_description():
    check("plain description passes through",
          indexing.clean_description("About apples and orchards.",
                                     "a.txt") == "About apples and orchards.")
    check("filename prefix is stripped",
          indexing.clean_description("a.txt: About apples.", "a.txt")
          == "About apples.")
    check("bullet and quotes are stripped",
          indexing.clean_description('- "About boats."', "b.txt")
          == "About boats.")
    check("only the first line is kept",
          indexing.clean_description("About gardens.\nMore detail here.",
                                     "c.txt") == "About gardens.")
    check("empty stays empty",
          indexing.clean_description("  \n ", "a.txt") == "")


def t_render_index():
    text = indexing.render_index(
        [("a.txt", "About apples."), ("b.txt", "About boats.")])
    lines = [ln for ln in text.splitlines() if ln.startswith("- ")]
    check("one entry line per record, in order",
          lines == ["- a.txt: About apples.", "- b.txt: About boats."],
          text)


def t_build_index_complete_and_fallback():
    d = _workspace()
    try:
        records = indexing.build_index(
            Path(d), ["a.txt", "b.txt", "c.txt"],
            lambda name, content: f"Summary of {name}.")
        check("build returns one record per file, in order",
              [r[0] for r in records] == ["a.txt", "b.txt", "c.txt"],
              str(records))
        none = indexing.build_index(
            Path(d), ["a.txt", "b.txt"],
            lambda name, content: "" if name == "b.txt" else "Fine.")
        check("an empty description declines the whole build",
              none is None, str(none))
        missing = indexing.build_index(
            Path(d), ["a.txt", "nope.txt"],
            lambda name, content: "Fine.")
        check("an unreadable file declines the whole build",
              missing is None, str(missing))
    finally:
        shutil.rmtree(d)


def t_indexes_verifier():
    parsed = _verify.parse_then('"audit-index.md" indexes the files from step 1')
    check("indexes clause parses to the indexes verifier",
          parsed is not None and parsed[0] == "indexes"
          and parsed[1] == "audit-index.md", str(parsed))
    d = _workspace()
    try:
        jail = Jail(d)
        ctx = _verify.VerifyContext(
            jail=jail, step_id=2, task="index everything",
            prior_items={1: ["a.txt", "b.txt", "c.txt"]})
        Path(d, "audit-index.md").write_text(
            "- a.txt: About apples.\n- b.txt: About boats.\n")
        res = _verify.verify_step(
            ['"audit-index.md" indexes the files from step 1'], ctx)
        check("indexes is strict: a missing item fails",
              not res.ok, str([(c.ok, c.detail) for c in res.checks]))
        Path(d, "audit-index.md").write_text(
            "- a.txt: About apples.\n- b.txt: About boats.\n"
            "- c.txt: About gardens.\n")
        res2 = _verify.verify_step(
            ['"audit-index.md" indexes the files from step 1'], ctx)
        check("indexes passes when every item appears",
              res2.ok, str([(c.ok, c.detail) for c in res2.checks]))
        ctx_none = _verify.VerifyContext(
            jail=jail, step_id=2, task="index everything", prior_items={})
        res3 = _verify.verify_step(
            ['"audit-index.md" indexes the files from step 1'], ctx_none)
        check("no recorded items: passes as not checked (like covers)",
              res3.ok, str([(c.ok, c.detail) for c in res3.checks]))
    finally:
        shutil.rmtree(d)


def _run_plan(describe_fail_for=None):
    """Run PLAN with a scripted model. Returns (result, prompts, dir).

    The scripted model: plans with PLAN; step 1 lists the folder with
    list_dir then declares DONE; description calls (no tools, prompt
    carries the indexing marker) answer per file; step 2's executor
    sub-run, if it ever starts, is captured so tests can see whether
    the primitive or the fallback handled the step."""
    d = _workspace()
    cfg = Config(workspace=d, max_steps=12, step_max_steps=6,
                 max_consecutive_errors=3)
    prompts = {}
    acted = set()

    def fake(messages, tool_defs):
        joined = "\n".join(m.get("content") or "" for m in messages)
        if tool_defs is None and "Break the task" in joined:
            return {"role": "assistant", "content": PLAN}
        if tool_defs is None and indexing.DESCRIBE_MARKER in joined:
            name = next((n for n in FILES if f'"{n}"' in joined), "?")
            if name == describe_fail_for:
                return {"role": "assistant", "content": ""}
            return {"role": "assistant",
                    "content": f"A file named {name} with its own topic."}
        if tool_defs is None:
            return {"role": "assistant", "content": "DONE: ok."}
        task_msg = messages[1]["content"] if len(messages) > 1 else ""
        sid = next((s for s in (1, 2)
                    if f"You are executing step {s} of 2" in task_msg), None)
        if sid is None:
            return {"role": "assistant", "content": "DONE: done."}
        prompts.setdefault(sid, task_msg)
        if sid in acted:
            return {"role": "assistant", "content": f"DONE: step {sid}."}
        acted.add(sid)
        if sid == 1:
            return {"role": "assistant", "content": "Listing.",
                    "tool_calls": [{"function": {
                        "name": "list_dir", "arguments": {"path": "."}}}]}
        return {"role": "assistant", "content": "Writing the index myself.",
                "tool_calls": [{"function": {
                    "name": "write_file",
                    "arguments": {"path": "audit-index.md",
                                  "content": "- a.txt: x\n- b.txt: y\n"
                                             "- c.txt: z\n"}}}]}

    r = planner.run_planned("Index the files in this folder", cfg,
                            chat_fn=fake)
    return r, prompts, d


def t_primitive_executes_the_index_step():
    r, prompts, d = _run_plan()
    try:
        check("run done", r.status == "done", r.status)
        check("step 2 never started an executor sub-run",
              2 not in prompts, str(list(prompts)))
        target = Path(d, "audit-index.md")
        check("index file exists", target.exists())
        text = target.read_text() if target.exists() else ""
        check("index has one entry per fixture file",
              all(f"- {n}: " in text for n in FILES), text)
        check("entries carry the per-file descriptions",
              "with its own topic" in text, text)
    finally:
        shutil.rmtree(d)


def t_fallback_when_a_description_fails():
    r, prompts, d = _run_plan(describe_fail_for="b.txt")
    try:
        check("run still completes via the fallback sub-run",
              r.status == "done", r.status)
        check("step 2 executor sub-run ran after the primitive declined",
              2 in prompts, str(list(prompts)))
        check("fallback content landed",
              Path(d, "audit-index.md").exists())
    finally:
        shutil.rmtree(d)


def t_planner_prompt_teaches_indexes():
    prompt = planner.build_planner_prompt("Make an index of the files",
                                          ["list_dir", "read_file"],
                                          workspace=None)
    check("planner prompt names the indexes clause",
          "indexes the files from step" in prompt, prompt[:200])
    check("planner prompt says the harness builds the index",
          "builds that file itself" in prompt, "")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("t_") and callable(v)]
    for fn in fns:
        fn()
    print("\nAll indexing tests passed.")
