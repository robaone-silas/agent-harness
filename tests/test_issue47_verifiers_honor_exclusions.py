"""Issue #47 option 1: verifiers honor exclusions.

The Oct 8 failure: a plan listed business files only in intent but
checked completeness against step 1's full listing, so the verified
checks required the excluded school form to appear. Option 1 makes
the checks compute over the source set minus what the plan's
contents-of excludes clauses declare for the same deliverable:
subtracted indexes proves presence of the included set, excludes
proves absence of the excluded content, and only the pair is a
complete contract.

Scoping, exactly as analyzed: same deliverable only, current or
earlier steps only. Only the contents-of excludes form yields a
filename to subtract; literal and fields forms yield none. An
unparseable clause (prose target) yields none and attests, the
same hole as the Oct 30 unquoted-preserves facet.

Known caveat, documented here rather than fixed: subtraction
treats content-exclusion as file-exclusion. A redaction shape
(a deliverable that must list every file by name but scrub one
file's contents) loses the name proof for the scrubbed file,
because its filename is subtracted from the indexes set even
though the plan wanted the name listed and only the content
scrubbed. A contains clause for the filename could restore it.

Run: python3 tests/test_issue47_verifiers_honor_exclusions.py
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness import planner, verify
from harness.tools import Jail

BUSINESS = ["gig-gear-notes.txt", "parts-inventory.csv",
            "repair-fender-amp.txt"]
ALL_FILES = BUSINESS + ["school-form.txt"]

SCHOOL_FORM = """Student: Jamie Rivera.
Field trip to the science museum on Friday.
Permission granted by the parent guardian.
"""

CORRECT = """# Business inventory
- gig-gear-notes.txt: Instructions for checking and maintaining performance gear.
- parts-inventory.csv: keybed assembly, 0 in stock; fader, 2 in stock.
- repair-fender-amp.txt: Repair request details for a Fender guitar amplifier.
"""

WRONG = CORRECT + """- school-form.txt: Field trip to the science museum on Friday.
"""

INDEXES = '"business-inventory.txt" indexes the files from step 1'
EXCLUDES = '"business-inventory.txt" excludes the contents of "school-form.txt"'


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name
          + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        raise AssertionError(name)


def _make_jail(tmp, deliverable):
    root = Path(tmp)
    (root / "gig-gear-notes.txt").write_text(
        "Instructions for checking and maintaining performance gear.\n")
    (root / "parts-inventory.csv").write_text(
        "keybed assembly, 0 in stock; fader, 2 in stock.\n")
    (root / "repair-fender-amp.txt").write_text(
        "Customer: Elena Vasquez. Item: Fender guitar amplifier. "
        "Status: ready for pickup.\n")
    (root / "school-form.txt").write_text(SCHOOL_FORM)
    (root / "business-inventory.txt").write_text(deliverable)
    return Jail(root)


def _steps_with_excludes():
    listing = planner.PlanStep(
        id=1, instruction="list the files",
        done_when="the output lists the files")
    compile_step = planner.PlanStep(
        id=2, instruction="compile the inventory",
        done_when=f"{INDEXES}\n{EXCLUDES}")
    return [listing, compile_step]


def _build_verify_context(jail, excluded):
    return verify.VerifyContext(
        jail=jail, step_id=2, task="business files only",
        prior_items={1: list(ALL_FILES)}, excluded_files=excluded)


def _checks(jail, excluded, deliverable_clauses):
    verify_context = _build_verify_context(jail, excluded)
    return verify.verify_step(deliverable_clauses, verify_context)


def t_subcase_b_trap_gone():
    """Correct deliverable with indexes plus excludes passes both."""
    tmp = tempfile.mkdtemp(prefix="issue47-test-")
    jail = _make_jail(tmp, CORRECT)
    excluded = verify._excluded_files_for(_steps_with_excludes(), 2)
    check("helper finds the contents-of filename",
          excluded == {"business-inventory.txt": {"school-form.txt"}},
          str(excluded))
    result = _checks(jail, excluded, [INDEXES, EXCLUDES])
    by_name = {check_result.verifier: check_result for check_result in result.checks}
    check("indexes passes on the subtracted set",
          by_name["indexes"].ok, by_name["indexes"].detail)
    check("excludes passes on the scrubbed deliverable",
          by_name["excludes"].ok, by_name["excludes"].detail)
    check("step passes overall", result.ok, str(result.checks))


def t_wrong_deliverable_fails_on_excludes():
    """Presence alone does not catch the leak; excludes does."""
    tmp = tempfile.mkdtemp(prefix="issue47-test-")
    jail = _make_jail(tmp, WRONG)
    excluded = verify._excluded_files_for(_steps_with_excludes(), 2)
    result = _checks(jail, excluded, [INDEXES, EXCLUDES])
    by_name = {check_result.verifier: check_result for check_result in result.checks}
    check("indexes passes (presence alone misses the leak)",
          by_name["indexes"].ok, by_name["indexes"].detail)
    check("excludes fails on the leaked school content",
          not by_name["excludes"].ok, by_name["excludes"].detail)
    check("step fails overall", not result.ok, str(result.checks))


def t_no_excludes_identical_to_today():
    """No excludes clauses means the full listing is required."""
    tmp = tempfile.mkdtemp(prefix="issue47-test-")
    listing = planner.PlanStep(
        id=1, instruction="list the files",
        done_when="the output lists the files")
    compile_step = planner.PlanStep(
        id=2, instruction="compile the inventory",
        done_when=INDEXES)
    excluded = verify._excluded_files_for([listing, compile_step], 2)
    check("helper finds nothing without excludes clauses",
          excluded == {}, str(excluded))
    jail = _make_jail(tmp, CORRECT)
    result = _checks(jail, excluded, [INDEXES])
    check("three-file deliverable fails indexes without excludes",
          not result.checks[0].ok, result.checks[0].detail)
    jail = _make_jail(tmp, WRONG)
    result = _checks(jail, excluded, [INDEXES])
    check("four-file deliverable passes indexes without excludes",
          result.checks[0].ok, result.checks[0].detail)


def t_redaction_shape_caveat():
    """KNOWN CAVEAT, not a bug: subtraction treats content-exclusion
    as file-exclusion. A deliverable that lists business files only
    passes indexes once school-form.txt is excluded, even though
    the plan's indexes clause nominally covers the full listing:
    the name proof for the scrubbed file is lost. A contains
    clause for the filename could restore it, but the planner
    would have to know to add one."""
    tmp = tempfile.mkdtemp(prefix="issue47-test-")
    jail = _make_jail(tmp, CORRECT)
    excluded = verify._excluded_files_for(_steps_with_excludes(), 2)
    result = _checks(jail, excluded, [INDEXES])
    check("indexes passes without the scrubbed filename present",
          result.checks[0].ok, result.checks[0].detail)
    result = _checks(jail, {}, [INDEXES])
    check("same deliverable fails indexes without the exclusion",
          not result.checks[0].ok, result.checks[0].detail)


def t_non_contents_forms_subtract_nothing():
    """Literal and fields excludes clauses yield no filename."""
    literal = planner.PlanStep(
        id=2, instruction="compile",
        done_when='"business-inventory.txt" excludes "SOLD"')
    fields = planner.PlanStep(
        id=2, instruction="compile",
        done_when='"business-inventory.txt" excludes customer from step 1')
    check("literal form yields nothing",
          verify._excluded_files_for([literal], 2) == {})
    check("fields form yields nothing",
          verify._excluded_files_for([fields], 2) == {})
    tmp = tempfile.mkdtemp(prefix="issue47-test-")
    jail = _make_jail(tmp, CORRECT)
    result = _checks(jail, {}, [INDEXES])
    check("indexes still requires the full listing",
          not result.checks[0].ok, result.checks[0].detail)


def t_scoping_same_deliverable_only():
    """An excludes clause for another file does not shrink this check."""
    other = planner.PlanStep(
        id=2, instruction="compile",
        done_when='"other.txt" excludes the contents of "school-form.txt"')
    excluded = verify._excluded_files_for([other], 2)
    check("other deliverable exclusion is scoped away",
          excluded == {"other.txt": {"school-form.txt"}}, str(excluded))
    tmp = tempfile.mkdtemp(prefix="issue47-test-")
    jail = _make_jail(tmp, CORRECT)
    result = _checks(jail, excluded, [INDEXES])
    check("indexes still requires school-form.txt for this deliverable",
          not result.checks[0].ok, result.checks[0].detail)


def t_scoping_earlier_or_same_step_only():
    """An excludes clause on a later step does not shrink this check."""
    listing = planner.PlanStep(
        id=1, instruction="list", done_when="the output lists")
    compile_step = planner.PlanStep(
        id=2, instruction="compile", done_when=INDEXES)
    finalize = planner.PlanStep(
        id=3, instruction="finalize", done_when=EXCLUDES)
    steps = [listing, compile_step, finalize]
    check("later-step exclusion ignored for step 2",
          verify._excluded_files_for(steps, 2) == {})
    check("same-or-earlier exclusion visible at step 3",
          verify._excluded_files_for(steps, 3)
          == {"business-inventory.txt": {"school-form.txt"}})


def t_unquoted_target_subtracts_nothing():
    """A prose-target excludes clause does not parse, so it
    subtracts nothing; a bare path-shaped contents source does."""
    prose = planner.PlanStep(
        id=2, instruction="compile",
        done_when="the output excludes the contents of the school form")
    check("prose clause does not parse as excludes",
          verify.parse_then(
              "the output excludes the contents of the school form")
          is None)
    check("prose clause yields nothing",
          verify._excluded_files_for([prose], 2) == {})
    bare = planner.PlanStep(
        id=2, instruction="compile",
        done_when='"business-inventory.txt" excludes the contents of '
                  'school-form.txt')
    check("bare path-shaped contents source parses and subtracts",
          verify._excluded_files_for([bare], 2)
          == {"business-inventory.txt": {"school-form.txt"}})


def main():
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("t_") and callable(v)]
    for fn in fns:
        fn()
    print(f"{len(fns)} tests passed")


if __name__ == "__main__":
    main()
