#!/usr/bin/env python3
"""Tests for the v0.8.1 step-output store (issue #2).

The bug: plan steps lost data produced by earlier steps because only a
250-char DONE: summary crossed the step boundary; tool outputs (the actual
file list, command output, etc.) never did.

The fix under test: the harness (not the model) writes every step's full
record, verbatim tool outputs included, to a deterministic file:

    <workspace>/.harness/runs/<YYYYMMDD-HHMMSS>/step-NN.md
    <workspace>/.harness/latest.txt   (newest run folder name)

and each step's prompt names the exact prior-step files so the model can
read_file them when it needs the real data.

Run: python3 tests/test_runstore.py
"""
import re
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness import planner
from harness.config import Config

PLAN_GHERKIN = """Feature: Organize documents
  Scenario: Step 1 - List the documents
    When List the files in the workspace
    Then inventory is recorded
  Scenario: Step 2 - Suggest an organization
    When Suggest an organization based on the file list
    Then suggestion is recorded
"""


def make_cfg(**kw):
    d = tempfile.mkdtemp(prefix="runstore-test-")
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


# ---------------------------------------------------------------- unit: RunStore

def t_run_folder_naming():
    from harness.runstore import RunStore
    d = tempfile.mkdtemp(prefix="runstore-unit-")
    try:
        store = RunStore(d, now=datetime(2026, 10, 2, 18, 17, 0))
        check("run id is timestamp", store.run_id == "20261002-181700", store.run_id)
        check("run id sorts lexicographically",
              re.fullmatch(r"\d{8}-\d{6}", store.run_id) is not None)
        check("run dir exists", (Path(d) / ".harness" / "runs" / "20261002-181700").is_dir())
        check("latest.txt at .harness level",
              (Path(d) / ".harness" / "latest.txt").read_text().strip() == "20261002-181700")
    finally:
        shutil.rmtree(d)


def t_run_folder_collision():
    from harness.runstore import RunStore
    d = tempfile.mkdtemp(prefix="runstore-unit-")
    try:
        now = datetime(2026, 10, 2, 18, 17, 0)
        s1 = RunStore(d, now=now)
        s2 = RunStore(d, now=now)
        s3 = RunStore(d, now=now)
        check("first run plain", s1.run_id == "20261002-181700", s1.run_id)
        check("collision gets -2", s2.run_id == "20261002-181700-2", s2.run_id)
        check("second collision gets -3", s3.run_id == "20261002-181700-3", s3.run_id)
        check("latest.txt tracks newest",
              (Path(d) / ".harness" / "latest.txt").read_text().strip() == "20261002-181700-3")
        check("all run dirs exist",
              all((Path(d) / ".harness" / "runs" / rid).is_dir()
                  for rid in (s1.run_id, s2.run_id, s3.run_id)))
    finally:
        shutil.rmtree(d)


def t_step_file_naming_and_paths():
    from harness.runstore import RunStore
    d = tempfile.mkdtemp(prefix="runstore-unit-")
    try:
        store = RunStore(d, now=datetime(2026, 10, 2, 18, 17, 0))
        check("step path zero padded", store.rel_path(1) == ".harness/runs/20261002-181700/step-01.md",
              store.rel_path(1))
        check("step path step 12", store.rel_path(12) == ".harness/runs/20261002-181700/step-12.md",
              store.rel_path(12))
        check("prior paths empty for step 1", store.prior_paths(1) == [])
        check("prior paths for step 3",
              store.prior_paths(3) == [".harness/runs/20261002-181700/step-01.md",
                                       ".harness/runs/20261002-181700/step-02.md"],
              str(store.prior_paths(3)))
    finally:
        shutil.rmtree(d)


def t_step_file_contents_verbatim():
    from harness.runstore import RunStore
    from harness.loop import Step
    d = tempfile.mkdtemp(prefix="runstore-unit-")
    try:
        store = RunStore(d, now=datetime(2026, 10, 2, 18, 17, 0))
        long_listing = "\n".join(f"doc-{i:02d}.pdf" for i in range(40))
        sub_steps = [
            Step(n=1, thought="Listing.", tool="list_dir",
                 args={"path": "."}, result=long_listing, path="native"),
            Step(n=2, thought="", tool=None, args=None, result=None, path="final"),
        ]
        store.write_step(
            step_id=1,
            instruction="List the files in the workspace",
            given="",
            done_when="inventory is recorded",
            answer="DONE: listed the files.",
            sub_steps=sub_steps,
            status="done",
            verify_checks=[{"then": "inventory is recorded", "verifier": "attest",
                            "ok": True, "detail": "no verifier matched"}],
            verify_waived=False,
        )
        p = Path(d) / ".harness" / "runs" / "20261002-181700" / "step-01.md"
        check("step file exists", p.is_file())
        text = p.read_text()
        check("step file has instruction", "List the files in the workspace" in text)
        check("step file has done_when", "inventory is recorded" in text)
        check("step file has tool name", "list_dir" in text)
        check("step file has tool args", '"path"' in text or "path" in text)
        check("step file has FULL verbatim tool output (not 250-char summary)",
              long_listing in text and "doc-39.pdf" in text)
        check("step file has DONE answer", "DONE: listed the files." in text)
        check("step file has verification outcome", "attest" in text)
        check("step file has status", "done" in text)
    finally:
        shutil.rmtree(d)


# ------------------------------------------------------- integration: planner

def t_planned_run_creates_store():
    cfg, d = make_cfg()
    try:
        # Real files on disk so step 1's list_dir output is real data.
        for name in ("tax-2025.pdf", "lease.docx", "photo-cat.jpg"):
            Path(d, name).write_text("x")
        fake = scripted([
            {"role": "assistant", "content": PLAN_GHERKIN},
            {"role": "assistant", "content": "Listing.",
             "tool_calls": [{"function": {"name": "list_dir", "arguments": {"path": "."}}}]},
            {"role": "assistant", "content": "DONE: listed."},
            {"role": "assistant", "content": "Suggesting.",
             "tool_calls": [{"function": {"name": "write_file",
                                          "arguments": {"path": "suggestion.txt",
                                                        "content": "by type"}}}]},
            {"role": "assistant", "content": "DONE: suggested."},
            {"role": "assistant", "content": "DONE: both steps complete."},
        ])
        r = planner.run_planned("organize my documents", cfg, chat_fn=fake)
        check("planned status done", r.status == "done", r.status)
        harness_dir = Path(d) / ".harness"
        check(".harness created", harness_dir.is_dir())
        latest = (harness_dir / "latest.txt").read_text().strip()
        check("latest.txt names a run", re.fullmatch(r"\d{8}-\d{6}(-\d+)?", latest) is not None, latest)
        run_dir = harness_dir / "runs" / latest
        check("step-01.md exists", (run_dir / "step-01.md").is_file())
        check("step-02.md exists", (run_dir / "step-02.md").is_file())
        step1 = (run_dir / "step-01.md").read_text()
        check("step-01 records the tool call", "list_dir" in step1)
        check("step-01 records verbatim listing incl. real filenames",
              "tax-2025.pdf" in step1 and "lease.docx" in step1 and "photo-cat.jpg" in step1,
              step1[:400])
        check("run exposes run_id", getattr(r, "run_id", None) == latest,
              str(getattr(r, "run_id", None)))
        check("steps expose output paths",
              r.steps[0].output_path == f".harness/runs/{latest}/step-01.md",
              str(getattr(r.steps[0], "output_path", None)))
        check("plan_dict includes output_path",
              r.plan_dict()["steps"][0].get("output_path") == f".harness/runs/{latest}/step-01.md")
    finally:
        shutil.rmtree(d)


def t_step2_prompt_names_step1_file_and_step2_can_read_it():
    cfg, d = make_cfg()
    try:
        for name in ("alpha.txt", "beta.txt"):
            Path(d, name).write_text("x")
        seen_prompts = []
        state = {"step2_read_done": False}

        def fake(messages, tool_defs):
            if tool_defs is None and "Break the task" in messages[0]["content"]:
                return {"role": "assistant", "content": PLAN_GHERKIN}
            if tool_defs is None:
                return {"role": "assistant", "content": "DONE: ok."}
            content = messages[-1]["content"]
            # Capture the scoped step prompts (first user message of each sub-run
            # is messages[1]; the harness task sits there).
            task_msg = messages[1]["content"] if len(messages) > 1 else ""
            if "You are executing step" in task_msg:
                seen_prompts.append(task_msg)
            if "You are executing step 1" in task_msg and not state.get("s1"):
                state["s1"] = True
                return {"role": "assistant", "content": "Listing.",
                        "tool_calls": [{"function": {"name": "list_dir",
                                                     "arguments": {"path": "."}}}]}
            if "You are executing step 2" in task_msg and not state["step2_read_done"]:
                # The model follows the prompt's pointer and reads step 1's file.
                state["step2_read_done"] = True
                # Find the exact path the prompt named.
                m = re.search(r"\.harness/runs/\S+/step-01\.md", task_msg)
                assert m, f"step 2 prompt did not name step-01.md path: {task_msg[:600]}"
                state["named_path"] = m.group(0)
                return {"role": "assistant", "content": "Reading prior step.",
                        "tool_calls": [{"function": {"name": "read_file",
                                                     "arguments": {"path": m.group(0)}}}]}
            return {"role": "assistant", "content": "DONE: step done."}

        r = planner.run_planned("organize my documents", cfg, chat_fn=fake)
        check("planned status done", r.status == "done", r.status)
        step2_prompts = [p for p in seen_prompts if "You are executing step 2" in p]
        check("step2 prompt seen", len(step2_prompts) >= 1, str(len(seen_prompts)))
        step2_prompt = step2_prompts[0]
        check("step2 prompt names exact step-01 path",
              bool(re.search(r"\.harness/runs/\d{8}-\d{6}(-\d+)?/step-01\.md", step2_prompt)),
              step2_prompt[:600])
        check("step2 prompt tells model to read it for exact data",
              "read_file" in step2_prompt, step2_prompt[:600])
        check("model actually read the step file", state["step2_read_done"])
        # And the data survived: step 2's own record shows the read returned
        # the step-1 record, which itself contains the verbatim listing.
        latest = (Path(d) / ".harness" / "latest.txt").read_text().strip()
        step2 = (Path(d) / ".harness" / "runs" / latest / "step-02.md").read_text()
        check("step-02 record shows read_file of step-01", "read_file" in step2 and "step-01.md" in step2)
        check("data crossed the boundary: step-02 saw alpha.txt via step-01 record",
              "alpha.txt" in step2, step2[:600])
    finally:
        shutil.rmtree(d)


def t_failed_step_still_written():
    cfg, d = make_cfg()
    try:
        bad = {"role": "assistant", "content": "Trying.",
               "tool_calls": [{"function": {"name": "nope", "arguments": {}}}]}
        fake = scripted([
            {"role": "assistant", "content": PLAN_GHERKIN},
            bad, bad, bad,
            {"role": "assistant", "content": "DONE: stopped early."},
        ])
        r = planner.run_planned("organize my documents", cfg, chat_fn=fake)
        check("step_failed status", r.status == "step_failed", r.status)
        latest = (Path(d) / ".harness" / "latest.txt").read_text().strip()
        run_dir = Path(d) / ".harness" / "runs" / latest
        check("failed step-01.md still written", (run_dir / "step-01.md").is_file())
        check("no step-02.md for never-run step", not (run_dir / "step-02.md").exists())
        text = (run_dir / "step-01.md").read_text()
        check("failed step records its status", "failed" in text)
    finally:
        shutil.rmtree(d)


if __name__ == "__main__":
    for fn in [t_run_folder_naming, t_run_folder_collision,
               t_step_file_naming_and_paths, t_step_file_contents_verbatim,
               t_planned_run_creates_store,
               t_step2_prompt_names_step1_file_and_step2_can_read_it,
               t_failed_step_still_written]:
        fn()
    print("\nAll runstore tests passed.")
