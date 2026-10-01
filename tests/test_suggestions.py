"""Unit tests for the suggestion library (harness/suggestions.py).

Run: python3 tests/test_suggestions.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness import suggestions as sug
from harness.planner import build_planner_prompt


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + (f" -- {info}" if info and not cond else ""))
    assert cond, f"{name}: {info}"


MULTI = ("Create a src/ directory. Inside it, write main.py that prints its own "
         "filename. Run it with python3 and save its output to out.txt.")


def t_matches_own_filename():
    found = sug.for_task(MULTI)
    check("matches multi-step task", len(found) == 1 and found[0]["id"] == "python-own-filename",
          str([s["id"] for s in found]))


def t_case_insensitive():
    found = sug.for_task("PRINTS ITS OWN FILENAME")
    check("case-insensitive", len(found) == 1)


def t_no_false_positive():
    found = sug.for_task('Write the text "Hello, harness!" to hello.txt')
    check("no match on write-file", found == [], str(found))
    found = sug.for_task("Read data.csv, sum the 'amount' column, and write just the number to total.txt")
    check("no match on read-and-compute", found == [], str(found))


def t_idiom_content():
    found = sug.for_task(MULTI)
    body = found[0]["body"]
    check("idiom has basename", "os.path.basename(__file__)" in body, body[:80])
    check("idiom warns against literal", "literal" in body.lower(), body[:80])
    check("idiom has alternative", "sys.argv[0]" in body, body[:80])


def t_planner_prompt_includes():
    p = build_planner_prompt(MULTI, ["exec", "write_file"])
    check("planner prompt has idiom", "os.path.basename(__file__)" in p)
    check("planner prompt has header", "Known idioms" in p)


def t_planner_prompt_omits():
    p = build_planner_prompt('Write the text "Hi" to hi.txt', ["write_file"])
    check("planner prompt omits when no match", "Known idioms" not in p)


def t_render():
    out = sug.render(sug.for_task(MULTI))
    check("render non-empty", "os.path.basename" in out)


if __name__ == "__main__":
    for fn in [t_matches_own_filename, t_case_insensitive, t_no_false_positive,
               t_idiom_content, t_planner_prompt_includes, t_planner_prompt_omits,
               t_render]:
        fn()
    print("\nAll suggestion tests passed.")
