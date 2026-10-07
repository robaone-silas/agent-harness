"""The excludes verb: the negative contract, counterpart to preserves.

Some deliverables must NOT carry what the earlier steps read:
prices or internal notes leaking into a customer facing summary.
No positive clause can state that. `excludes` names what must be
absent, in three forms:

  "summary.txt" excludes prices from step 1
      (field form: values harvested from the source steps' records,
      the same machinery as preserves, checked in reverse)
  "summary.txt" excludes the contents of "internal-notes.txt"
      (contents form: labeled values and distinctive lines of the
      named file must not appear)
  "summary.txt" excludes "SOLD"
      (literal form: the quoted text must not appear)

On the unfixed code every form parses to None (attestation) and a
leaking deliverable passes on trust, so the tests below fail red.

Run: python3 tests/test_excludes.py
"""
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness import gherkin, planner, verify  # noqa: E402
from harness.config import Config  # noqa: E402
from harness.tools import Jail  # noqa: E402

TASK = ("Write a customer summary of the repair job, with the "
        "customer, item, and status, but no prices.")

STEP1_RECORD = """# Step 1 record
### Tool calls
#### read_file {"path": "repair-fender-amp.txt"}
Result:
Customer: Elena Vasquez. Item: Fender guitar amplifier. Problem: crackling volume pot. Price: 180 dollars. Status: ready for pickup.
#### read_file {"path": "repair-mackie-mixer.txt"}
Result:
Customer: Harbor Lights Band. Item: Mackie mixer. Problem: channel 4 fader is noisy. Price: 240 dollars. Status: repair in progress.
"""

NOTES = """Supplier: Island Parts Co. Cost: 90 dollars.
Wholesale account number 4417 renews in March.
Ask about the church discount before quoting.
"""

CLEAN_SUMMARY = """Repair summary for Elena Vasquez

Item: Fender guitar amplifier. Status: ready for pickup.
"""

LEAKY_SUMMARY = """Repair summary for Elena Vasquez

Item: Fender guitar amplifier. Status: ready for pickup.
Price: 180 dollars.
"""


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name
          + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        raise AssertionError(name)


def _ws(tmp, deliverable=None, notes=False):
    root = Path(tmp)
    runs = root / ".harness" / "runs" / "run1"
    runs.mkdir(parents=True)
    (root / ".harness" / "latest.txt").write_text("run1\n")
    (runs / "step-01.md").write_text(STEP1_RECORD)
    if deliverable is not None:
        (root / "summary.txt").write_text(deliverable)
    if notes:
        (root / "internal-notes.txt").write_text(NOTES)
    return Jail(root)


def _one(tmp, deliverable, clause, notes=False):
    jail = _ws(tmp, deliverable, notes)
    res = verify.verify_step(
        [clause], verify.VerifyContext(jail=jail, step_id=2, task=TASK))
    assert len(res.checks) == 1
    return res.checks[0]


def t_parse_forms():
    for clause in ('"summary.txt" excludes prices from step 1',
                   '"summary.txt" excludes the contents of '
                   '"internal-notes.txt"',
                   '"summary.txt" excludes "SOLD"'):
        p = verify.parse_then(clause)
        check(f"parses as excludes: {clause[:44]}",
              p is not None and p[0] == "excludes"
              and p[1] == "summary.txt", str(p))


def t_field_form_clean_passes():
    with tempfile.TemporaryDirectory() as tmp:
        c = _one(tmp, CLEAN_SUMMARY,
                 '"summary.txt" excludes prices from step 1')
        check("clean summary passes the field form",
              c.verifier == "excludes" and c.ok, c.detail)


def t_field_form_leak_fails_and_names_the_value():
    with tempfile.TemporaryDirectory() as tmp:
        c = _one(tmp, LEAKY_SUMMARY,
                 '"summary.txt" excludes prices from step 1')
        check("a leaked price fails", c.verifier == "excludes"
              and not c.ok, c.detail)
        check("the failure names the leaked value",
              "180 dollars" in c.detail, c.detail)


def t_field_form_plural_matches_singular_label():
    # The clause says "prices"; the records label the field "Price".
    with tempfile.TemporaryDirectory() as tmp:
        c = _one(tmp, LEAKY_SUMMARY,
                 '"summary.txt" excludes prices from steps 1 through 1')
        check("plural field wording still guards the values",
              not c.ok and "180 dollars" in c.detail, c.detail)


def t_field_form_nothing_harvested_not_checked():
    with tempfile.TemporaryDirectory() as tmp:
        c = _one(tmp, CLEAN_SUMMARY,
                 '"summary.txt" excludes warranties from step 1')
        check("no such field in the records: passes as not checked",
              c.verifier == "excludes" and c.ok
              and "not checked" in c.detail, c.detail)


def t_contents_form():
    clause = '"summary.txt" excludes the contents of "internal-notes.txt"'
    with tempfile.TemporaryDirectory() as tmp:
        c = _one(tmp, CLEAN_SUMMARY, clause, notes=True)
        check("clean summary passes the contents form",
              c.verifier == "excludes" and c.ok, c.detail)
    with tempfile.TemporaryDirectory() as tmp:
        leaky = CLEAN_SUMMARY + "\nParts from Island Parts Co.\n"
        c = _one(tmp, leaky, clause, notes=True)
        check("a leaked labeled value fails the contents form",
              not c.ok and "Island Parts Co" in c.detail, c.detail)
    with tempfile.TemporaryDirectory() as tmp:
        leaky = (CLEAN_SUMMARY
                 + "\nWholesale account number 4417 renews in March.\n")
        c = _one(tmp, leaky, clause, notes=True)
        check("a leaked distinctive line fails the contents form",
              not c.ok and "4417" in c.detail, c.detail)


def t_literal_form():
    with tempfile.TemporaryDirectory() as tmp:
        c = _one(tmp, CLEAN_SUMMARY, '"summary.txt" excludes "SOLD"')
        check("absent literal passes", c.verifier == "excludes"
              and c.ok, c.detail)
    with tempfile.TemporaryDirectory() as tmp:
        c = _one(tmp, CLEAN_SUMMARY + "\nSOLD\n",
                 '"summary.txt" excludes "SOLD"')
        check("present literal fails", not c.ok
              and "SOLD" in c.detail, c.detail)


def t_missing_deliverable_fails():
    with tempfile.TemporaryDirectory() as tmp:
        c = _one(tmp, None, '"summary.txt" excludes prices from step 1')
        check("missing deliverable fails",
              c.verifier == "excludes" and not c.ok, c.detail)


def t_target_is_promised():
    steps = [planner.PlanStep(
        id=2, instruction="Write summary.txt",
        done_when='"summary.txt" excludes prices from step 1')]
    check("excludes target is a promised deliverable",
          "summary.txt" in verify.promised_deliverables(steps))


# ---------- end to end: a leak is pushed back and scrubbed ----------

def _tool(name, args):
    return {"role": "assistant", "content": "",
            "tool_calls": [{"function": {"name": name,
                                          "arguments": args}}]}


def _done(text="DONE: ok."):
    return {"role": "assistant", "content": text}


def _record_of(task_msg, n):
    m = re.search(rf"\.harness/runs/\S+/step-0{n}\.md", task_msg)
    assert m, "no record path in prompt"
    return m.group(0)


def t_e2e_leak_pushed_back_and_scrubbed():
    plan_text = """Feature: Customer summary
  Scenario: Step 1 - Read the repair record
    When I read "repair-fender-amp.txt"
    Then the output shows the customer, item, price, and status
  Scenario: Step 2 - Write the customer summary
    When I write "summary.txt" for the customer from step 1
    Then "summary.txt" excludes prices from step 1
"""
    plan, err = gherkin.parse_feature(plan_text)
    assert plan is not None, err
    steps = planner.to_plan_steps(plan)
    d = tempfile.mkdtemp(prefix="excludes-e2e-")
    Path(d, "repair-fender-amp.txt").write_text(
        "Customer: Elena Vasquez. Item: Fender guitar amplifier. "
        "Problem: crackling volume pot. Price: 180 dollars. "
        "Status: ready for pickup.")
    state = {"task_seen": None, "phase": 0, "attempts": {}}

    def step1(phase, task_msg, attempt):
        script = [
            _tool("read_file", {"path": "repair-fender-amp.txt"}),
            _done("DONE: read the repair record."),
        ]
        return script[min(phase, len(script) - 1)]

    def step2(phase, task_msg, attempt):
        if attempt == 1:
            script = [
                _tool("read_file", {"path": _record_of(task_msg, 1)}),
                _tool("write_file", {"path": "summary.txt",
                                     "content": LEAKY_SUMMARY}),
                _done("DONE: wrote the summary."),
            ]
        else:
            script = [
                _tool("read_file", {"path": _record_of(task_msg, 1)}),
                _tool("write_file", {"path": "summary.txt",
                                     "content": CLEAN_SUMMARY}),
                _done("DONE: rewrote the summary without prices."),
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
    first = [c for c in verifies[0].checks if c.verifier == "excludes"]
    check("first verification failed on excludes",
          bool(first) and not first[0].ok and verifies[0].ok is False,
          str(verifies[0]))
    check("the pushback named the leaked price",
          bool(first) and "180 dollars" in first[0].detail,
          first[0].detail if first else "no excludes check")
    final = [c for c in steps[1].verify if c["verifier"] == "excludes"]
    check("final recorded check passed", bool(final) and final[-1]["ok"],
          str(steps[1].verify))
    got = Path(d, "summary.txt").read_text()
    check("the summary on disk carries no price",
          "180 dollars" not in got and "Elena Vasquez" in got)


def main():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("t_")]
    for fn in fns:
        fn()
    print(f"{len(fns)} tests passed")


if __name__ == "__main__":
    main()
