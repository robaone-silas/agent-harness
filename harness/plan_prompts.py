"""Planner prompt and workspace view (issue #62).

Split out of planner.py: what the planner model is shown. The
deterministic workspace digest (Tier 1 discovery), the root file
listing the lint and the Tier 2 reconciliation compare against,
the worked example plans, and the planner prompt itself.
planner.py re-exports these names, so existing imports keep working.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

def _file_entry_size(path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _format_digest_entry(path) -> str:
    if path.is_dir():
        return f"- {path.name}/ (folder)"
    return f"- {path.name} ({_file_entry_size(path)} bytes)"


def _visible_workspace_entries(workspace: str | None) -> list | None:
    """The workspace root's entries sorted by name, `.harness/`
    bookkeeping left out, or None when the root cannot be listed."""
    from pathlib import Path
    if not workspace:
        return None
    try:
        entries = sorted(Path(workspace).iterdir(), key=lambda path_entry: path_entry.name)
    except OSError:
        logger.debug("workspace root %r cannot be listed", workspace, exc_info=True)
        return None
    return [path_entry for path_entry in entries if path_entry.name != ".harness"]


def workspace_digest(workspace: str | None, limit: int = 100) -> str:
    """Deterministic pre-planning discovery (Tier 1, v0.8.1): the harness
    lists the workspace itself and shows the planner what is actually
    there, so plans are written against real files instead of the model's
    priors (field, 2026-10-03: a bare "organize by category" prompt got
    category folders for .pdf/.jpg files in a folder containing neither).

    Names, kinds, and sizes only: no content is read, no model calls.
    `.harness/` bookkeeping is never listed. Returns "" when no listing
    can be made: discovery must never block planning."""
    visible_entries = _visible_workspace_entries(workspace)
    if visible_entries is None:
        return ""
    if not visible_entries:
        digest_body = "(the workspace folder is currently empty)"
    else:
        digest_lines = [_format_digest_entry(path_entry) for path_entry in visible_entries[:limit]]
        digest_body = "\n".join(digest_lines)
        if len(visible_entries) > limit:
            digest_body += f"\n- ... and {len(visible_entries) - limit} more entries"
    return ("\nWorkspace contents right now (listed by the harness itself):\n"
            + digest_body
            + "\nPlan against what is actually here: do not invent files, "
              "extensions, or categories that are not present.\n")


_EXAMPLE_PLANS = """
Example plans (copy the shape, not the content):

Task: Make an index of the recipes in this folder
Feature: Recipe index
  Scenario: Step 1 - List the recipes
    When I list the files in the folder
    Then the output lists the recipe files
  Scenario: Step 2 - Write the index
    When I write recipe-index.md indexing the listed recipes
    Then "recipe-index.md" indexes the files from step 1

Task: Move the 2025 meeting notes into an archive folder
Feature: Archive meeting notes
  Scenario: Step 1 - List the notes
    When I list the files in the folder
    Then the output lists the note files
  Scenario: Step 2 - Create the archive folder
    When I create the folder "archive"
    Then "archive" exists
  Scenario: Step 3 - Move one note
    When I run: mv notes-2025-03.txt archive/
    Then "archive/notes-2025-03.txt" exists

Task: Make a reading guide to the articles in this folder: each entry gives the article's title and a one-sentence summary of its argument
Feature: Reading guide
  Scenario: Step 1 - List the articles
    Intent: know exactly which articles the guide must cover
    When I list the files in the folder
    Then the output lists the article files
  Scenario: Step 2 - Write the guide
    Intent: each entry gives the article's title and a one-sentence summary of its argument, not the article text
    When I read each article from step 1 and write guide.md with one entry per article
    Then "guide.md" has one entry for each article from step 1, and each entry gives the article's title and a one-sentence summary of its argument

Task: Build a catalog of the songs in this folder, one entry per song with its length
Feature: Song catalog
  Scenario: Step 1 - List the songs
    Intent: know exactly which songs the catalog must cover
    When I list the files in the folder
    Then the output lists the song files
  Scenario: Step 2 - Read the first group of songs
    When I read the first three songs from step 1 and note each song's length
    Then the output gives a length for each song read
  Scenario: Step 3 - Read the remaining songs
    When I read the remaining songs from step 1 and note each song's length
    Then the output gives a length for each song read
  Scenario: Step 4 - Write the catalog
    Intent: one entry per song with its length, covering every song from step 1
    When I write catalog.md with one entry per song from the notes in steps 2 and 3
    Then "catalog.md" has one entry for each song from step 1, with its length as noted in steps 2 and 3

Task: Make a volunteer roster from the sign-up sheets: each entry gives the volunteer, the role, and the shift, but no phone numbers
Feature: Volunteer roster
  Scenario: Step 1 - Read the sign-up sheets
    When I read the sign-up sheets in the folder and note each volunteer, role, shift, and phone number
    Then the output lists each volunteer with role, shift, and phone number
  Scenario: Step 2 - Write the roster
    Intent: each entry gives the volunteer, the role, and the shift, with no phone numbers
    When I write roster.txt with one entry per volunteer from step 1
    Then "roster.txt" preserves volunteer, role, and shift from step 1
    And "roster.txt" excludes phone numbers from step 1

Task: Add up the amounts in jan.txt, feb.txt, and mar.txt and put the total in total.txt
Feature: Quarterly total
  Scenario: Step 1 - Read the monthly files
    When I read "jan.txt", "feb.txt", and "mar.txt"
    Then the output shows the amount from each file
  Scenario: Step 2 - Write the total
    When I add the three amounts and write the total to total.txt
    Then "total.txt" contains the computed sum of the three amounts

Task: Make an inventory of the seed packets in this folder and what each plant is for, in a file I can keep
Feature: Seed packet inventory
  Scenario: Step 1 - List the packets
    Intent: know exactly which packets the inventory must cover
    When I list the files in the folder
    Then the output lists the packet files
  Scenario: Step 2 - Write the inventory
    Intent: each entry names the plant and what it is for, in a file the user can keep
    When I read each packet file from step 1 and write seed-inventory.txt with the plant name and its purpose
    Then "seed-inventory.txt" has one entry for each packet from step 1, naming the plant and what it is for

In the catalog example, note the shape: the per-item steps only gather,
and the final step still answers for every song from step 1 in a
single clause. Splitting the work across steps
never splits away the completeness obligation, and the obligation is
never scattered into one clause per file.

"""


_FINDINGS_GUIDANCE = (
    "\nBase the plan's categories, values, and groupings on "
    "these findings, not on assumptions about what such a "
    "workspace usually contains.\n"
    "\nReconcile the findings against the workspace contents "
    "listed above before planning: a workspace file that "
    "is absent from the findings is unexamined, not "
    "automatically out of scope. If an unexamined file "
    "could belong to this task, account for it in the plan "
    "(list it, read it, or name it) or explicitly exclude "
    "it; do not describe a set drawn only from the findings "
    "as complete, or as all of the files.\n")


def _discovery_findings_block(discovery: str) -> str:
    return ("\nDiscovery findings (from a read-only discovery pass "
            "the harness just ran):\n" + discovery + _FINDINGS_GUIDANCE)


def build_planner_prompt(task: str, tool_names: list[str],
                         workspace: str | None = None,
                         discovery: str = "") -> str:
    idioms = _idiom_block(task)
    digest = workspace_digest(workspace)
    if discovery:
        digest += _discovery_findings_block(discovery)
    return f"""You are a planner. Break the task below into a short sequence of small steps.
Write the plan in Gherkin — one Scenario per step, in order.

Format:
Feature: <short title for this task>
  Scenario: Step 1 - <short name>
    Intent <what this step is for, in the task's terms — see the intent rule below>
    When <one concrete action naming exact files or commands>
    Then <how to tell the step worked, checkable in files or command output>
  Scenario: Step 2 - <short name>
    When <one concrete action>
    Then <checkable outcome>

Available tools for the steps: {", ".join(tool_names)}.

Rules:
- 2 to 6 scenarios, ordered so each step's output feeds the next.
- Each When is one concrete action; each Then is checkable in files or command output.
- Write Then clauses as direct verifiable phrasings the harness checks itself:
  `"path" exists`, `"path" contains "text"`, `"path" contains exactly "text"`,
  `"path" has 3 lines`. Prefer these over prose — the harness verifies them
  in code after each step, and only falls back to trust for other phrasings.
- When a step writes about a listing from the filenames ALONE, without
  reading the files' contents (a strategy, triage, or sorting plan,
  often because the task forbids reading contents), write its Then as
  `"out.txt" accounts for the files from step N` (N = the listing
  step). The check is strict: every filename from the listing must
  appear in the document, so no file can be silently skipped.
- When a step's whole job is to read every file from an earlier listing
  step and compile an index, inventory, or catalog with a short
  description of each file, write its Then as
  `"out.txt" indexes the files from step N`. For an
  `indexes` step the harness builds that file itself: it reads each
  listed file, gets a one-line description of each, and writes the
  target with one entry per file, so no file can be omitted. Those
  descriptions are the harness's own generic one-liners. When each
  entry must carry specific content from the files (a title, a
  summary of its argument, a measured length), a generic description
  cannot promise it: keep the writing with the step itself and state
  that contract in a prose Then instead, as the reading guide
  example does.
- When a step compiles records that earlier steps READ (not listed)
  into a deliverable, and the fields each record must contribute
  matter (the task names them: a customer, an item, a status), write
  its Then as `"out.txt" preserves customer, item, and status from
  step N` (or `from steps N and M`). The harness harvests those
  fields' values from the source steps' records and checks every
  value survived into the deliverable. A generic prose clause is
  taken on trust and cannot catch a compilation that drops a field.
- When a deliverable must NOT carry something the earlier steps read
  (prices, internal notes, the contents of a specific file), write
  its Then as `"out.txt" excludes prices from step N` or
  `"out.txt" excludes the contents of "internal-notes.txt"`. The
  harness checks that none of those values appear in the deliverable.
- Name every file a step creates in that step's When, not only in its
  Then. The step executes its When; a deliverable named only in the
  Then may never be written, because the step was never told to
  write it.
- Each step runs under a turn budget that scales with the operations it
  implies: by default a base of 6 turns, plus one turn per file it must
  read beyond that, up to a maximum of 24. If a step would need more
  than the maximum, split it into two steps.
- Preserve the task's intent in every step. When the task specifies what a
  deliverable must contain or be like (for example: each entry gives a name
  and a one-line description), give every step that contributes to that
  deliverable an Intent line carrying the task's own words for it, and write
  its When to match the Intent: a description is not the content, and a
  summary is not the source text. Decomposition that drops the task's
  specification produces work that is finished and wrong.
- The Then must accurately describe THIS step's expected outcome. A precisely
  worded wrong expectation fails verification just as surely as a vague one.
- Concrete values come from the task statement only — never <angle-bracket
  placeholders> or phrases like "content from step 2". For values a step must
  COMPUTE, do not invent the result: write "total.txt contains the computed sum",
  never "total.txt contains exactly "12345"". Acceptance criteria state WHAT to
  check, not the implementation's literal output.
- A Given line is optional, for starting-state context a step needs.
- Steps may rely on data produced by earlier steps: the harness stores each
  step's full output (tool results included) in a file and tells the executor
  the exact path, so a later step can read an earlier step's real output.
  Still never invent the values themselves in a Then.
- Do NOT add verify or summarize scenarios — the harness handles finishing.
{_EXAMPLE_PLANS}- Return ONLY the Gherkin, no other text.
{idioms}{digest}Task: {task}"""


def _idiom_block(task: str) -> str:
    """Known idioms matching this task, for the planner prompt (may be empty)."""
    from . import suggestions as _sug
    found_idioms = _sug.for_task(task)
    if not found_idioms:
        return ""
    return ("\nKnown idioms that apply to this task — use them instead of "
            "hardcoding:\n" + _sug.render(found_idioms) + "\n")


def _root_workspace_files(workspace: str | None) -> list[str]:
    """Sorted names of the regular files at the workspace root.

    Root only, matching the digest's view of the workspace: folders and
    their contents are not files the discovery pass was expected to read,
    and `.harness/` bookkeeping is never a workspace file."""
    visible_entries = _visible_workspace_entries(workspace) or []
    return [path_entry.name for path_entry in visible_entries if path_entry.is_file()]
