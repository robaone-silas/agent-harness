"""Richer parse-rejection feedback for the planner (2026-10-08).

Field failure, 2026-10-08 persona pools proof run: Tomas's yard
inventory finished discovery cleanly, then the final plan pasted a
discovery finding ("- garden-journal.txt: planted three tomato
varieties...") into the plan where a step belonged. The parse
rejection feedback the model got was the parser's error and nothing
else: "That plan was rejected: line 10: not a Gherkin step: ...."
plus "Return ONLY the corrected Gherkin". That names the symptom,
but the model's confusion was categorical: findings treated as plan
material. It made the same mistake on the retry and planning gave
up (the run recorded BLOCKED, no reviewable plan).

The project's standing lesson is that small models copy shapes
better than they obey rules, and the feedback that has worked
(verification retries, lint findings) names the exact fix at the
failure point. The parse path was the one rejection that did none
of that. It now also states the grammar in one sentence, shows a
minimal plan skeleton to copy, and, when the offending line came
from the discovery notes, says so.

On the unfixed code the feedback carries none of the additions, so
the content tests below fail red. The lint-feedback test passes
both ways by design: that path is pinned unchanged.

Run: python3 tests/test_planner_parse_feedback.py
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness import planner  # noqa: E402
from harness.config import Config  # noqa: E402

TASK = "Make me a list of my yard and garage papers and what each one is for."

FINDING = ("- garden-journal.txt: planted three tomato varieties "
           "in May and peppers in June.")

DISCOVERY = (
    "Discovery findings (from a read-only pass over the workspace):\n"
    f"{FINDING}\n"
    "- mower-manual.txt: change the oil every spring.\n"
)

BAD_PLAN_PASTED = f"""Feature: Yard Papers
  Scenario: Step 1 - List the yard papers
    When I list the files in the folder
    Then the output lists the yard paper files
{FINDING}
  Scenario: Step 2 - Write the list
    When I write "yard-papers.txt" from the listing in step 1
    Then "yard-papers.txt" exists
"""

BAD_PLAN_JUNK = """Feature: Yard Papers
  Scenario: Step 1 - List the yard papers
    When I list the files in the folder
    Then the output lists the yard paper files
Here are the papers I found in the folder today.
  Scenario: Step 2 - Write the list
    When I write "yard-papers.txt" from the listing in step 1
    Then "yard-papers.txt" exists
"""

GOOD_PLAN = """Feature: Yard Papers
  Scenario: Step 1 - List the yard papers
    When I list the files in the folder
    Then the output lists the yard paper files
  Scenario: Step 2 - Write the list
    When I write "yard-papers.txt" from the listing in step 1
    Then "yard-papers.txt" exists
"""

# Parses cleanly but trips a planlint ERROR (dangling_file): the
# contains target is never produced by any step and is not in the
# (empty) workspace.
LINT_BAD_PLAN = """Feature: Yard Papers
  Scenario: Step 1 - Write the list
    When I write "yard-papers.txt" from my notes
    Then "missing-source.txt" contains "garden"
"""

LINT_GOOD_PLAN = """Feature: Yard Papers
  Scenario: Step 1 - Write the list
    When I write "yard-papers.txt" from my notes
    Then "yard-papers.txt" exists
"""


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name
          + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        raise AssertionError(name)


def _scripted(responses):
    calls = []

    def fake_chat(messages, tools):
        calls.append(messages[-1]["content"])
        return {"content": responses[min(len(calls) - 1,
                                          len(responses) - 1)]}
    return fake_chat, calls


def _request(responses, discovery=""):
    fake_chat, calls = _scripted(responses)
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Config(workspace=tmp)
        text, steps = planner.request_plan(
            TASK, [], fake_chat, cfg, discovery=discovery)
    return text, steps, calls


def t_parse_feedback_names_the_error():
    _text, steps, calls = _request([BAD_PLAN_JUNK, GOOD_PLAN])
    check("the retry happened", len(calls) == 2, str(len(calls)))
    check("the corrected plan was accepted", isinstance(steps, list),
          str(steps)[:120])
    feedback = calls[1]
    check("feedback carries the parser's error",
          "That plan was rejected:" in feedback
          and "not a Gherkin step" in feedback, feedback[:300])


def t_parse_feedback_teaches_the_shape():
    _text, _steps, calls = _request([BAD_PLAN_JUNK, GOOD_PLAN])
    feedback = calls[1]
    check("feedback states the plan grammar",
          "A plan contains only" in feedback
          and "Given/When/Then" in feedback, feedback[:400])
    check("feedback forbids pasted notes as plan lines",
          "never" in feedback and "lines in the plan" in feedback,
          feedback[:400])
    check("feedback shows a skeleton to copy",
          "Feature: <name>" in feedback
          and "Scenario: Step 1 -" in feedback
          and "When I <action>" in feedback, feedback[:500])
    check("feedback still demands Gherkin only",
          feedback.rstrip().endswith(
              "Return ONLY the corrected Gherkin, no other text."),
          feedback[-120:])


def t_pasted_discovery_line_is_named():
    _text, _steps, calls = _request(
        [BAD_PLAN_PASTED, GOOD_PLAN], discovery=DISCOVERY)
    feedback = calls[1]
    check("feedback names the discovery paste",
          "came from the discovery notes" in feedback, feedback[:400])


def t_foreign_junk_is_not_blamed_on_discovery():
    _text, _steps, calls = _request(
        [BAD_PLAN_JUNK, GOOD_PLAN], discovery=DISCOVERY)
    feedback = calls[1]
    check("unrelated junk is not blamed on the discovery notes",
          "came from the discovery notes" not in feedback,
          feedback[:400])
    check("the shape teaching is still there",
          "A plan contains only" in feedback, feedback[:400])


def t_lint_feedback_is_unchanged():
    _text, steps, calls = _request([LINT_BAD_PLAN, LINT_GOOD_PLAN])
    check("the lint retry happened", len(calls) == 2, str(len(calls)))
    check("the corrected plan was accepted", isinstance(steps, list),
          str(steps)[:120])
    feedback = calls[1]
    check("lint feedback keeps its design-flaws form",
          feedback.startswith("That plan has design flaws:"),
          feedback[:200])
    check("lint feedback gains no skeleton",
          "A plan contains only" not in feedback, feedback[:300])


def t_discovery_planner_feedback_is_rich():
    fake_chat, calls = _scripted([BAD_PLAN_JUNK, GOOD_PLAN])
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Config(workspace=tmp)
        text, steps = planner.request_discovery_plan(TASK, cfg, fake_chat)
    check("the discovery retry happened", len(calls) == 2,
          str(len(calls)))
    check("the corrected discovery plan was accepted",
          isinstance(steps, list), str(steps)[:120])
    feedback = calls[1]
    check("discovery feedback teaches the shape too",
          "A plan contains only" in feedback
          and "Scenario: Step 1 -" in feedback, feedback[:400])


def main():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("t_")]
    for fn in fns:
        fn()
    print(f"{len(fns)} tests passed")


if __name__ == "__main__":
    main()
