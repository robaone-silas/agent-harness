#!/usr/bin/env python3
"""Tests for the Gherkin plan contract: render, parse, validate, round-trip.

Run: python3 tests/test_gherkin.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness.gherkin import (
    FeaturePlan, ParsedStep, parse_feature, render_feature,
)


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        raise AssertionError(name)


FEATURE = """Feature: Sum the column
  # a comment line is ignored

  Scenario: Step 1 - Compute the sum
    Given data.csv exists with an amount column
    When run `awk -F',' 'NR>1{sum+=$2} END{print sum}' data.csv > total.txt`
    Then total.txt exists
    And total.txt contains exactly "100"

  Scenario: Step 2 - Show it
    When run `cat total.txt`
    Then the output is "100"
    But the output has no extra lines
"""


def t_parse_valid():
    plan, err = parse_feature(FEATURE)
    check("parse ok", plan is not None, err)
    check("title", plan.title == "Sum the column", plan.title)
    check("two steps", len(plan.steps) == 2, str(len(plan.steps)))
    s1, s2 = plan.steps
    check("given kept", s1.given == ["data.csv exists with an amount column"], str(s1.given))
    check("when kept", s1.when == ["run `awk -F',' 'NR>1{sum+=$2} END{print sum}' data.csv > total.txt`"])
    check("and attaches to then", s1.then == ['total.txt exists', 'total.txt contains exactly "100"'],
          str(s1.then))
    check("but attaches to then", s2.then == ['the output is "100"', "the output has no extra lines"],
          str(s2.then))


def t_render_canonical():
    plan, err = parse_feature(FEATURE)
    out = render_feature(plan.title, plan.steps)
    check("render feature", out.startswith("Feature: Sum the column"))
    check("render scenario", "Scenario: Step 1 - Compute the sum" in out)
    check("render given/when/then",
          "Given data.csv exists" in out and "When run `awk" in out
          and 'Then total.txt contains exactly "100"' in out, out[:200])


def t_round_trip():
    plan, err = parse_feature(FEATURE)
    plan2, err2 = parse_feature(render_feature(plan.title, plan.steps))
    check("round trip parses", plan2 is not None, err2)
    check("round trip stable",
          [(s.title, s.when, s.then) for s in plan2.steps] ==
          [(s.title, s.when, s.then) for s in plan.steps])


def t_fenced():
    plan, err = parse_feature("```gherkin\n" + FEATURE + "\n```")
    check("fenced gherkin parses", plan is not None and len(plan.steps) == 2, err)


def t_rejects():
    cases = [
        ("missing Then", "Feature: x\n  Scenario: s\n    When do it\n", "Then"),
        ("missing When", "Feature: x\n  Scenario: s\n    Then done\n", "When"),
        ("placeholder", "Feature: x\n  Scenario: s\n    When write <x> to f\n    Then f exists\n",
         "placeholder"),
        ("placeholder in Then", "Feature: x\n  Scenario: s\n    When do it\n    Then x is <y>\n",
         "placeholder"),
        ("no scenario", "Feature: x\n", "Scenario"),
        ("step outside scenario", "Feature: x\n  When do it\n", "outside"),
        ("dangling And", "Feature: x\n  Scenario: s\n    And do it\n", "no preceding"),
        ("empty When", "Feature: x\n  Scenario: s\n    When\n    Then done\n", "empty"),
        ("not gherkin", "Hello world\n", "not a Gherkin step"),
    ]
    for name, text, needle in cases:
        plan, err = parse_feature(text)
        check(f"reject: {name}", plan is None and needle in err, err)


if __name__ == "__main__":
    for fn in [t_parse_valid, t_render_canonical, t_round_trip, t_fenced, t_rejects]:
        fn()
    print("\nAll gherkin tests passed.")
