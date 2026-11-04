#!/usr/bin/env python3
"""Tests for the run_code tool.

Field failure behind it (2026-11-04): gemma4:e2b asked directly
"How many E's are in seventeen" answered 5, with a confident miscount
in its reasoning. Asked to write a counting function instead, it wrote
a correct regex counter that, when run, returns 4. Same model, same
question, different reliability by mode: a direct answer is attestation,
an executed program is checkable. The gap was vocabulary and ownership:
the toolset could write a file and run a shell command, but only in two
separate steps, so the model had to sequence the write and the run and
then transcribe the result into its answer, and transcription is where
a correct computation becomes a wrong claim.

run_code collapses that into one harness-owned step. The model supplies
Python code, the harness writes it to a jailed internal script, runs it
with the same timeout and blocked-command guards as exec, and, when an
output path is given, writes the program's exact stdout to that file
itself. The Then verifier then checks the file, never the model's prose
about the file.

Run: python3 tests/test_run_code.py
"""
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness import loop
from harness.config import Config
from harness.discovery import READ_ONLY_TOOLS
from harness.tools import Jail, Refusal, ToolError, make_tools


def check(name, condition, detail=""):
    print(("PASS " if condition else "FAIL ") + name + (f" -- {detail}" if detail and not condition else ""))
    if not condition:
        raise AssertionError(name)


def make_registry(exec_timeout=30, blocked=()):
    workspace_dir = tempfile.mkdtemp(prefix="run-code-test-")
    registry = make_tools(Jail(workspace_dir), blocked, exec_timeout, 2000)
    return registry, workspace_dir


def t_run_code_returns_program_output():
    registry, workspace_dir = make_registry()
    try:
        result = registry["run_code"]["func"]({
            "code": "print(sum(1 for character in 'seventeen' if character == 'e'))"})
        check("result reports exit zero", result.startswith("exit=0"), result)
        check("result carries the computed count", "\n4" in result, result)
    finally:
        shutil.rmtree(workspace_dir)


def t_run_code_writes_exact_stdout_to_output_path():
    registry, workspace_dir = make_registry()
    try:
        result = registry["run_code"]["func"]({
            "code": "print(sum(1 for character in 'seventeen' if character == 'e'))",
            "output_path": "answer.txt"})
        check("result names the output file", "answer.txt" in result, result)
        check("file holds the exact stdout, not a transcription",
              Path(workspace_dir, "answer.txt").read_text() == "4\n",
              repr(Path(workspace_dir, "answer.txt").read_text()))
    finally:
        shutil.rmtree(workspace_dir)


def t_run_code_failure_writes_no_output_file():
    registry, workspace_dir = make_registry()
    try:
        result = registry["run_code"]["func"]({
            "code": "raise ValueError('deliberate failure')",
            "output_path": "answer.txt"})
        check("result reports a nonzero exit", result.startswith("exit=1"), result)
        check("result carries the program error", "ValueError" in result, result)
        check("no output file on failure",
              not Path(workspace_dir, "answer.txt").exists())
    finally:
        shutil.rmtree(workspace_dir)


def t_run_code_output_path_escape_refused():
    registry, workspace_dir = make_registry()
    try:
        try:
            registry["run_code"]["func"]({
                "code": "print('x')", "output_path": "../escape.txt"})
            check("escape refused", False, "no exception raised")
        except Refusal:
            check("escape refused", True)
        check("nothing written outside",
              not Path(workspace_dir).parent.joinpath("escape.txt").exists())
    finally:
        shutil.rmtree(workspace_dir)


def t_run_code_output_path_inside_harness_store_refused():
    # Issue #22: .harness is run records, not deliverables. A step that
    # saves its output there has delivered nothing, so refuse at the
    # moment of the write, exactly as write_file does.
    registry, workspace_dir = make_registry()
    try:
        try:
            registry["run_code"]["func"]({
                "code": "print('x')", "output_path": ".harness/answer.txt"})
            check("harness store refused", False, "no exception raised")
        except Refusal:
            check("harness store refused", True)
    finally:
        shutil.rmtree(workspace_dir)


def t_run_code_blocked_code_refused():
    registry, workspace_dir = make_registry(blocked=("rm -rf /",))
    try:
        try:
            registry["run_code"]["func"]({"code": "import os\nos.system('rm -rf /')"})
            check("blocked code refused", False, "no exception raised")
        except Refusal:
            check("blocked code refused", True)
    finally:
        shutil.rmtree(workspace_dir)


def t_run_code_empty_code_rejected():
    registry, workspace_dir = make_registry()
    try:
        try:
            registry["run_code"]["func"]({"code": "   "})
            check("empty code rejected", False, "no exception raised")
        except ToolError:
            check("empty code rejected", True)
    finally:
        shutil.rmtree(workspace_dir)


def t_run_code_generated_script_not_listed_as_deliverable():
    registry, workspace_dir = make_registry()
    try:
        registry["run_code"]["func"]({
            "code": "print('4')", "output_path": "answer.txt"})
        listing = registry["list_dir"]["func"]({"path": "."})
        check("answer file is listed", "answer.txt" in listing, listing)
        check("generated script is not listed",
              ".harness" not in listing and "run_code" not in listing, listing)
    finally:
        shutil.rmtree(workspace_dir)


def t_run_code_second_call_overwrites_output():
    registry, workspace_dir = make_registry()
    try:
        registry["run_code"]["func"]({
            "code": "print('first')", "output_path": "answer.txt"})
        registry["run_code"]["func"]({
            "code": "print('second')", "output_path": "answer.txt"})
        check("second run replaces the first output",
              Path(workspace_dir, "answer.txt").read_text() == "second\n",
              repr(Path(workspace_dir, "answer.txt").read_text()))
    finally:
        shutil.rmtree(workspace_dir)


def t_run_code_times_out_like_exec():
    registry, workspace_dir = make_registry(exec_timeout=1)
    try:
        try:
            registry["run_code"]["func"]({
                "code": "import time\ntime.sleep(5)"})
            check("timeout raised", False, "no exception raised")
        except ToolError as error:
            check("timeout raised", "timed out" in str(error), str(error))
    finally:
        shutil.rmtree(workspace_dir)


def t_run_code_registered_and_described_for_the_model():
    registry, workspace_dir = make_registry()
    try:
        check("run_code registered", "run_code" in registry, str(sorted(registry)))
        description = registry["run_code"]["description"].lower()
        check("description says the harness runs the code",
              "run" in description, description)
        check("description steers away from prose answers",
              "instead" in description and "prose" in description, description)
        parameters = registry["run_code"]["parameters"]
        check("code is required", "code" in parameters["required"],
              str(parameters["required"]))
        check("output_path is optional",
              "output_path" not in parameters["required"],
              str(parameters["required"]))
    finally:
        shutil.rmtree(workspace_dir)


def t_run_code_is_not_a_discovery_tool():
    check("run_code not in READ_ONLY_TOOLS",
          "run_code" not in READ_ONLY_TOOLS, str(READ_ONLY_TOOLS))


def t_run_code_through_the_loop_writes_the_answer():
    workspace_dir = tempfile.mkdtemp(prefix="run-code-loop-")
    try:
        config = Config(workspace=workspace_dir, max_steps=8, step_max_steps=6,
                        max_consecutive_errors=3)
        queued_calls = [
            {"role": "assistant", "content": "Counting with code.",
             "tool_calls": [{"function": {"name": "run_code",
                                          "arguments": {
                                              "code": "print(sum(1 for character in 'seventeen' if character == 'e'))",
                                              "output_path": "answer.txt"}}}]},
        ]

        def fake_chat(messages, tool_defs):
            if queued_calls:
                return queued_calls.pop(0)
            return {"role": "assistant", "content": "DONE: counted with code."}

        run_result = loop.run("count the E's in seventeen with code", config,
                              chat_fn=fake_chat)
        check("loop run done", run_result.status == "done", run_result.status)
        check("loop run wrote the computed answer",
              Path(workspace_dir, "answer.txt").read_text() == "4\n",
              repr(Path(workspace_dir, "answer.txt").read_text()))
    finally:
        shutil.rmtree(workspace_dir)


if __name__ == "__main__":
    for test_function in [t_run_code_returns_program_output,
                          t_run_code_writes_exact_stdout_to_output_path,
                          t_run_code_failure_writes_no_output_file,
                          t_run_code_output_path_escape_refused,
                          t_run_code_output_path_inside_harness_store_refused,
                          t_run_code_blocked_code_refused,
                          t_run_code_empty_code_rejected,
                          t_run_code_generated_script_not_listed_as_deliverable,
                          t_run_code_second_call_overwrites_output,
                          t_run_code_times_out_like_exec,
                          t_run_code_registered_and_described_for_the_model,
                          t_run_code_is_not_a_discovery_tool,
                          t_run_code_through_the_loop_writes_the_answer]:
        test_function()
    print("\nAll run_code tests passed.")
