"""Tests for the issue #22 fix: no false success on a missing deliverable.

Field failure (daily persona run 2026-10-04, Daniel Okafor): step 2
wrote the strategy text into its own step-record path under
.harness/runs/ instead of organization-strategy.md; its Then
(`"organization-strategy.md" contains the suggested organization
strategy`) matched no verifier because the expected text was prose,
so it attested ok; the run reported done with the deliverable never
created. Three fixes, one per gap:

1. Existence floor under attestation (verify.verify_step): a clause
   that names a quoted file cannot pass on trust while that file is
   missing. It fails with exact feedback, feeding the step retry.
2. The .harness store is not a write target (tools): write_file,
   append_file, and edit_file refuse paths under .harness/; reads
   stay open.
3. End-of-run deliverable audit (verify.promised/missing +
   planner.execute_plan): before a run may report done, every file
   the plan's Thens promised must exist, waived steps notwithstanding.

Run: python3 tests/test_false_success.py
"""
import re
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness import gherkin, planner, verify
from harness.config import Config
from harness.planner import PlanStep, to_plan_steps
from harness.tools import Jail, Refusal, make_tools

DANIEL_TASK = (
    "Suggest an organization strategy for these documents based only on "
    "the filenames. Do not read the contents of the files. Do not move, "
    "rename, delete, or modify any existing file. Write the organization "
    "strategy to organization-strategy.md."
)

DANIEL_PLAN = """Feature: Document organization strategy
  Scenario: Step 1 - List all documents
    Intent: know exactly which documents the organization strategy must cover
    When I list the files in the folder
    Then the output lists all files in the workspace
  Scenario: Step 2 - Suggest the organization strategy
    Intent: suggest an organization strategy for these documents based only on the filenames
    When I suggest an organization strategy based on the listed files
    Then "organization-strategy.md" contains the suggested organization strategy
"""

DANIEL_THEN = ('"organization-strategy.md" contains the suggested '
               'organization strategy')


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name
          + (f" -- {info}" if info and not cond else ""))
    assert cond, f"{name}: {info}"


def _ctx(d, task=""):
    return verify.VerifyContext(jail=Jail(d), step_id=2, task=task)


# ------------------------------------------------------- Fix 1: floor

def t_floor_fails_attestation_when_named_file_missing():
    d = tempfile.mkdtemp(prefix="issue22-")
    try:
        res = verify.verify_step([DANIEL_THEN], _ctx(d, DANIEL_TASK))
        check("attestation cannot pass while the named file is missing",
              not res.ok, str(res.checks))
        check("the failure names the missing file",
              "organization-strategy.md" in res.checks[0].detail,
              res.checks[0].detail)
    finally:
        shutil.rmtree(d)


def t_floor_attests_when_named_file_exists():
    d = tempfile.mkdtemp(prefix="issue22-")
    try:
        Path(d, "organization-strategy.md").write_text("strategy\n")
        res = verify.verify_step([DANIEL_THEN], _ctx(d, DANIEL_TASK))
        check("with the file present the clause attests as before",
              res.ok and res.checks[0].verifier == "attest",
              str(res.checks))
    finally:
        shutil.rmtree(d)


def t_floor_ignores_quoted_non_file_text():
    d = tempfile.mkdtemp(prefix="issue22-")
    try:
        res = verify.verify_step(['the output is "done"',
                                  "the output lists the files"], _ctx(d))
        check("quoted non-file text and unquoted prose still attest",
              res.ok, str(res.checks))
    finally:
        shutil.rmtree(d)


def t_floor_skips_absence_intent_clauses():
    d = tempfile.mkdtemp(prefix="issue22-")
    try:
        res = verify.verify_step(
            ['"scratch.txt" should not be left behind'], _ctx(d))
        check("an absence-intent clause is not floored", res.ok,
              str(res.checks))
    finally:
        shutil.rmtree(d)


# ------------------------------------------------- Fix 2: .harness

def _registry(d):
    return make_tools(Jail(d), (), 30, 4000)


def t_harness_store_refuses_writes():
    d = tempfile.mkdtemp(prefix="issue22-")
    try:
        reg = _registry(d)
        for name, args in (
            ("write_file", {"path": ".harness/runs/x/step-02.md",
                            "content": "strategy"}),
            ("append_file", {"path": ".harness/runs/x/step-02.md",
                             "content": "strategy"}),
        ):
            try:
                reg[name]["func"](args)
                refused = False
            except Refusal:
                refused = True
            check(f"{name} refuses a .harness path", refused)
        check("nothing was written under .harness",
              not Path(d, ".harness").exists())
    finally:
        shutil.rmtree(d)


def t_harness_store_refuses_edits_but_allows_reads():
    d = tempfile.mkdtemp(prefix="issue22-")
    try:
        rec = Path(d, ".harness", "runs", "x", "step-01.md")
        rec.parent.mkdir(parents=True)
        rec.write_text("record of step 1")
        reg = _registry(d)
        try:
            reg["edit_file"]["func"]({"path": ".harness/runs/x/step-01.md",
                                      "old_text": "record",
                                      "new_text": "forged"})
            refused = False
        except Refusal:
            refused = True
        check("edit_file refuses a .harness path", refused)
        check("the record is untouched",
              rec.read_text() == "record of step 1")
        out = reg["read_file"]["func"](
            {"path": ".harness/runs/x/step-01.md"})
        check("read_file still reads the record store",
              "record of step 1" in out, out)
    finally:
        shutil.rmtree(d)


def t_normal_writes_unaffected():
    d = tempfile.mkdtemp(prefix="issue22-")
    try:
        reg = _registry(d)
        out = reg["write_file"]["func"](
            {"path": "organization-strategy.md", "content": "strategy"})
        check("a normal write still works", out.startswith("wrote"), out)
        check("the file exists",
              Path(d, "organization-strategy.md").is_file())
    finally:
        shutil.rmtree(d)


# --------------------------------------------------- Fix 3: the audit

def _steps(pairs):
    return [PlanStep(id=i + 1, instruction=w, done_when=t)
            for i, (w, t) in enumerate(pairs)]


def t_promised_deliverables_extraction():
    steps = _steps([
        ("I list the files", "the output lists the files"),
        ("I write the strategy", DANIEL_THEN),
        ("I build the index", '"audit-index.md" covers the files from step 1'),
        ("I check the total", '"total.txt" exists'),
        ("I clean up", '"scratch.txt" does not exist'),
    ])
    promised = verify.promised_deliverables(steps)
    check("prose contains, covers, and exists subjects are promised",
          promised == ["audit-index.md", "organization-strategy.md",
                       "total.txt"], str(promised))


def t_missing_deliverables_against_the_world():
    d = tempfile.mkdtemp(prefix="issue22-")
    try:
        steps = _steps([
            ("I list the files", "the output lists the files"),
            ("I write the strategy", DANIEL_THEN),
        ])
        missing = verify.missing_deliverables(steps, Jail(d))
        check("a missing promised file is reported",
              missing == ["organization-strategy.md"], str(missing))
        Path(d, "organization-strategy.md").write_text("strategy\n")
        check("a present promised file is not reported",
              verify.missing_deliverables(steps, Jail(d)) == [])
    finally:
        shutil.rmtree(d)


# --------------------------------------- end to end: Daniel's shape

STRATEGY = ("Organization strategy: group the invoices by year, keep "
            "policies together, and file receipts by month.")


class DanielActor:
    """Step 1 lists. Step 2 writes the strategy into a .harness path
    (the field failure) and claims DONE. In recovery mode, once the
    conversation carries the missing-file feedback, it writes the
    deliverable at its proper path instead."""

    def __init__(self, recovery: bool):
        self.recovery = recovery
        self._prompt = None
        self._phase = 0

    def __call__(self, messages, tool_defs):
        if tool_defs is None:
            return {"role": "assistant", "content": "DONE: summarized."}
        text = "\n".join(m.get("content") or "" for m in messages)
        prompt = next((m.get("content") or "" for m in messages
                       if "You are executing step" in (m.get("content")
                                                       or "")), "")
        if prompt != self._prompt:
            self._prompt = prompt
            self._phase = 0
        phase = self._phase
        self._phase += 1
        if "executing step 1" in prompt:
            if phase == 0:
                return {"role": "assistant", "content": "Listing.",
                        "tool_calls": [{"function": {
                            "name": "list_dir", "arguments": {"path": "."}}}]}
            return {"role": "assistant", "content": "DONE: listed."}
        feedback_seen = ("does not exist" in text
                         and "organization-strategy.md" in text)
        if self.recovery and feedback_seen:
            if phase == 0:
                return {"role": "assistant", "content": "Writing it now.",
                        "tool_calls": [{"function": {
                            "name": "write_file",
                            "arguments": {
                                "path": "organization-strategy.md",
                                "content": STRATEGY}}}]}
            return {"role": "assistant", "content": "DONE: written."}
        if phase == 0:
            return {"role": "assistant", "content": "Writing.",
                    "tool_calls": [{"function": {
                        "name": "write_file",
                        "arguments": {
                            "path": ".harness/runs/field/step-02.md",
                            "content": STRATEGY}}}]}
        return {"role": "assistant", "content": "DONE: strategy written."}


def _run_daniel(recovery: bool):
    d = tempfile.mkdtemp(prefix="issue22-e2e-")
    for name in ("invoice-1042.pdf", "policy-2026.pdf",
                 "meeting-notes.txt"):
        Path(d, name).write_text(f"contents of {name}")
    plan, err = gherkin.parse_feature(DANIEL_PLAN)
    assert plan is not None, err
    steps = to_plan_steps(plan)
    cfg = Config(workspace=d, max_steps=12, step_max_steps=8,
                 max_consecutive_errors=3)
    result = planner.execute_plan(DANIEL_TASK, steps, cfg,
                                  chat_fn=DanielActor(recovery))
    return result, d


def t_daniel_shape_cannot_report_done():
    result, d = _run_daniel(recovery=False)
    try:
        check("the run does not report done", result.status != "done",
              result.status)
        check("the failure names the missing deliverable",
              "organization-strategy.md" in result.answer, result.answer)
        check("the deliverable really is absent",
              not Path(d, "organization-strategy.md").exists())
    finally:
        shutil.rmtree(d)


def t_daniel_shape_recovers_with_feedback():
    result, d = _run_daniel(recovery=True)
    try:
        check("with the feedback acted on, the run completes",
              result.status == "done", result.status)
        check("the deliverable exists with the strategy",
              Path(d, "organization-strategy.md").read_text() == STRATEGY)
    finally:
        shutil.rmtree(d)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("t_") and callable(v)]
    for fn in fns:
        fn()
    print("\nAll issue #22 (false success) tests passed.")
