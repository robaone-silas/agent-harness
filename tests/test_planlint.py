"""Unit tests for the deterministic plan lint (harness/planlint.py).

Run: python3 tests/test_planlint.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness import planlint, gherkin
from harness.planner import PlanStep, request_plan, to_plan_steps
from harness.config import Config


def _steps(task, pairs):
    return [PlanStep(id=i + 1, instruction=w, done_when=t)
            for i, (w, t) in enumerate(pairs)]


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + (f" -- {info}" if info and not cond else ""))
    assert cond, f"{name}: {info}"


def t_dangling_file():
    task = 'Write the text "Hello" to hello.txt in the workspace'
    steps = _steps(task, [
        ('I create the output directory', 'the directory "out" exists'),
        ('I write "Hello" to hello.txt',
         'the file "greeting.txt" contains exactly "Hello"'),
    ])
    fs = planlint.lint_plan(task, steps)
    errs = [f for f in fs if f.level == "error"]
    check("dangling_file fires", len(errs) == 1 and errs[0].code == "dangling_file",
          f"{[(f.code, f.detail) for f in fs]}")
    check("dangling_file names the file", "greeting.txt" in errs[0].detail, errs[0].detail)
    check("dangling_file on step 2", errs[0].step_id == 2, str(errs[0].step_id))


def t_value_from_nowhere():
    task = "Count the lines in notes.txt and write the count to count.txt"
    steps = _steps(task, [
        ("I count the lines in notes.txt", "the command output is a number"),
        ("I write the count to count.txt", 'count.txt contains exactly "42"'),
    ])
    fs = planlint.lint_plan(task, steps)
    warns = [f for f in fs if f.code == "value_from_nowhere"]
    check("value_from_nowhere fires", len(warns) == 1, f"{[(f.code, f.detail) for f in fs]}")
    check("value_from_nowhere on step 2", warns[0].step_id == 2, str(warns[0].step_id))
    check("value_from_nowhere is warn", warns[0].level == "warn", warns[0].level)


def t_value_grounded_in_task_passes():
    task = 'Write the text "Hello, harness!" to hello.txt'
    steps = _steps(task, [
        ('I write "Hello, harness!" to hello.txt',
         '"hello.txt" contains exactly "Hello, harness!"'),
    ])
    fs = planlint.lint_plan(task, steps)
    check("grounded value clean", fs == [], f"{[(f.code, f.detail) for f in fs]}")


def t_unparseable_check():
    task = "Run broken.py"
    steps = _steps(task, [
        ("I run broken.py", 'the output contains exactly "OK'),  # unterminated quote
    ])
    fs = planlint.lint_plan(task, steps)
    warns = [f for f in fs if f.code == "unparseable_check"]
    check("unparseable_check fires", len(warns) == 1, f"{[(f.code, f.detail) for f in fs]}")


def t_consistent_hallucination_not_catchable():
    # BAD-1 shape: When and Then consistently invent 12345. The lint cannot
    # see this — documented limit, needs model/human judgment.
    task = "Read data.csv, sum the 'amount' column, and write just the number to total.txt"
    steps = _steps(task, [
        ("I execute `awk -F',' 'NR>1 {sum += $2} END {print sum}' data.csv`",
         "the file `total.txt` should not exist"),
        ("I execute `echo 12345 > total.txt`",
         "the file `total.txt` should contain exactly `12345`"),
    ])
    fs = planlint.lint_plan(task, steps)
    check("consistent hallucination: no false error", not any(f.level == "error" for f in fs),
          f"{[(f.code, f.detail) for f in fs]}")


def t_good_plan_clean():
    task = "Read data.csv, sum the 'amount' column, and write just the number to total.txt"
    steps = _steps(task, [
        ("I execute `awk -F',' 'NR>1 {sum += $2} END {print sum}' data.csv`",
         "the output of the command contains the total sum of the 'amount' column"),
        ("I write the output of the previous step to total.txt",
         "the file total.txt contains the total sum of the 'amount' column"),
    ])
    fs = planlint.lint_plan(task, steps)
    check("good plan clean", fs == [], f"{[(f.code, f.detail) for f in fs]}")


def t_lint_retry_integration():
    bad = ("Feature: T\n  Scenario: Step 1\n"
           '    When I write "Hello" to hello.txt\n'
           '    Then the file "greeting.txt" contains exactly "Hello"\n')
    good = ("Feature: T\n  Scenario: Step 1\n"
            '    When I write "Hello" to hello.txt\n'
            '    Then the file "hello.txt" contains exactly "Hello"\n')
    calls = []

    def fake_chat(messages, tools):
        calls.append(messages[-1]["content"][:200])
        return {"content": bad if len(calls) == 1 else good}

    cfg = Config()
    text, steps = request_plan('Write "Hello" to hello.txt', [], fake_chat, cfg)
    check("lint retry fires", len(calls) == 2, f"calls={len(calls)}")
    check("retry mentions the flaw", "greeting.txt" in calls[1], calls[1])
    errs = [f for s in steps for f in s.lint if f["level"] == "error"]
    check("fixed plan has no errors", errs == [], str(errs))
    check("findings attached to steps", all(isinstance(s.lint, list) for s in steps))


def t_clean_plan_no_retry():
    good = ("Feature: T\n  Scenario: Step 1\n"
            '    When I write "Hello, harness!" to hello.txt\n'
            '    Then "hello.txt" contains exactly "Hello, harness!"\n')
    calls = []

    def fake_chat(messages, tools):
        calls.append(1)
        return {"content": good}

    cfg = Config()
    text, steps = request_plan('Write the text "Hello, harness!" to hello.txt',
                               [], fake_chat, cfg)
    check("clean plan: single call", len(calls) == 1, f"calls={len(calls)}")
    check("clean plan: lint empty", all(s.lint == [] for s in steps),
          str([s.lint for s in steps]))


def t_covers_near_miss_warns_unparseable():
    # planlint alignment with the widened verifier (2026-10-03): a Then
    # that reaches for "cover the files" in an arrangement the verifier
    # still cannot check ("will cover") must warn as an unparseable check
    # rather than attest silently.
    task = "Summarize the files in the folder"
    steps = _steps(task, [
        ("I list the files", "the list is recorded"),
        ("I write summary.txt", "summary.txt will cover the files from step 1"),
    ])
    fs = planlint.lint_plan(task, steps)
    warns = [f for f in fs if f.code == "unparseable_check"]
    check("covers near-miss warns", len(warns) == 1,
          f"{[(f.code, f.detail) for f in fs]}")
    check("covers near-miss is on step 2", warns[0].step_id == 2, str(warns[0].step_id))


def t_gherkin_still_validates_first():
    # Structural rejection still works and doesn't reach the lint.
    calls = []

    def fake_chat(messages, tools):
        calls.append(1)
        return {"content": "not gherkin at all"}

    cfg = Config()
    text, err = request_plan("do a thing", [], fake_chat, cfg)
    check("bad gherkin -> None", text is None, str(text))
    check("bad gherkin retried", len(calls) == 2, f"calls={len(calls)}")


if __name__ == "__main__":
    for fn in [t_dangling_file, t_value_from_nowhere, t_value_grounded_in_task_passes,
               t_unparseable_check, t_consistent_hallucination_not_catchable,
               t_covers_near_miss_warns_unparseable,
               t_good_plan_clean, t_lint_retry_integration, t_clean_plan_no_retry,
               t_gherkin_still_validates_first]:
        fn()
    # parse_then sanity (verify.py helper the lint relies on)
    from harness import verify as _v
    check("parse_then contains_exactly",
          _v.parse_then('"a.txt" contains exactly "hi"') == ("contains_exactly", "a.txt", "hi"),
          str(_v.parse_then('"a.txt" contains exactly "hi"')))
    check("parse_then exists", _v.parse_then('"a.txt" exists') == ("exists", "a.txt", None),
          str(_v.parse_then('"a.txt" exists')))
    check("parse_then None on prose", _v.parse_then("the output looks good") is None)
    print("\nAll planlint tests passed.")
