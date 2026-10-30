"""The accounts_for verb: the listing-only completeness contract.

Daniel Okafor's shape: an earlier step lists the files, the task
forbids reading their contents, and the deliverable is written from
the names alone (an organization strategy). indexes cannot serve
(it reads every file), preserves cannot serve (no records were
read), and covers served it weakly: its at-least-half floor let a
document skip files silently (Tomas's 2026-10-06 inventory passed
covers at 4 of 5). accounts_for is strict by design:

  "organization-strategy.md" accounts for the files from step 1

Every filename the listing step produced must appear in the
deliverable. Names in, names accounted for; contents are never
read and substance stays on trust, as the attested_compile warning
already says at approval.

On the unfixed code the clause parses to None (attestation), so
the behavioral tests below fail red.

Run: python3 tests/test_accounts_for.py
"""
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness import gherkin, graphcheck, planner, planlint, verify  # noqa: E402
from harness.config import Config  # noqa: E402
from harness.tools import Jail  # noqa: E402

TASK = ("Suggest an organization strategy for these documents based "
        "only on the filenames. Do not read the contents of the files.")

FILES = ["2025-tax-return.pdf", "bank-statement-jan.csv",
         "board-minutes.docx", "client-contract.pdf",
         "invoice-1042.pdf"]

FULL = """Organization strategy

- 2025-tax-return.pdf: file under Taxes, by year.
- bank-statement-jan.csv: file under Banking, by month.
- board-minutes.docx: file under Governance.
- client-contract.pdf: file under Clients, by client name.
- invoice-1042.pdf: file under Invoices, by number.
"""

# Four of five: covers passed this at its floor. accounts_for must not.
PARTIAL = """Organization strategy

- 2025-tax-return.pdf: file under Taxes, by year.
- bank-statement-jan.csv: file under Banking, by month.
- board-minutes.docx: file under Governance.
- invoice-1042.pdf: file under Invoices, by number.
"""

CLAUSE = '"organization-strategy.md" accounts for the files from step 1'


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name
          + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        raise AssertionError(name)


def _one(tmp, deliverable, clause=CLAUSE, items=FILES):
    root = Path(tmp)
    if deliverable is not None:
        (root / "organization-strategy.md").write_text(deliverable)
    ctx = verify.VerifyContext(
        jail=Jail(root), step_id=2, task=TASK,
        prior_items={1: list(items)} if items is not None else {})
    res = verify.verify_step([clause], ctx)
    assert len(res.checks) == 1
    return res.checks[0]


def t_parse_forms():
    for clause in (CLAUSE,
                   '"x.md" account for the files from step 1',
                   '"x.md" accounts for the files listed by step 2',
                   'the contents of "x.md" accounts for the files '
                   'from step 1'):
        p = verify.parse_then(clause)
        check(f"parses as accounts_for: {clause[:44]}",
              p is not None and p[0] == "accounts_for", str(p))


def t_all_names_present_passes():
    with tempfile.TemporaryDirectory() as tmp:
        c = _one(tmp, FULL)
        check("complete strategy passes",
              c.verifier == "accounts_for" and c.ok, c.detail)


def t_one_missing_name_fails_strictly():
    with tempfile.TemporaryDirectory() as tmp:
        c = _one(tmp, PARTIAL)
        check("four of five fails (no at-least-half floor)",
              c.verifier == "accounts_for" and not c.ok, c.detail)
        check("the failure names the skipped file",
              "client-contract.pdf" in c.detail, c.detail)


def t_no_listing_recorded_not_checked():
    with tempfile.TemporaryDirectory() as tmp:
        c = _one(tmp, FULL, items=[])
        check("no recorded listing: passes as not checked",
              c.verifier == "accounts_for" and c.ok
              and "not checked" in c.detail, c.detail)


def t_missing_deliverable_fails():
    with tempfile.TemporaryDirectory() as tmp:
        c = _one(tmp, None)
        check("missing deliverable fails",
              c.verifier == "accounts_for" and not c.ok, c.detail)


def t_deferral_stub_fails_even_with_all_names():
    stub = ("Organization strategy\n\n"
            + "\n".join(FILES)
            + "\n\n(Content to be filled in once the files are reviewed.)\n")
    with tempfile.TemporaryDirectory() as tmp:
        c = _one(tmp, stub)
        check("a deferral stub fails despite naming every file",
              not c.ok and "deferral" in c.detail, c.detail)


def t_target_is_promised():
    steps = [planner.PlanStep(id=2, instruction="Write the strategy",
                              done_when=CLAUSE)]
    check("accounts_for target is a promised deliverable",
          "organization-strategy.md"
          in verify.promised_deliverables(steps))


def _lint_steps(then):
    text = f"""Feature: Strategy
  Scenario: Step 1 - List the documents
    When I list the files in the folder
    Then the output lists the document files
  Scenario: Step 2 - Write the strategy
    Intent: suggest an organization strategy from the filenames
    When I write "organization-strategy.md" from the names in step 1
    Then {then}
"""
    plan, err = gherkin.parse_feature(text)
    assert plan is not None, err
    return planner.to_plan_steps(plan)


def t_lint_attested_compile_stays_silent():
    findings = planlint.lint_plan(TASK, _lint_steps(CLAUSE))
    codes = [f.code for f in findings]
    check("no attested_compile warning on an accounts_for step",
          "attested_compile" not in codes, str(codes))
    check("no lint errors on an accounts_for plan",
          not [f for f in findings if f.level == "error"],
          str(findings))


def t_graphcheck_listing_edge_and_bad_source():
    findings = graphcheck.check_plan(TASK, _lint_steps(CLAUSE))
    errors = [f for f in findings if f.level == "error"]
    check("graphcheck is clean for accounts_for on a listing step",
          not errors, str(errors))
    bad_text = """Feature: Strategy
  Scenario: Step 1 - List the documents
    When I list the files in the folder
    Then the output lists the document files
  Scenario: Step 2 - Note the folder name
    When I read "notes.txt" and note its title
    Then the output shows the title
  Scenario: Step 3 - Write the strategy
    When I write "organization-strategy.md" from the names in step 1
    Then "organization-strategy.md" accounts for the files from step 2
"""
    plan, err = gherkin.parse_feature(bad_text)
    assert plan is not None, err
    findings = graphcheck.check_plan(TASK, planner.to_plan_steps(plan))
    codes = [f.code for f in findings if f.level == "error"]
    check("graphcheck flags accounts_for drawn on a non-listing step",
          "covers_source_not_a_listing" in codes, str(codes))


# ---------- end to end: a skipped file is pushed back and fixed ----------

def _tool(name, args):
    return {"role": "assistant", "content": "",
            "tool_calls": [{"function": {"name": name,
                                          "arguments": args}}]}


def _done(text="DONE: ok."):
    return {"role": "assistant", "content": text}


def t_e2e_skipped_file_pushed_back():
    plan_text = f"""Feature: Strategy
  Scenario: Step 1 - List the documents
    When I list the files in the folder
    Then the output lists the document files
  Scenario: Step 2 - Write the strategy
    When I write "organization-strategy.md" from the names in step 1
    Then {CLAUSE}
"""
    plan, err = gherkin.parse_feature(plan_text)
    assert plan is not None, err
    steps = planner.to_plan_steps(plan)
    d = tempfile.mkdtemp(prefix="accounts-e2e-")
    for name in FILES:
        Path(d, name).write_text(f"contents of {name}")
    state = {"task_seen": None, "phase": 0, "attempts": {}}

    def step1(phase, task_msg, attempt):
        script = [_tool("list_dir", {"path": "."}),
                  _done("DONE: listed the documents.")]
        return script[min(phase, len(script) - 1)]

    def step2(phase, task_msg, attempt):
        content = PARTIAL if attempt == 1 else FULL
        script = [
            _tool("write_file", {"path": "organization-strategy.md",
                                 "content": content}),
            _done("DONE: wrote the strategy."),
        ]
        return script[min(phase, len(script) - 1)]

    scripts = {1: step1, 2: step2}

    def fake(messages, tool_defs):
        if tool_defs is None:
            return _done("DONE: summary.")
        task_msg = messages[1]["content"] if len(messages) > 1 else ""
        if task_msg != state["task_seen"]:
            state["task_seen"] = task_msg
            state["phase"] = 0
            m = re.search(r"You are executing step (\d+)", task_msg)
            sid = int(m.group(1)) if m else 0
            state["attempts"][sid] = state["attempts"].get(sid, 0) + 1
        phase = state["phase"]
        state["phase"] += 1
        m = re.search(r"You are executing step (\d+)", task_msg)
        sid = int(m.group(1)) if m else 0
        fn = scripts.get(sid)
        if fn is None:
            return _done("DONE: nothing scripted.")
        return fn(phase, task_msg, state["attempts"].get(sid, 1))

    events = []
    cfg = Config(workspace=d, max_steps=12, step_max_steps=8,
                 max_consecutive_errors=3)
    result = planner.execute_plan(TASK, steps, cfg, chat_fn=fake,
                                  on_event=lambda k, *a:
                                  events.append((k, a)))
    check("run completes", result.status == "done", result.status)
    verifies = [a[1] for k, a in events
                if k == "step_verify" and a[0].id == 2]
    check("step 2 verified more than once", len(verifies) >= 2,
          str(len(verifies)))
    first = [c for c in verifies[0].checks
             if c.verifier == "accounts_for"]
    check("first verification failed on accounts_for",
          bool(first) and not first[0].ok and verifies[0].ok is False,
          str(verifies[0]))
    check("the pushback named the skipped file",
          bool(first) and "client-contract.pdf" in first[0].detail,
          first[0].detail if first else "no accounts_for check")
    final = [c for c in steps[1].verify
             if c["verifier"] == "accounts_for"]
    check("final recorded check passed", bool(final) and final[-1]["ok"],
          str(steps[1].verify))
    got = Path(d, "organization-strategy.md").read_text()
    check("the strategy on disk accounts for every file",
          all(name in got for name in FILES))


def main():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("t_")]
    for fn in fns:
        fn()
    print(f"{len(fns)} tests passed")


if __name__ == "__main__":
    main()
