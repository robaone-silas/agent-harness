"""Tests for the issue #48 fix: a Then-only output file is visible to
lint and to the executor.

Field failure (daily persona run 2026-10-09, Marcus Bennett): step 3's
When read gig-gear-notes.txt and named no output file; its Then named
the deliverable only in a prose clause, `"urgent-gear.txt" contains a
list of music gear needing attention before Saturday`. The clause
parses to no verifier, so plan lint's dangling_file check (which only
looked at parsed clauses) never saw the file, while the same plan's
parsed preserves Then drew the ERROR for its own Then-only file. At
execution the step wrote its gear list three times to its own step
record path; the .harness guard refused each write with "write to the
file your step names instead", which resolved to nothing because the
step's When names no file. The step failed, the final work list was
never written, and the run reported failure with no deliverable.

The fix, one piece per layer that stayed silent:

1. verify.then_subject_file: the file a Then clause is *about* — the
   parsed verifier's target, or, for a prose clause, a quoted
   path-shaped name in subject position at the clause's start. A file
   merely mentioned later in a clause (a listing Then enumerating the
   files it found) is not a subject.
2. planlint: an unparsed Then's subject file gets the same treatment
   as a parsed clause's target — it counts as a named deliverable,
   and when no step creates it and the task doesn't mention it,
   dangling_file fires (ERROR), with feedback telling the planner to
   name the file in the step's When. Absence-intent clauses are
   exempt, as they are at the existence floor.
3. Executor prompt: a step whose Thens name a file its When does not
   is told, in its operative prompt block, that the file is its
   output file and to write its result there.
4. Blocked-write recovery: when a write is refused (.harness store),
   the refusal the model sees names the step's Then-named output
   file, so the step can recover inside its budget.

Run: python3 tests/test_then_only_output.py
"""
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness import loop, planlint, planner, verify
from harness.config import Config
from harness.tools import Jail


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name
          + (f" -- {info}" if info and not cond else ""))
    assert cond, f"{name}: {info}"


TASK = ("Make a work list for my business: for each repair job, the "
        "customer, the item, and its status. Also list the music gear "
        "that needs attention before Saturday. Write it in a file I "
        "can keep, and do not change the original records.")

STEP1 = planner.PlanStep(
    id=1, instruction="I list the files in the workspace",
    done_when=('the output lists "gig-gear-notes.txt", '
               '"parts-inventory.csv", "repair-fender-amp.txt", '
               '"repair-mackie-mixer.txt", and '
               '"repair-yamaha-keyboard.txt"'))
STEP2 = planner.PlanStep(
    id=2,
    instruction=('I read "repair-fender-amp.txt", '
                 '"repair-mackie-mixer.txt", and '
                 '"repair-yamaha-keyboard.txt" and compile a list '
                 'of repair jobs'),
    done_when=('"repair-jobs.txt" preserves customer, item, and '
               'status from steps 1 and 2'))
STEP3 = planner.PlanStep(
    id=3,
    instruction=('I read "gig-gear-notes.txt" and identify items '
                 'needing attention before Saturday'),
    done_when=('"urgent-gear.txt" contains a list of music gear '
               'needing attention before Saturday'))
MARCUS_STEPS = [STEP1, STEP2, STEP3]


def _dangling(findings):
    return [f for f in findings if f.code == "dangling_file"]


# ------------------------------------------------- subject extraction

def t_subject_file_shapes():
    check("prose contains subject is the clause's file",
          verify.then_subject_file(
              '"urgent-gear.txt" contains a list of music gear '
              'needing attention before Saturday') == "urgent-gear.txt")
    check("parsed clause subject is its verifier target",
          verify.then_subject_file(
              '"repair-jobs.txt" preserves customer, item, and '
              'status from steps 1 and 2') == "repair-jobs.txt")
    check("parsed exists subject",
          verify.then_subject_file('"summary.txt" exists')
          == "summary.txt")
    check("a listing Then enumerating files has no subject file",
          verify.then_subject_file(
              'the output lists "gig-gear-notes.txt", '
              '"parts-inventory.csv"') is None)
    check("a quoted prose value in subject position is not a file",
          verify.then_subject_file(
              '"Total: 42" is the computed result') is None)
    check("a clause naming no file has no subject",
          verify.then_subject_file(
              'the output shows the amount from each file') is None)


# ------------------------------------------------------------ the lint

def t_lint_flags_then_only_prose_file():
    findings = planlint.lint_plan(TASK, MARCUS_STEPS)
    dang = _dangling(findings)
    by_step = {f.step_id: f for f in dang}
    check("dangling_file fires for the parsed Then-only file (step 2)",
          2 in by_step, str([f.as_dict() for f in findings]))
    check("dangling_file fires for the prose Then-only file (step 3)",
          3 in by_step, str([f.as_dict() for f in findings]))
    check("no dangling_file for the listing step's enumerated files",
          1 not in by_step, str([f.as_dict() for f in dang]))
    check("the step 3 finding teaches naming the file in the When",
          "When" in by_step[3].detail
          and "urgent-gear.txt" in by_step[3].detail,
          by_step[3].detail if 3 in by_step else "(no finding)")


def t_lint_quiet_when_the_when_names_the_file():
    step = planner.PlanStep(
        id=3,
        instruction=('I read "gig-gear-notes.txt" and write '
                     '"urgent-gear.txt" with the items needing '
                     'attention before Saturday'),
        done_when=('"urgent-gear.txt" contains a list of music gear '
                   'needing attention before Saturday'))
    findings = planlint.lint_plan(TASK, [STEP1, step])
    check("no dangling_file when the When names the output file",
          not _dangling(findings),
          str([f.as_dict() for f in findings]))


def t_lint_quiet_for_absence_prose():
    step = planner.PlanStep(
        id=2, instruction="I remove the scratch file from the workspace",
        done_when='"scratch.txt" has been removed from the workspace')
    task = "Tidy the workspace: remove the scratch file."
    findings = planlint.lint_plan(task, [step])
    check("no dangling_file for an absence-intent prose Then",
          not _dangling(findings),
          str([f.as_dict() for f in findings]))


# ------------------------------------- review round, 2026-10-19

def t_preservation_clause_is_not_an_output():
    # A Then asserting a source file stays as it was names a
    # constraint, not a place to write: the executor must not be
    # told to write its result into the preserved file, and lint
    # must not advise naming it in the When as the step's output.
    step = planner.PlanStep(
        id=2,
        instruction=("I read the sources from step 1 and write "
                     "summary.txt"),
        done_when='"source.txt" remains unchanged')
    check("preservation clause names no output",
          planner._then_named_outputs(step) == [],
          str(planner._then_named_outputs(step)))
    check("preservation intent reads the clause",
          verify.preservation_intent('"source.txt" remains unchanged'))
    check("a preserves clause is a deliverable, not preservation "
          "intent",
          not verify.preservation_intent(
              '"repair-jobs.txt" preserves customer, item, and '
              'status from steps 1 and 2'))
    findings = planlint.lint_plan(TASK, [STEP1, step])
    check("no dangling_file for a preservation prose Then",
          not [f for f in _dangling(findings) if f.step_id == 2],
          str([f.as_dict() for f in findings]))


def t_absence_subject_is_not_a_deliverable_for_clarity():
    # An absence clause's subject counts neither as an output nor
    # as the plan's deliverable: a plan whose only file assertion
    # is a removal still owes the deliverable_clarity warning.
    step = planner.PlanStep(
        id=1, instruction="I remove the scratch file",
        done_when='"scratch.txt" has been removed from the workspace')
    findings = planlint.lint_plan(
        "Give me an inventory of the workspace files.", [step])
    codes = [f.code for f in findings]
    check("absence subject draws no dangling_file",
          "dangling_file" not in codes, str(codes))
    check("deliverable_clarity still fires without a real output",
          "deliverable_clarity" in codes, str(codes))


def t_move_destination_is_not_an_output():
    # The archive shape the planner prompt itself teaches: the
    # destination is computed from the mv command, not found by
    # substring, and it is the move's result, not a file the step
    # writes its result into.
    step = planner.PlanStep(
        id=3, instruction="I run: mv notes-2025-03.txt archive/",
        done_when='"archive/notes-2025-03.txt" exists')
    check("move destination names no output",
          planner._then_named_outputs(step) == [],
          str(planner._then_named_outputs(step)))


def t_similar_source_name_does_not_hide_the_output():
    # Substring matching hid the guidance here: the output's name
    # sits inside the longer source name the When reads.
    step = planner.PlanStep(
        id=2,
        instruction='I read "not-urgent-gear.txt" and summarize it',
        done_when='"urgent-gear.txt" exists')
    check("output hidden by a longer source name is still named",
          planner._then_named_outputs(step) == ["urgent-gear.txt"],
          str(planner._then_named_outputs(step)))


def t_absence_intent_respects_parsed_verbs():
    # The absence heuristic reads prose only: a parsed clause's
    # verb decides, so a contains value that happens to be an
    # absence word does not strip the clause's output guidance.
    check("a contains value is not absence intent",
          not verify.absence_intent('"result.txt" contains "removed"'))
    check("not_exists is absence intent",
          verify.absence_intent('"scratch.txt" does not exist'))
    check("removal prose is absence intent",
          verify.absence_intent(
              '"scratch.txt" has been removed from the workspace'))
    step = planner.PlanStep(
        id=2, instruction="I compile the cleanup results",
        done_when='"result.txt" contains "removed"')
    check("the contains target keeps its output guidance",
          planner._then_named_outputs(step) == ["result.txt"],
          str(planner._then_named_outputs(step)))


def t_preservation_subject_excluded_across_clauses():
    # A sibling existence clause must not resurrect a file another
    # clause of the same step protects, whichever order they come
    # in: the constraint is collected across the whole step before
    # any output candidate is chosen.
    for thens in (
            '"source.txt" exists\n"source.txt" remains unchanged',
            '"source.txt" remains unchanged\n"source.txt" exists'):
        step = planner.PlanStep(
            id=2,
            instruction=("I read the sources from step 1 and write "
                         "summary.txt"),
            done_when=thens)
        check("preserved source names no output in either clause order",
              planner._then_named_outputs(step) == [],
              str(planner._then_named_outputs(step)))
    # The same whole-step rule for absence: an existence clause
    # beside a removal clause names no output.
    step = planner.PlanStep(
        id=2, instruction="I tidy the workspace",
        done_when=('"scratch.txt" exists\n'
                   '"scratch.txt" has been removed from the workspace'))
    check("removed file names no output beside an existence clause",
          planner._then_named_outputs(step) == [],
          str(planner._then_named_outputs(step)))


def t_preservation_predicate_is_about_the_subject():
    # A preservation word inside the filename or inside the
    # described contents is not a claim about the file: both files
    # below are deliverables to create, not files to leave alone.
    check("unchanged inside the contents is not preservation",
          not verify.preservation_intent(
              '"report.txt" contains a list of unchanged records'))
    check("unchanged inside the filename is not preservation",
          not verify.preservation_intent(
              '"unchanged-jobs.txt" contains a list of repair jobs'))
    check("remains unchanged is preservation",
          verify.preservation_intent('"source.txt" remains unchanged'))
    check("is not modified is preservation",
          verify.preservation_intent('"source.txt" is not modified'))
    check("stays the same is preservation",
          verify.preservation_intent('"source.txt" stays the same'))
    check("asserting change is not preservation",
          not verify.preservation_intent('"source.txt" is modified'))
    step = planner.PlanStep(
        id=2, instruction="I compile the cleanup report",
        done_when='"report.txt" contains a list of unchanged records')
    check("the report keeps its output guidance",
          planner._then_named_outputs(step) == ["report.txt"],
          str(planner._then_named_outputs(step)))
    step = planner.PlanStep(
        id=2, instruction="I compile the jobs list",
        done_when='"unchanged-jobs.txt" contains a list of repair jobs')
    check("the jobs file keeps its output guidance",
          planner._then_named_outputs(step) == ["unchanged-jobs.txt"],
          str(planner._then_named_outputs(step)))
    findings = planlint.lint_plan(
        "List the repair jobs.", [step])
    check("the jobs file draws dangling_file as a deliverable",
          any(f.code == "dangling_file" and f.step_id == 2
              for f in findings),
          str([f.as_dict() for f in findings]))


def t_preservation_subject_with_apostrophe():
    # A filename carrying an apostrophe must still match its
    # preservation predicate: the closing delimiter is the one
    # that opened the quote, not the apostrophe inside the name.
    check("apostrophe filename still reads as preservation",
          verify.preservation_intent(
              "\"owner's-notes.txt\" remains unchanged"))
    step = planner.PlanStep(
        id=2,
        instruction=("I read the sources from step 1 and write "
                     "summary.txt"),
        done_when=('"owner\'s-notes.txt" exists\n'
                   '"owner\'s-notes.txt" remains unchanged'))
    check("apostrophe source names no output beside its exists clause",
          planner._then_named_outputs(step) == [],
          str(planner._then_named_outputs(step)))


def t_absence_predicate_is_about_the_subject():
    # The round-2 anchoring lesson, applied to absence: a removal
    # word inside the filename or inside the described contents is
    # not a claim the file is gone. Both files below are
    # deliverables to create, not files to remove.
    check("removed inside the contents is not absence",
          not verify.absence_intent(
              '"report.txt" contains a list of removed records'))
    check("deleted inside the filename is not absence",
          not verify.absence_intent(
              '"deleted-records.txt" contains a list of repair jobs'))
    check("has been removed is absence",
          verify.absence_intent(
              '"scratch.txt" has been removed from the workspace'))
    check("does not exist is absence",
          verify.absence_intent('"scratch.txt" does not exist'))
    check("is absent is absence",
          verify.absence_intent('"scratch.txt" is absent'))
    check("no longer exists is absence",
          verify.absence_intent('"scratch.txt" no longer exists'))
    check("was deleted is absence",
          verify.absence_intent('"scratch.txt" was deleted'))
    step = planner.PlanStep(
        id=2, instruction="I compile the removals report",
        done_when='"report.txt" contains a list of removed records')
    check("the removals report keeps its output guidance",
          planner._then_named_outputs(step) == ["report.txt"],
          str(planner._then_named_outputs(step)))
    step = planner.PlanStep(
        id=2, instruction="I compile the deletions list",
        done_when='"deleted-records.txt" contains a list of repair jobs')
    check("the deletions file keeps its output guidance",
          planner._then_named_outputs(step) == ["deleted-records.txt"],
          str(planner._then_named_outputs(step)))
    findings = planlint.lint_plan("List the repair jobs.", [step])
    check("the deletions file draws dangling_file as a deliverable",
          any(f.code == "dangling_file" and f.step_id == 2
              for f in findings),
          str([f.as_dict() for f in findings]))


# ------------------------------------------------------ executor prompt

def t_step_prompt_names_the_then_only_output_file():
    d = tempfile.mkdtemp(prefix="issue48-prompt-")
    try:
        prompts = []

        def chat_fn(messages, tool_defs):
            prompts.append(messages[-1]["content"])
            return {"role": "assistant", "content": "DONE: identified"}

        step = planner.PlanStep(
            id=3, instruction=STEP3.instruction,
            done_when=STEP3.done_when,
            output_path=".harness/runs/X/step-03.md")
        planner._run_step_verified(
            TASK, step, [STEP1, step], Config(workspace=d), chat_fn,
            Jail(d), lambda *a: None)
        check("the step prompt was captured", bool(prompts))
        check("the prompt presents the Then-named file as the output file",
              any("Output file for this step" in p
                  and "urgent-gear.txt" in p for p in prompts),
              prompts[0][:600] if prompts else "(none)")
    finally:
        shutil.rmtree(d)


def t_step_prompt_quiet_when_when_names_the_file():
    d = tempfile.mkdtemp(prefix="issue48-prompt2-")
    try:
        prompts = []

        def chat_fn(messages, tool_defs):
            prompts.append(messages[-1]["content"])
            return {"role": "assistant", "content": "DONE: written"}

        step = planner.PlanStep(
            id=3,
            instruction=('I read "gig-gear-notes.txt" and write '
                         '"urgent-gear.txt" with the items needing '
                         'attention'),
            done_when=('"urgent-gear.txt" contains a list of music '
                       'gear needing attention before Saturday'),
            output_path=".harness/runs/X/step-03.md")
        Path(d, "urgent-gear.txt").write_text("spare tubes\n")
        planner._run_step_verified(
            TASK, step, [STEP1, step], Config(workspace=d), chat_fn,
            Jail(d), lambda *a: None)
        check("no output-file directive when the When names the file",
              all("Output file for this step" not in p for p in prompts),
              prompts[0][:600] if prompts else "(none)")
    finally:
        shutil.rmtree(d)


# ------------------------------------------- blocked-write recovery hint

def _write_call(path, content):
    return {"role": "assistant", "content": "",
            "tool_calls": [{"function": {
                "name": "write_file",
                "arguments": {"path": path, "content": content}}}]}


def t_refusal_names_the_steps_output_file():
    d = tempfile.mkdtemp(prefix="issue48-loop-")
    try:
        seen = []

        def chat_fn(messages, tool_defs):
            seen.append(messages)
            n = len(seen)
            if n == 1:
                # The field move: write the deliverable into the
                # step's own record path under .harness.
                return _write_call(".harness/runs/X/step-03.md",
                                   "spare tubes, frayed cable")
            if n == 2:
                return _write_call("urgent-gear.txt",
                                   "spare tubes, frayed cable")
            return {"role": "assistant", "content": "DONE: gear listed"}

        hint = ("HARNESS: this step's output file is 'urgent-gear.txt' "
                "(named in the step's Then). Write to "
                "'urgent-gear.txt' instead.")
        r = loop.run("step task", Config(workspace=d), chat_fn=chat_fn,
                     refusal_hint=hint)
        check("the run recovers and finishes", r.status == "done",
              r.status)
        tool_results = [m["content"] for m in seen[-1]
                        if m.get("role") == "tool"]
        refusal = [c for c in tool_results if ".harness store" in c]
        check("the blocked write's feedback was seen",
              bool(refusal), str(tool_results))
        check("the refusal names the step's output file",
              any("urgent-gear.txt" in c for c in refusal),
              str(refusal))
        check("the deliverable landed at the named file",
              Path(d, "urgent-gear.txt").is_file())
    finally:
        shutil.rmtree(d)


def t_no_hint_no_naming():
    d = tempfile.mkdtemp(prefix="issue48-loop2-")
    try:
        seen = []

        def chat_fn(messages, tool_defs):
            seen.append(messages)
            if len(seen) == 1:
                return _write_call(".harness/runs/X/step-03.md", "x")
            return {"role": "assistant", "content": "DONE: stopped"}

        r = loop.run("step task", Config(workspace=d), chat_fn=chat_fn)
        tool_results = [m["content"] for m in seen[-1]
                        if m.get("role") == "tool"]
        check("without a hint the refusal stays as it was",
              any(".harness store" in c and "urgent-gear" not in c
                  for c in tool_results), str(tool_results))
    finally:
        shutil.rmtree(d)


def t_step_recovers_from_blocked_write_via_hint():
    """End to end at the step level: the fake model repeats the
    field move (write the deliverable into its own step record),
    reads the refusal, and recovers to the named file."""
    d = tempfile.mkdtemp(prefix="issue48-step-")
    try:
        state = {"calls": 0, "hint_seen": False}

        def chat_fn(messages, tool_defs):
            state["calls"] += 1
            for m in messages:
                if m.get("role") == "tool" and \
                        "this step's output file is " \
                        "'urgent-gear.txt'" in m["content"]:
                    state["hint_seen"] = True
            if state["calls"] == 1:
                return _write_call(".harness/runs/X/step-03.md",
                                   "spare tubes, frayed cable")
            if state["calls"] == 2:
                return _write_call("urgent-gear.txt",
                                   "spare tubes, frayed cable")
            return {"role": "assistant", "content": "DONE: gear listed"}

        step = planner.PlanStep(
            id=3, instruction=STEP3.instruction,
            done_when=STEP3.done_when,
            output_path=".harness/runs/X/step-03.md")
        ok = planner._run_step_verified(
            TASK, step, [STEP1, step], Config(workspace=d), chat_fn,
            Jail(d), lambda *a: None)
        check("the step completes after recovering", ok)
        check("the refusal the step saw named its output file",
              state["hint_seen"])
        check("the deliverable is at the Then-named file",
              Path(d, "urgent-gear.txt").read_text()
              == "spare tubes, frayed cable")
    finally:
        shutil.rmtree(d)


if __name__ == "__main__":
    t_subject_file_shapes()
    t_lint_flags_then_only_prose_file()
    t_lint_quiet_when_the_when_names_the_file()
    t_lint_quiet_for_absence_prose()
    t_step_prompt_names_the_then_only_output_file()
    t_step_prompt_quiet_when_when_names_the_file()
    t_refusal_names_the_steps_output_file()
    t_no_hint_no_naming()
    t_step_recovers_from_blocked_write_via_hint()
    t_preservation_clause_is_not_an_output()
    t_absence_subject_is_not_a_deliverable_for_clarity()
    t_move_destination_is_not_an_output()
    t_similar_source_name_does_not_hide_the_output()
    t_absence_intent_respects_parsed_verbs()
    t_preservation_subject_excluded_across_clauses()
    t_preservation_predicate_is_about_the_subject()
    t_preservation_subject_with_apostrophe()
    t_absence_predicate_is_about_the_subject()
    print("ALL PASSED")
