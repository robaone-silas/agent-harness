"""Issue #37, the flagship verb: `preserves`.

Marcus's 2026-10-06 failure: a read-then-compile plan whose compile
step attested ok while work_list.txt dropped every customer and every
status the read steps had gathered. No existing clause form can state
the real contract, because the planner plans before reading and cannot
quote the values that must survive. `preserves` names FIELDS instead:
  "work_list.txt" preserves customer, item, and status from steps 1 and 2
The harness harvests Label: value pairs from the named steps' stored
records (which embed what those steps actually read) and checks that
every harvested value for a named field appears in the deliverable.

On the unfixed code the clause parses to None (attestation) and the
field-dropping deliverable passes silently, so the behavioral tests
below fail red.

Run: python3 tests/test_preserves.py
"""
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness import gherkin, graphcheck, planner, verify  # noqa: E402
from harness.config import Config  # noqa: E402
from harness.tools import Jail  # noqa: E402

TASK = ("Please read the three music and repair notes in my workspace "
        "and write one combined work list I can keep.")

STEP1_RECORD = """# Step 1 record
### Tool calls
#### read_file {"path": "repair-fender-amp.txt"}
Result:
Customer: Elena Vasquez. Item: Fender guitar amplifier. Problem: crackling volume pot. Status: ready for pickup.
#### read_file {"path": "repair-mackie-mixer.txt"}
Result:
Customer: Harbor Lights Band. Item: Mackie mixer. Problem: channel 4 fader is noisy. Status: repair in progress.
#### read_file {"path": "repair-yamaha-keyboard.txt"}
Result:
Customer: Pastor John Neal. Item: Yamaha keyboard. Problem: three dead keys. Status: waiting on a replacement keybed part.
"""

STEP2_RECORD = """# Step 2 record
### Tool calls
#### read_file {"path": "gig-gear-check.txt"}
Result:
Spare tubes for the tube amp. A frayed instrument cable. Wireless mic batteries need charging.
"""

GOOD_LIST = """Marcus work list

From repair-fender-amp.txt: Customer: Elena Vasquez. Item: Fender guitar amplifier. Status: ready for pickup.
From repair-mackie-mixer.txt: Customer: Harbor Lights Band. Item: Mackie mixer. Status: repair in progress.
From repair-yamaha-keyboard.txt: Customer: Pastor John Neal. Item: Yamaha keyboard. Status: waiting on a replacement keybed part.
"""

# The exact shape that fooled attestation in the field: item + problem
# survive, every customer and every status is dropped.
BAD_LIST = """Marcus work list

1. From repair-fender-amp.txt: Item: Fender guitar amplifier. Problem: crackling volume pot.
2. From repair-mackie-mixer.txt: Item: Mackie mixer. Problem: channel 4 fader is noisy.
3. From repair-yamaha-keyboard.txt: Item: Yamaha keyboard. Problem: three dead keys.
"""

CLAUSE = '"work_list.txt" preserves customer, item, and status from steps 1 and 2'

SOURCES = {
    "repair-fender-amp.txt":
        "Customer: Elena Vasquez. Item: Fender guitar amplifier. "
        "Problem: crackling volume pot. Status: ready for pickup.",
    "repair-mackie-mixer.txt":
        "Customer: Harbor Lights Band. Item: Mackie mixer. "
        "Problem: channel 4 fader is noisy. Status: repair in progress.",
    "repair-yamaha-keyboard.txt":
        "Customer: Pastor John Neal. Item: Yamaha keyboard. "
        "Problem: three dead keys. "
        "Status: waiting on a replacement keybed part.",
    "gig-gear-check.txt":
        "Spare tubes for the tube amp. A frayed instrument cable. "
        "Wireless mic batteries need charging.",
}


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name
          + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        raise AssertionError(name)


# ---------- verifier-level tests (hand-built run store) ----------

def _ws(tmp, deliverable=None, step2=STEP2_RECORD):
    root = Path(tmp)
    runs = root / ".harness" / "runs" / "run1"
    runs.mkdir(parents=True)
    (root / ".harness" / "latest.txt").write_text("run1\n")
    (runs / "step-01.md").write_text(STEP1_RECORD)
    (runs / "step-02.md").write_text(step2)
    if deliverable is not None:
        (root / "work_list.txt").write_text(deliverable)
    return Jail(root)


def _one(tmp, deliverable, clause=CLAUSE, step2=STEP2_RECORD):
    jail = _ws(tmp, deliverable, step2)
    res = verify.verify_step(
        [clause], verify.VerifyContext(jail=jail, step_id=3, task=TASK))
    assert len(res.checks) == 1
    return res.checks[0]


def t_parse_forms():
    p = verify.parse_then(CLAUSE)
    check("clause parses as preserves on the target",
          p is not None and p[0] == "preserves" and p[1] == "work_list.txt",
          str(p))
    check("singular step reference parses",
          (verify.parse_then('"x.txt" preserves status from step 1')
           or (None,))[0] == "preserves")
    check("range reference parses",
          (verify.parse_then('"x.txt" preserves customer and status '
                             'from steps 1 through 2')
           or (None,))[0] == "preserves")


def t_complete_deliverable_passes():
    with tempfile.TemporaryDirectory() as tmp:
        c = _one(tmp, GOOD_LIST)
        check("complete list passes as preserves",
              c.verifier == "preserves" and c.ok, c.detail)


def t_field_loss_fails_and_names_missing_values():
    with tempfile.TemporaryDirectory() as tmp:
        c = _one(tmp, BAD_LIST)
        check("field-dropping list fails", c.verifier == "preserves"
              and not c.ok, c.detail)
        for needle in ("Elena Vasquez", "Harbor Lights Band",
                       "Pastor John Neal", "ready for pickup",
                       "repair in progress",
                       "waiting on a replacement keybed part"):
            check(f"failure names {needle!r}", needle in c.detail,
                  c.detail)


def t_plural_and_range_refs_harvest_every_named_step():
    step2 = ("# Step 2 record\n#### read_file {\"path\": \"extra.txt\"}\n"
             "Result:\nCustomer: Dana Lee. Item: Shure microphone. "
             "Status: done.\n")
    for clause in ('"work_list.txt" preserves customer and status '
                   'from steps 1 and 2',
                   '"work_list.txt" preserves customer and status '
                   'from steps 1 through 2'):
        with tempfile.TemporaryDirectory() as tmp:
            c = _one(tmp, GOOD_LIST, clause, step2)
            check(f"refs harvest both steps ({clause[-18:]})",
                  not c.ok and "Dana Lee" in c.detail, c.detail)


def t_missing_deliverable_fails():
    with tempfile.TemporaryDirectory() as tmp:
        c = _one(tmp, None)
        check("missing deliverable fails",
              c.verifier == "preserves" and not c.ok, c.detail)


def t_unfound_field_is_noted_not_silent():
    clause = ('"work_list.txt" preserves customer, status, and warranty '
              'from step 1')
    with tempfile.TemporaryDirectory() as tmp:
        c = _one(tmp, GOOD_LIST, clause)
        check("found fields pass, unfound field noted",
              c.ok and "warranty" in c.detail
              and "not checked" in c.detail, c.detail)


def t_nothing_harvestable_passes_as_not_checked():
    with tempfile.TemporaryDirectory() as tmp:
        c = _one(tmp, GOOD_LIST,
                 '"work_list.txt" preserves warranty from step 2')
        check("no labeled values: passes as not checked",
              c.verifier == "preserves" and c.ok
              and "not checked" in c.detail, c.detail)


def t_deliverable_is_promised():
    steps = [planner.PlanStep(id=3, instruction="Write work_list.txt",
                              done_when=CLAUSE)]
    check("preserves target is a promised deliverable",
          "work_list.txt" in verify.promised_deliverables(steps))


def t_graphcheck_clean():
    text = f"""Feature: Work list
  Scenario: Compile
    Given the repair notes are in the workspace
    When I write "work_list.txt" from the repair notes
    Then {CLAUSE}
"""
    plan, err = gherkin.parse_feature(text)
    assert plan is not None, err
    findings = graphcheck.check_plan(TASK, planner.to_plan_steps(plan))
    errors = [f for f in findings if f.level == "error"]
    check("graphcheck reports no error for a preserves plan",
          not errors, str(errors))


# ---------- end to end: the field's exact failure shape ----------

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


def t_e2e_bad_write_pushed_back_retry_fixes():
    plan_text = f"""Feature: Work list
  Scenario: Step 1 - Collect the electronics repair notes
    When I read "repair-fender-amp.txt", "repair-mackie-mixer.txt", and "repair-yamaha-keyboard.txt"
    Then the output shows the customer, item, and status of each repair job
  Scenario: Step 2 - Collect the gig and gear notes
    When I read "gig-gear-check.txt"
    Then the output shows the gear state
  Scenario: Step 3 - Compile the work list
    When I write "work_list.txt" combining the repair jobs from step 1 and the gear state from step 2
    Then {CLAUSE}
"""
    plan, err = gherkin.parse_feature(plan_text)
    assert plan is not None, err
    steps = planner.to_plan_steps(plan)

    d = tempfile.mkdtemp(prefix="preserves-e2e-")
    for name, text in SOURCES.items():
        Path(d, name).write_text(text)

    state = {"task_seen": None, "phase": 0, "attempts": {}}

    def step1(phase, task_msg, attempt):
        script = [
            _tool("read_file", {"path": "repair-fender-amp.txt"}),
            _tool("read_file", {"path": "repair-mackie-mixer.txt"}),
            _tool("read_file", {"path": "repair-yamaha-keyboard.txt"}),
            _done("DONE: read all three repair notes."),
        ]
        return script[min(phase, len(script) - 1)]

    def step2(phase, task_msg, attempt):
        script = [
            _tool("read_file", {"path": "gig-gear-check.txt"}),
            _done("DONE: read the gear notes."),
        ]
        return script[min(phase, len(script) - 1)]

    def step3(phase, task_msg, attempt):
        if attempt == 1:
            script = [
                _tool("read_file", {"path": _record_of(task_msg, 1)}),
                _tool("read_file", {"path": _record_of(task_msg, 2)}),
                _tool("write_file", {"path": "work_list.txt",
                                     "content": BAD_LIST}),
                _done("DONE: wrote the work list."),
            ]
        else:
            script = [
                _tool("read_file", {"path": _record_of(task_msg, 1)}),
                _tool("write_file", {"path": "work_list.txt",
                                     "content": GOOD_LIST}),
                _done("DONE: rewrote the work list completely."),
            ]
        return script[min(phase, len(script) - 1)]

    scripts = {1: step1, 2: step2, 3: step3}

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
                if k == "step_verify" and a[0].id == 3]
    check("step 3 verified more than once", len(verifies) >= 2,
          str(len(verifies)))
    first = [c for c in verifies[0].checks if c.verifier == "preserves"]
    check("first verification failed on preserves",
          bool(first) and not first[0].ok
          and verifies[0].ok is False, str(verifies[0]))
    check("the pushback named a lost customer",
          bool(first) and "Elena Vasquez" in first[0].detail,
          first[0].detail if first else "no preserves check")
    step3_obj = steps[2]
    final = [c for c in step3_obj.verify if c["verifier"] == "preserves"]
    check("final recorded check passed", bool(final) and final[-1]["ok"],
          str(step3_obj.verify))
    got = Path(d, "work_list.txt").read_text()
    check("the deliverable on disk is the complete list",
          "Pastor John Neal" in got and "ready for pickup" in got)


def main():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("t_")]
    for fn in fns:
        fn()
    print(f"{len(fns)} tests passed")


if __name__ == "__main__":
    main()
