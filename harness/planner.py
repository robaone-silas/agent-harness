"""Plan-then-execute mode, v0.6: plans are Gherkin.

Measured e2b behavior: reliable single atomic actions, no sequencing,
no completion tracking. So the harness takes over the sequencing, and the
plan itself is a Gherkin feature file — one Scenario per step — which is
the shared language between the planner, the human approving the plan,
and the executor:

  1. Propose: ask the model (once) for a Gherkin plan; parse and validate
     it. A plan with a missing Then or an unresolvable <placeholder> is
     rejected here, never executed.
  2. Approve (human): the .feature file is reviewed/edited, Then clauses
     first — they define what "done" means.
  3. Execute: each Scenario runs as one small scoped instruction, tracked
     step by step in code.

`run_planned` does propose+execute in one go (auto mode, what the eval
uses). `propose` + `execute_plan` are the separable halves for the
approval flow.
"""
from __future__ import annotations

import re as _re
from dataclasses import dataclass, field, replace

from . import gherkin
from . import graphcheck as _graphcheck
from . import indexing as _indexing
from . import loop
from .config import Config


def _file_entry_size(path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _format_digest_entry(path) -> str:
    if path.is_dir():
        return f"- {path.name}/ (folder)"
    return f"- {path.name} ({_file_entry_size(path)} bytes)"


def workspace_digest(workspace: str | None, limit: int = 100) -> str:
    """Deterministic pre-planning discovery (Tier 1, v0.8.1): the harness
    lists the workspace itself and shows the planner what is actually
    there, so plans are written against real files instead of the model's
    priors (field, 2026-10-03: a bare "organize by category" prompt got
    category folders for .pdf/.jpg files in a folder containing neither).

    Names, kinds, and sizes only: no content is read, no model calls.
    `.harness/` bookkeeping is never listed. Returns "" when no listing
    can be made: discovery must never block planning."""
    from pathlib import Path
    if not workspace:
        return ""
    try:
        entries = sorted(Path(workspace).iterdir(), key=lambda path_entry: path_entry.name)
    except OSError:
        return ""
    visible_entries = [path_entry for path_entry in entries if path_entry.name != ".harness"]
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


def build_planner_prompt(task: str, tool_names: list[str],
                         workspace: str | None = None,
                         discovery: str = "") -> str:
    idioms = _idiom_block(task)
    digest = workspace_digest(workspace)
    if discovery:
        digest += ("\nDiscovery findings (from a read-only discovery pass "
                   "the harness just ran):\n" + discovery
                   + "\nBase the plan's categories, values, and groupings on "
                     "these findings, not on assumptions about what such a "
                     "workspace usually contains.\n"
                   + "\nReconcile the findings against the workspace contents "
                     "listed above before planning: a workspace file that "
                     "is absent from the findings is unexamined, not "
                     "automatically out of scope. If an unexamined file "
                     "could belong to this task, account for it in the plan "
                     "(list it, read it, or name it) or explicitly exclude "
                     "it; do not describe a set drawn only from the findings "
                     "as complete, or as all of the files.\n")
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


@dataclass
class PlanStep:
    id: int
    instruction: str
    done_when: str
    given: str = ""
    title: str = ""  # Scenario title from the Gherkin (v0.8.1 plan frame)
    intent: str = ""  # the step's Intent: line (v0.8.1 intent preservation)
    status: str = "pending"  # pending | done | failed
    result: str = ""
    verify: list = field(default_factory=list)  # per-Then check records (v0.7)
    lint: list = field(default_factory=list)  # planlint findings (dicts)
    verify_waived: bool = False  # verification failed but step accepted anyway
    done_via_world_state: bool = False  # sub-run missed DONE; verification passed (2026-10-04 ruling)
    output_path: str = ""  # .harness/runs/<run>/step-NN.md (v0.8.1, issue #2)
    source_items: list = field(default_factory=list)  # names this step produced (v0.8.1 covers)
    tool_summary: str = ""  # harness-computed "tools used; output size" line
    budget: int = 0  # effective turn budget for this step (issue #14)
    touched_args: set = field(default_factory=set)  # dumped tool-call args
    # from this step's completed attempts (issue #17 read coverage)


def to_plan_steps(plan: gherkin.FeaturePlan) -> list[PlanStep]:
    steps = []
    for step_index, step_definition in enumerate(plan.steps, 1):
        steps.append(PlanStep(
            id=step_index,
            instruction="\n".join(step_definition.when)[:500],
            # done_when is the step's verification contract: badges, lint,
            # executor prompts, and verification all consume it. It is NOT
            # length-capped. A 300-char cap silently truncated combined
            # Then clauses mid-clause (issue #7: a six-clause step lost its
            # tail), so every clause is preserved exactly.
            done_when="\n".join(step_definition.then),
            given="\n".join(step_definition.given)[:300],
            title=(step_definition.title or "")[:120],
            intent=(step_definition.intent or "")[:300],
        ))
    for plan_step in steps:
        plan_step.budget = step_budget(plan_step, steps)
    return steps


# Dynamic per-step budgets (issue #14): a flat cap judged every step
# against the same number of turns no matter how much work its own plan
# implied, so a compile step needing 7 reads, a write, and the final
# answer died against a cap of 6 with its deliverable unwritten. The
# budget now counts the planned operations deterministically: every
# distinct file operand the step names, plus, for each earlier step it
# draws on, one turn to read that step's stored record and one turn per
# source item that step produced, plus slack for the write and the
# final answer. Floored at the configured base, capped at the ceiling;
# the repeat-call breaker, the error limits, and the pushback refunds
# still bound a step that wanders instead of working.
_BUDGET_SLACK = 2


_RANGE_CONNECTORS = frozenset({"through", "to", "-", "–", "—"})


def _expand_step_range(start: int, end: int) -> set[int]:
    lower_bound, upper_bound = min(start, end), max(start, end)
    if upper_bound - lower_bound <= 64:
        return set(range(lower_bound, upper_bound + 1))
    return {start, end}


def _parse_step_segment_tokens(tokens: list[str]) -> set[int]:
    step_references: set[int] = set()
    previous_step_number: int | None = None
    connector: str | None = None
    for token in tokens:
        if not token.isdigit():
            connector = token.lower()
            continue
        step_number = int(token)
        if previous_step_number is not None and connector in _RANGE_CONNECTORS:
            step_references.update(_expand_step_range(previous_step_number, step_number))
        else:
            step_references.add(step_number)
            if previous_step_number is not None:
                step_references.add(previous_step_number)
        previous_step_number, connector = step_number, None
    if previous_step_number is not None:
        step_references.add(previous_step_number)
    return step_references


def _step_refs(text: str) -> set[int]:
    """Step numbers a step's text draws on (issue #32).

    Planners refer to earlier steps in whatever phrasing comes
    naturally: singular ("step 1", "step 1 and step 2"), plural lists
    ("steps 1 and 2", "steps 1, 2, and 3"), and ranges ("steps 1
    through 5", "steps 1 to 5", "steps 1-5", "step 1 through 5").
    The budget counter originally recognized only the singular form,
    so a compile step phrased as a plural range counted no prior
    steps at all and kept the flat base. A range counts every step
    in it, a list counts each member: a referenced prior step is
    read once however it was named.
    """
    import re as _re
    step_references = {int(matched_step) for matched_step in _re.findall(r"\bstep\s+(\d+)", text,
                                        flags=_re.IGNORECASE)}
    segment_pattern = _re.compile(
        r"\bsteps?\s+(\d+(?:(?:\s*(?:,|and|through|to|[-–—])\s*)+\d+)+)",
        _re.IGNORECASE)
    token_pattern = _re.compile(r"\d+|through|to|and|,|[-–—]", _re.IGNORECASE)
    for segment_match in segment_pattern.finditer(text):
        tokens = token_pattern.findall(segment_match.group(1))
        step_references.update(_parse_step_segment_tokens(tokens))
    return step_references


def _step_quoted_operands(text: str) -> set[str]:
    import re as _re
    return {quoted_token for quoted_token in _re.findall(r'"([^"\n]+)"', text)
            if quoted_token.strip() and not any(character.isspace() for character in quoted_token)}


def _prior_step_cost(prior_step: PlanStep) -> int:
    return 1 + len(prior_step.source_items or [])


def step_budget(step: PlanStep, steps: list[PlanStep], config=None) -> int:
    """Effective turn budget for one plan step, from its planned work."""
    base_budget = getattr(config, "step_max_steps", 6) if config is not None else 6
    ceiling_budget = (getattr(config, "step_budget_ceiling", 24)
               if config is not None else 24)
    step_text = "\n".join([step.given or "", step.intent or "",
                           step.instruction or "", step.done_when or ""])
    operations_count = len(_step_quoted_operands(step_text))
    steps_by_id = {plan_step.id: plan_step for plan_step in steps}
    for step_reference in _step_refs(step_text):
        prior_step = steps_by_id.get(step_reference)
        if prior_step is not None and prior_step.id < step.id:
            operations_count += _prior_step_cost(prior_step)
    return max(base_budget, min(ceiling_budget, operations_count + _BUDGET_SLACK))


_PLAN_SHAPE_FEEDBACK = (
    "A plan contains only Feature, Scenario, Intent, and Given/When/Then "
    "lines. Notes and findings are background for writing steps, never "
    "lines in the plan.\n"
    "Shape:\n"
    "  Feature: <name>\n"
    "    Scenario: Step 1 - <goal>\n"
    "      When I <action>\n"
    "      Then <checkable result>"
)


def _normalize_for_matching(text: str) -> str:
    """Collapse whitespace and case so two texts can be compared
    without formatting differences getting in the way."""
    return " ".join((text or "").split()).lower()


def _parse_feedback(parse_error: str, response: str,
                    discovery_notes: str = "") -> str:
    """The full rejection message for a plan that fails Gherkin parse.

    The parser's error names the symptom; the retry stands a better
    chance when the message also teaches at the failure point: the
    grammar in one sentence, a skeleton to copy, and, when the
    offending line was pasted out of the discovery notes (Tomas's
    2026-10-08 yard inventory: a findings bullet sitting where a
    step belonged, and the same mistake on the retry), that
    confusion named directly.
    """
    import re
    message_parts = [f"That plan was rejected: {parse_error}."]
    line_match = re.search(r"line (\d+)", parse_error or "")
    if line_match and discovery_notes:
        # The parser numbers lines in the fence-stripped text, so
        # read the offending line from that same text: indexing the
        # raw response lands one line early whenever the model
        # wrapped its plan in a Markdown fence.
        plan_lines = gherkin._strip_fences(response or "").splitlines()
        line_number = int(line_match.group(1))
        if 1 <= line_number <= len(plan_lines):
            offending_line = _normalize_for_matching(
                plan_lines[line_number - 1])
            if offending_line and offending_line in \
                    _normalize_for_matching(discovery_notes):
                message_parts.append(
                    "That line came from the discovery notes; "
                    "do not paste findings into the plan.")
    message_parts.append(_PLAN_SHAPE_FEEDBACK)
    message_parts.append(
        "Return ONLY the corrected Gherkin, no other text.")
    return "\n".join(message_parts)


def request_plan(task: str, tool_names: list[str], chat_fn, config: Config,
                 discovery: str = ""
                 ) -> tuple[str | None, list[PlanStep] | str]:
    """Ask the model for a Gherkin plan; retry with rejections explained.

    Two validations run per proposal: structural (Gherkin parse) then design
    (deterministic plan lint). Either can trigger the one retry; findings are
    attached to the returned steps as s.lint for the approval display.
    Returns (gherkin_text, steps) or (None, error_string)."""
    from . import planlint as _planlint
    messages = [{"role": "user",
                 "content": build_planner_prompt(task, tool_names,
                                                  workspace=config.workspace,
                                                  discovery=discovery)}]
    last_problem = "no response"
    for attempt in (1, 2):
        try:
            response_message = chat_fn(messages, None)
        except Exception as error:
            return None, f"planner call failed: {error}"
        content = (response_message.get("content") or "").strip()
        plan, parse_error = gherkin.parse_feature(content)
        if plan is None:
            feedback = _parse_feedback(parse_error, content, discovery)
            last_problem = parse_error
        else:
            steps = to_plan_steps(plan)
            findings = _planlint.lint_plan(
                task, steps,
                workspace_files=_root_workspace_files(config.workspace))
            findings_by_step: dict[int, list] = {}
            for finding in findings:
                findings_by_step.setdefault(finding.step_id, []).append(finding.as_dict())
            for plan_step in steps:
                plan_step.lint = findings_by_step.get(plan_step.id, [])
            error_findings = [finding for finding in findings if finding.level == "error"]
            if not error_findings or attempt == 2:
                return content, steps
            feedback = ("That plan has design flaws: "
                        + "; ".join(finding.detail for finding in error_findings)
                        + ". Return ONLY the corrected Gherkin, "
                        "no other text.")
            last_problem = "; ".join(finding.detail for finding in error_findings)
        messages.append({"role": "assistant", "content": content})
        messages.append({"role": "user", "content": feedback})
    return None, last_problem


def _default_chat_fn(config: Config):
    from . import client as _client

    def chat_fn(messages, tool_definitions):
        tools = _client.to_ollama_tools(tool_definitions) if tool_definitions else None
        return _client.chat(
            config.endpoint, config.model, messages,
            tools=tools,
            temperature=config.temperature, top_p=config.top_p,
            timeout=config.request_timeout)

    return chat_fn


def _chat_and_tools(config: Config, chat_fn=None):
    from . import tools as _tools
    effective_chat_fn = chat_fn or _default_chat_fn(config)
    jail = _tools.Jail(config.workspace)
    registry = _tools.make_tools(jail, config.blocked_substrings, config.exec_timeout,
                                 config.max_output_chars)
    return effective_chat_fn, jail, registry


def propose(task: str, config: Config, chat_fn=None
            ) -> tuple[str | None, list[PlanStep] | str]:
    """Propose a Gherkin plan without executing it. Returns (gherkin_text,
    steps) or (None, error_string)."""
    chat_fn, jail, registry = _chat_and_tools(config, chat_fn)
    return request_plan(task, list(registry), chat_fn, config)


# ------------------------------------------------------- Tier 2 discovery
#
# Some plans cannot be written well until someone has looked: the task's
# categories, values, or structure depend on what the workspace actually
# contains. Tier 1 (the digest) covers names and sizes. Tier 2 runs a
# model-planned discovery pass first, restricted at the registry level
# to read-only tools so it cannot change anything, stores its records
# like any run, and then plans again with the findings in the prompt.
# The plan the human approves is the informed one.

READ_ONLY_TOOLS = ("list_dir", "read_file", "grep_files")

_EXHAUSTIVE_WORDS_RE = _re.compile(
    r"\b(list|listing|inventory|index|catalogue|catalog)\b", _re.IGNORECASE)
_EXHAUSTIVE_SCOPE_RE = _re.compile(
    r"\b(all|every|each)\b[^.\n]{0,40}\b(files?|documents?|papers?|records?)\b",
    _re.IGNORECASE)


def _is_exhaustive_task(task: str) -> bool:
    """True when the task asks for an account of the whole workspace:
    a list, inventory, index, or catalogue, or all / every / each of the
    files (or documents, papers, records). Those tasks make every
    root-level file potentially in scope, so discovery for them must be
    exhaustive rather than a sample (issue #6). Category, strategy, and
    value tasks carry none of these cues and keep sampling."""
    task_lowercase = (task or "").lower()
    return bool(_EXHAUSTIVE_WORDS_RE.search(task_lowercase) or _EXHAUSTIVE_SCOPE_RE.search(task_lowercase))


_EXHAUSTIVE_STEERING = (
    "- This task is exhaustive: it asks for a list, an "
    "inventory, or an index, or for all or every file. "
    "Sampling is not acceptable for this task.\n"
    "- Start with a listing step that lists the files at "
    "the workspace root.\n"
    "- Account for every file at the workspace root: each "
    "one must be read, searched, named in the plan, or "
    "explicitly excluded with a reason. No root file may "
    "be left unaccounted for."
)

_SAMPLING_STEERING = (
    "- Sampling is acceptable for this task: it asks what "
    "categories, a strategy, or values should be, not for an "
    "account of every file. Read the files most likely to "
    "decide the plan's categories or values; do not read "
    "everything."
)


def _discovery_steering(task: str) -> str:
    """The task-sensitive sampling rule for the discovery prompt.

    Exhaustive tasks (see _is_exhaustive_task) get a listing step and a
    full accounting of the workspace root; the sampling rule that is
    right for category, strategy, and value discovery is expressly
    withdrawn for them, because a sample silently drops files the task
    asked about (issue #6, Tomas's omitted water-heater manual)."""
    if _is_exhaustive_task(task):
        return _EXHAUSTIVE_STEERING
    return _SAMPLING_STEERING


def build_discovery_prompt(task: str, workspace: str | None = None) -> str:
    digest = workspace_digest(workspace)
    steering = _discovery_steering(task)
    return f"""You are planning a read-only discovery pass. The task below
cannot be planned well yet: the planner first needs facts about the
workspace. Write a short Gherkin plan, 1 to 4 steps, whose steps only
LOOK at things. Discovery changes nothing.

Format:
Feature: <short title>
  Scenario: Step 1 - <short name>
    When <one concrete look: list, read, or search>
    Then <what will be known after this step>

Available discovery tools: {", ".join(READ_ONLY_TOOLS)}.
Rules:
- Only list, read, and search. No writes, no moves, no commands.
- Each step learns something the real plan needs: what files exist,
  what the important ones contain, how things are structured.
{steering}
- Return ONLY the Gherkin, no other text.
{digest}Task: {task}"""


def request_discovery_plan(task: str, config: Config, chat_fn
                           ) -> tuple[str | None, list[PlanStep] | str]:
    """Ask the model for a read-only discovery plan; one retry on a
    structural rejection, mirroring request_plan's shape (no lint:
    discovery steps make no commitments worth linting)."""
    messages = [{"role": "user",
                 "content": build_discovery_prompt(task,
                                                    workspace=config.workspace)}]
    last_problem = "no response"
    for attempt in (1, 2):
        try:
            response_message = chat_fn(messages, None)
        except Exception as error:
            return None, f"discovery planner call failed: {error}"
        content = (response_message.get("content") or "").strip()
        plan, parse_error = gherkin.parse_feature(content)
        if plan is not None:
            return content, to_plan_steps(plan)
        last_problem = parse_error
        messages.append({"role": "assistant", "content": content})
        messages.append({"role": "user", "content":
                         _parse_feedback(parse_error, content)})
    return None, last_problem


def _root_workspace_files(workspace: str | None) -> list[str]:
    """Sorted names of the regular files at the workspace root.

    Root only, matching the digest's view of the workspace: folders and
    their contents are not files the discovery pass was expected to read,
    and `.harness/` bookkeeping is never a workspace file."""
    from pathlib import Path
    if not workspace:
        return []
    try:
        entries = sorted(Path(workspace).iterdir(), key=lambda path_entry: path_entry.name)
    except OSError:
        return []
    return [path_entry.name for path_entry in entries if path_entry.name != ".harness" and path_entry.is_file()]


def unexamined_workspace_files(workspace: str | None, discovery_steps=None,
                               findings: str = "") -> list[str]:
    """Root workspace files the discovery pass never accounted for.

    Deterministic reconciliation at the Tier 2 handoff (issue #6): a
    file counts as accounted for when its name appears in the discovery
    plan (any step's instruction, done_when, given, title, or intent)
    or in the discovery records handed over as findings. That one test
    covers every way a file can be accounted for: listed (a list_dir
    record names it), read, searched, named in the plan, or explicitly
    excluded (an exclusion still names the file). A file named nowhere
    was not examined, whatever the plan claims about "all" files."""
    texts = [findings or ""]
    for step in discovery_steps or []:
        texts.append("\n".join([step.instruction or "", step.done_when or "",
                                step.given or "", step.title or "", step.intent or ""]))
    accounted_text = "\n".join(texts)
    return [file_name for file_name in _root_workspace_files(workspace)
            if file_name not in accounted_text]


def discovery_findings_text(workspace: str, run_id: str, step_count: int,
                            total_cap: int = 8000,
                            per_step_cap: int = 2500,
                            discovery_steps=None) -> str:
    """The findings handed to the replan: excerpts of the discovery run's
    stored step records (tool results verbatim, where the facts live),
    bounded so discovery cannot become context-stuffing. Truncation is
    announced with a pointer to the full record.

    The excerpts are followed by a deterministic unexamined-files
    section (issue #6): the root workspace files compared against the
    FULL records and the discovery plan, so excerpt truncation cannot
    hide a file, and a file discovery never touched is named as
    unexamined instead of silently absent from the handoff."""
    from pathlib import Path
    if not run_id:
        return ""
    findings_parts: list[str] = []
    full_records: list[str] = []
    used_characters = 0
    for step_number in range(1, step_count + 1):
        relative_path = f".harness/runs/{run_id}/step-{step_number:02d}.md"
        record_path = Path(workspace) / relative_path
        if not record_path.is_file():
            continue
        record_text = record_path.read_text(errors="replace")
        full_records.append(record_text)
        if len(record_text) > per_step_cap:
            record_text = record_text[:per_step_cap] + f"\n[truncated; full record: {relative_path}]"
        if used_characters + len(record_text) > total_cap:
            record_text = record_text[:max(0, total_cap - used_characters)]
        if record_text:
            findings_parts.append(record_text)
            used_characters += len(record_text)
        if used_characters >= total_cap:
            break
    body = "\n\n".join(findings_parts)
    if _root_workspace_files(workspace):
        missing_files = unexamined_workspace_files(
            workspace, discovery_steps=discovery_steps,
            findings="\n".join(full_records))
        if missing_files:
            section = ("Unexamined workspace files (the harness compared "
                       "the workspace root against the discovery plan and "
                       "records; these files were not listed, read, "
                       "searched, named, or explicitly excluded during "
                       "discovery, so nothing is known about them):\n"
                       + "\n".join(f"- {name}" for name in missing_files))
        else:
            section = ("Unexamined workspace files: none. Every file at "
                       "the workspace root was accounted for by the "
                       "discovery plan or its records.")
        body = (body + "\n\n" + section) if body else section
    return body


def plan_with_discovery(task: str, config: Config, chat_fn=None, on_event=None
                        ) -> tuple[str | None, list[PlanStep] | str, dict]:
    """Tier 2: discovery pass, then the real plan written with the
    findings in hand. Returns (gherkin_text, steps, info) or
    (None, error, info); info carries the discovery run id, its status,
    and the findings text. A failed discovery execution still replans
    with whatever findings completed steps produced."""
    chat_fn, jail, registry = _chat_and_tools(config, chat_fn)
    discovery_text, discovery_steps = request_discovery_plan(task, config, chat_fn)
    if discovery_text is None:
        return None, discovery_steps, {"discovery_run_id": "",
                                       "discovery_status": "plan_failed",
                                       "findings": ""}
    discovery_run = execute_plan(task, discovery_steps, config, chat_fn=chat_fn,
                                 on_event=on_event, only_tools=list(READ_ONLY_TOOLS))
    findings = discovery_findings_text(config.workspace, discovery_run.run_id, len(discovery_steps),
                                       discovery_steps=discovery_steps)
    text, steps = request_plan(task, list(registry), chat_fn, config,
                               discovery=findings)
    info = {"discovery_run_id": discovery_run.run_id,
            "discovery_status": discovery_run.status,
            "findings": findings,
            "discovery_steps": discovery_steps}
    return text, steps, info


@dataclass
class PlannedRun:
    status: str  # done | done_on_trust | plan_failed | step_failed | model_error
    answer: str
    steps: list[PlanStep] = field(default_factory=list)
    run_id: str = ""  # .harness/runs/<run_id>/ for this run (v0.8.1)
    trust_only: list = field(default_factory=list)  # [{"step": id,
        # "deliverables": [names]}] for steps whose deliverable rests
        # entirely on attested checks (issue #37)

    def plan_dict(self) -> dict:
        return {"run_id": self.run_id,
                "trust_only": self.trust_only,
                "steps": [
            {"id": plan_step.id, "instruction": plan_step.instruction, "done_when": plan_step.done_when,
             "intent": plan_step.intent, "budget": plan_step.budget,
             "status": plan_step.status, "result": plan_step.result[:500],
             "verify": plan_step.verify, "lint": plan_step.lint,
             "verify_waived": plan_step.verify_waived,
             "done_via_world_state": plan_step.done_via_world_state,
             "output_path": plan_step.output_path} for plan_step in self.steps]}


# The plan frame (v0.8.1): every step prompt opens by explaining the
# situation the executor is in. v0.6 scoped the prompt to the current step
# only, fearing the model would freelance across steps; three field
# failures (a lost file list, an evaporated deliverable, an ungrounded
# write step) were all starvation instead. So the executor now sees the
# whole plan, its place in it, and where the data lives — while the
# operative instruction stays scoped and last.
FRAME_TEXT = (
    "How this works: this task is a multi-step plan, carried out one step "
    "at a time in separate runs like this one. Steps marked [done] are "
    "finished: do not redo them. Their full outputs, tool results "
    "included, are stored in the files named beside them, and the short "
    "summaries lose details. Steps marked [pending] run after you, in "
    "their own runs: do not do them, but they will read your stored "
    "output, so finish your step completely. If your step's work is based "
    "on an earlier step's output, read that step's file first with "
    "read_file instead of working from the summary. Your own step's "
    "output file is different: the harness writes it after you finish, "
    "it does not exist yet, and it is not a source. Do not try to read it."
)


def _format_outline_step_result(step: PlanStep) -> str:
    body = step.result or ""
    if body.strip().upper().startswith("DONE:"):
        body = body.strip()[5:].strip()
    if body:
        return f"    Result: {step.result[:200]}"
    if step.tool_summary:
        return f"    Result: (no summary given) {step.tool_summary}"
    if step.result:
        return f"    Result: {step.result[:200]}"
    return ""


def _format_outline_step(step: PlanStep, current_step_id: int) -> list[str]:
    lines = []
    if step.id == current_step_id:
        status_marker = "YOUR STEP"
    elif step.status == "done":
        status_marker = "done"
    else:
        status_marker = "pending"

    title = _re.sub(r"^Step\s+\d+\s*[-:]\s*", "", (step.title or "").strip())
    label = title or (step.instruction.split("\n")[0][:100] if step.instruction else "")
    lines.append(f"  Step {step.id} [{status_marker}] {label}")

    if step.id != current_step_id and step.instruction and title:
        lines.append(f"    When: {step.instruction.split(chr(10))[0][:160]}")

    if step.status == "done" and step.id != current_step_id:
        result_line = _format_outline_step_result(step)
        if result_line:
            lines.append(result_line)
        if step.output_path:
            lines.append(f"    Full output: {step.output_path}")
    return lines


def _plan_outline(steps: list[PlanStep], current_step_id: int) -> str:
    """The shape of the whole plan, one line per step, with statuses.

    Done steps are annotated with their (short) result and, when the run
    store is active, the exact file holding their full output."""
    lines = ["The plan:"]
    for plan_step in steps:
        lines.extend(_format_outline_step(plan_step, current_step_id))
    return "\n".join(lines) + "\n"


def _index_describe(chat_fn, name: str, content: str) -> str:
    """One per-file description call for the indexing primitive.

    Returns the model's raw reply, or "" on any failure (the indexing
    build treats an empty description as a decline, and the step
    falls back to its normal executor sub-run)."""
    try:
        response_message = chat_fn([{"role": "user",
                                     "content": _indexing.describe_prompt(name,
                                                                          content)}],
                                   None)
    except Exception:
        return ""
    if not isinstance(response_message, dict):
        return ""
    return (response_message.get("content") or "")


def _graph_contract_block(task: str, step: PlanStep,
                          steps: list[PlanStep]) -> str:
    """The step's node contract from the plan graph, for its prompt.

    Experimental (Config.graph_contracts): the executor is told, in
    field terms, what inputs it may rely on and who produced them, and
    what outputs it must produce, instead of inferring the data flow
    from prose. The graph is computed from the plan text alone (no
    workspace scan), so the contract is the same graph the approval
    display derives from the plan, and it does not drift as steps
    create files. Returns "" when the step has no fields."""
    graph = _graphcheck.build_graph(task, steps)
    graph_node = next((node for node in graph.nodes if node.step_id == step.id), None)
    if graph_node is None or (not graph_node.inputs and not graph_node.outputs):
        return ""
    producers = {edge.field: edge.producer for edge in graph.edges
                 if edge.consumer == step.id}

    def describe(label: str) -> str:
        if label in producers:
            return f'"{label}" (produced by step {producers[label]})'
        if label.startswith("files from step ") \
                or (label.startswith("step ") and label.endswith(" output")):
            return f'"{label}"'
        return f'"{label}" (from the task or the workspace)'

    lines = ["Your place in the plan graph (computed from the plan, "
             "deterministic):"]
    if graph_node.inputs:
        lines.append("  Inputs available to you: "
                     + "; ".join(describe(input_field) for input_field in graph_node.inputs) + ".")
    if graph_node.outputs:
        lines.append("  Outputs this step must produce: "
                     + "; ".join(f'"{output_field}"' for output_field in graph_node.outputs) + ".")
        lines.append("  This step is complete only when every output "
                     "above exists with its content in place, under "
                     "exactly that name. Later steps consume these "
                     "outputs as named.")
    return "\n".join(lines) + "\n"


def _sub_step_names_prior(sub_step, prior_output_paths: list[str]) -> bool:
    import json as _json
    if not (sub_step.tool and sub_step.args):
        return False
    dumped_arguments = _json.dumps(sub_step.args, default=str)
    return any(prior_output_path in dumped_arguments for prior_output_path in prior_output_paths)


def _find_prior_read_index(sub_steps, prior_output_paths: list[str]) -> int | None:
    return next((step_index for step_index, sub_step in enumerate(sub_steps)
                 if _sub_step_names_prior(sub_step, prior_output_paths)), None)


def _unreferenced_priors_feedback(step: PlanStep, prior_output_paths: list[str]) -> str:
    return (f"HARNESS: you are finishing step {step.id} without having read "
            f"the output of the earlier step(s) this plan builds on: "
            f"{', '.join(prior_output_paths)}. Those files are the record of what the "
            f"earlier steps actually produced. Read the file now with "
            f"read_file and redo this step using its real contents. If this "
            f"step genuinely does not depend on that output, answer DONE: "
            f"again and say why in one sentence. Your own step's output "
            f"file ({step.output_path}) is not one of these sources: the "
            f"harness writes it after you finish, it does not exist yet, "
            f"so do not try to read it.")


def _collect_covers_targets_and_items(step: PlanStep, steps: list[PlanStep]) -> tuple[list[str], list[str]]:
    from . import verify as _verify
    steps_by_id = {plan_step.id: plan_step for plan_step in steps}
    covered_items: list[str] = []
    target_names: list[str] = []
    for then_clause in (step.done_when or "").split("\n"):
        then_clause = then_clause.strip()
        if not then_clause or _verify.match_verifier(then_clause) != "covers":
            continue
        step_match = _re.search(r"step\s+(\d+)", then_clause)
        if not step_match:
            continue
        source_step = steps_by_id.get(int(step_match.group(1)))
        if source_step is None:
            continue
        parsed_clause = _verify.parse_then(then_clause)
        if parsed_clause and parsed_clause[1]:
            target_names.append(parsed_clause[1])
        for item in source_step.source_items or []:
            if item not in covered_items:
                covered_items.append(item)
    return target_names, covered_items


def _find_missing_covered_items(covered_items: list[str], steps: list[PlanStep],
                                current_step: PlanStep, sub_steps) -> list[str]:
    import json as _json
    argument_blobs = set()
    for prior_step in steps:
        if prior_step.id < current_step.id:
            argument_blobs.update(prior_step.touched_args or set())
    for sub_step in sub_steps:
        if sub_step.tool and sub_step.args:
            argument_blobs.add(_json.dumps(sub_step.args, default=str))
    return [item for item in covered_items if not any(item in argument_blob for argument_blob in argument_blobs)]


def _missing_covers_feedback(targets: list[str], missing_items: list[str]) -> str:
    claim_prefix = f'"{targets[0]}" covers' if targets else "This step covers"
    return (f"HARNESS: {claim_prefix} the files from an earlier "
            f"listing step, but these files have not been read "
            f"yet in this run: {', '.join(missing_items)}. Work about "
            f"a file must come from its actual contents, not "
            f"from its name in a list. Read each named file now "
            f"with read_file, then redo this step from what "
            f"they contain. If a named file genuinely is not a "
            f"source for this step, answer DONE: again and say "
            f"why in one sentence.")


def _has_work_after_prior_read(sub_steps, read_index: int, prior_output_paths: list[str]) -> bool:
    return any(sub_step.tool and not _sub_step_names_prior(sub_step, prior_output_paths)
               for sub_step in sub_steps[read_index + 1:])


def _unchanged_after_read_feedback(step: PlanStep, prior_output_paths: list[str]) -> str:
    return (f"HARNESS: you read {', '.join(prior_output_paths)} but you have not changed "
            f"anything since reading it. Redo this step now using what the "
            f"file contains: write or edit the deliverable from its real "
            f"contents. If nothing genuinely needs to change, answer DONE: "
            f"again and say why in one sentence. Your own step's output "
            f"file ({step.output_path}) is written by the harness after "
            f"you finish and does not exist yet; it is not a source to "
            f"read.")


def _grounding_gate(step: PlanStep, steps: list[PlanStep], verify_now=None):
    """Build the DONE gate for one step (v0.8.1), or None when the step has
    no completed prior steps with stored outputs.

    Field failure behind it: a step wrote its deliverable without ever
    reading the previous step's stored output, and produced a placeholder.
    The frame advises reading the source; the gate checks it. Any tool call
    whose arguments name a prior step's output file counts as grounded
    (read_file, an exec cat, anything). The gate fires once per sub-run;
    a second DONE is the escape hatch for genuinely independent steps.

    verify_now (the surgical fix, 2026-10-03): stage 2 used to fire blind,
    ordering a redo of a deliverable verification had never examined; in
    the field that redo briefly overwrote a good index with a worse one.
    When verify_now is supplied, stage 2 first runs the step's own Thens
    against the workspace: if they pass on content-bearing checks
    (contains, covers, and kin; a bare exists proves nothing, the
    founding stub passed it), the suspicion is answered and the gate
    stays silent. An attested or failing Then keeps the pushback
    exactly as before.

    Coverage stage (issue #17): when the step carries a covers clause,
    the listing step's source items are compared against the files read
    so far in the run: completed prior steps' recorded tool calls plus
    this sub-run's. Step scope is soft, so an earlier gather step's
    reads count; what matters is that the run has actually opened every
    file the deliverable claims to cover. An unread source file draws a
    pushback naming it. This stage ignores verify_now on purpose: a
    deliverable can mention a file it never read, and mentions are not
    evidence. It shares the gate's existing budget of two pushbacks per
    sub-run (the loop cap is unchanged) and is evaluated before stage 2,
    because "read these files" is the more specific instruction when
    both apply."""
    prior_output_paths = [prior_step.output_path for prior_step in steps
                          if prior_step.id < step.id and prior_step.status == "done" and prior_step.output_path]
    if not prior_output_paths:
        return None

    def gate(sub_steps) -> str | None:
        read_index = _find_prior_read_index(sub_steps, prior_output_paths)
        if read_index is None:
            return _unreferenced_priors_feedback(step, prior_output_paths)

        targets, covered_items = _collect_covers_targets_and_items(step, steps)
        if covered_items:
            missing_items = _find_missing_covered_items(covered_items, steps, step, sub_steps)
            if missing_items:
                return _missing_covers_feedback(targets, missing_items)

        if _has_work_after_prior_read(sub_steps, read_index, prior_output_paths):
            return None

        if verify_now is not None:
            try:
                if verify_now():
                    return None
            except Exception:
                pass

        return _unchanged_after_read_feedback(step, prior_output_paths)

    return gate


def _then_named_outputs(step: PlanStep) -> list[str]:
    """Files the step's Thens name as their subject that the step's
    When never names (issue #48): the deliverable exists only in the
    clause that checks the work, not in the instruction that does
    it, so the executing step has no stated place to write. The
    executor prompt and the blocked-write feedback both name these
    files explicitly, so the gap is bridged even when a plan that
    carries it reaches execution. Absence clauses name no output:
    the file they name is meant to be gone, not written. Neither
    does a preservation clause: the file it names is meant to be
    left exactly as it was. A file a step promises to leave alone
    or to remove is a constraint on the step, never its output,
    and that holds whichever clause states it: the constraint
    subjects are collected across the whole step first, so an
    existence clause naming the same file ("source.txt" exists
    beside "source.txt" remains unchanged) cannot resurrect it as
    a write target. "Named in the When" is judged on the
    When's extracted file mentions and its mv/cp destinations, not
    on substring matching: a destination like archive/<name> is
    named by the move even though the joined path never appears
    literally, and an output whose name sits inside a longer source
    name is not named at all."""
    from . import planlint as _planlint
    from . import verify as _verify
    named_in_when = _planlint._mentions(step.instruction or "") \
        | _planlint._move_destinations(step.instruction or "")
    clauses = [clause.strip()
               for clause in (step.done_when or "").split("\n")]
    clauses = [clause for clause in clauses if clause]
    constrained_subjects: set[str] = set()
    for clause in clauses:
        if _verify.absence_intent(clause) or _verify.preservation_intent(clause):
            subject_file = _verify.then_subject_file(clause)
            if subject_file:
                constrained_subjects.add(subject_file)
    outputs: list[str] = []
    for clause in clauses:
        subject_file = _verify.then_subject_file(clause)
        if not subject_file:
            continue
        if subject_file in named_in_when or subject_file in constrained_subjects or subject_file in outputs:
            continue
        outputs.append(subject_file)
    return outputs


_CONTENT_BEARING_VERIFIERS = frozenset({
    "contains", "contains_exactly", "indexes", "accounts_for", "has_lines"
})


def _is_content_bearing_pass(verification_result) -> bool:
    if not verification_result.checks:
        return False
    if any(not check.ok or check.verifier == "attest" for check in verification_result.checks):
        return False
    return any(check.verifier in _CONTENT_BEARING_VERIFIERS for check in verification_result.checks)


def _record_attempt_metrics(step: PlanStep, attempts: list[dict]):
    if not attempts:
        return
    import json as _json
    for attempt in attempts:
        for sub_step in attempt.get("sub_steps") or []:
            if sub_step.tool and sub_step.args:
                step.touched_args.add(_json.dumps(sub_step.args, default=str))

    last_sub_steps = attempts[-1].get("sub_steps") or []
    tool_counts: dict[str, int] = {}
    total_output_lines = 0
    for sub_step in last_sub_steps:
        if sub_step.tool:
            tool_counts[sub_step.tool] = tool_counts.get(sub_step.tool, 0) + 1
            total_output_lines += len((sub_step.result or "").splitlines())
    if tool_counts:
        step.tool_summary = (
            ", ".join(f"{tool_name} x{count}" for tool_name, count in tool_counts.items())
            + f"; {total_output_lines} lines of tool output")


def _write_step_to_store(run_store, step: PlanStep, attempts: list[dict], status: str):
    if run_store is None:
        return
    try:
        run_store.write_step(
            step_id=step.id, instruction=step.instruction,
            given=step.given, done_when=step.done_when,
            answer=step.result, sub_steps=[], status=status,
            verify_checks=step.verify, verify_waived=step.verify_waived,
            attempts=attempts or None)
    except Exception:
        pass


def _run_step_verified(task: str, step: PlanStep, steps: list[PlanStep],
                       config: Config, chat_fn, jail, emit_event,
                       run_store=None, only_tools=None) -> bool:
    """Run one plan step; after the sub-run reports DONE, verify the Then
    clauses in code before advancing. A failed verification warns and retries
    the step once (bounded by config.verify_retries) with the exact failure as
    feedback. If verification still fails, the failure is downgraded to a
    warning: the step is accepted and the plan continues, because the verifier
    checks plan *adherence* while the task-level checker judges *correctness* —
    a wrong plan must not block right work. Verification itself costs zero
    model calls. Sub-run failures (model errors, limits) still hard-fail.

    v0.8.1 (issue #2): when a RunStore is present, every attempt's full
    record (tool calls with verbatim results, answer, verification) is
    written to .harness/runs/<run>/step-NN.md.

    v0.8.1 (plan frame): the prompt leads with FRAME_TEXT and the plan
    outline (every step, its status, done steps' output files), then
    repeats the current step as the operative block at the end. The
    outline replaces both the old 250-char context list and the store's
    separate path block: summaries and paths now ride with the step they
    describe."""
    from . import verify as _verify
    verify_context = _verify.VerifyContext(
        jail=jail, step_id=step.id, task=task,
        prior_items={prior_step.id: list(prior_step.source_items) for prior_step in steps if prior_step.source_items},
        intent=step.intent or "")
    feedback_message = ""
    last_verification_result = None
    attempts: list[dict] = []
    total_steps = len(steps)
    then_clauses = [clause.strip() for clause in step.done_when.split("\n") if clause.strip()]

    def verify_now() -> bool:
        """True only when the step's Thens machine-check as passing right
        now AND at least one passing check is content-bearing.

        Attested clauses are not evidence, and neither is a bare
        existence pass: the stub that founded stage 2 satisfied
        `"file" exists` while promising its content "to be filled in".
        The checks that answer stage 2's suspicion measure content:
        contains, contains_exactly, indexes (against the source items),
        has_lines. See _grounding_gate."""
        if not then_clauses:
            return False
        verification_result = _verify.verify_step(then_clauses, verify_context)
        return _is_content_bearing_pass(verification_result)

    gate = _grounding_gate(step, steps, verify_now=verify_now)
    # Experimental: the step's plan-graph node contract rides in its
    # prompt when enabled. Computed once here; the graph derives from
    # plan text, so the verification retry sees the same contract.
    contract = (_graph_contract_block(task, step, steps)
                if getattr(config, "graph_contracts", False) else "")
    # Issue #14: this step's turn budget is computed from its planned
    # operations now that prior steps' source items are known, and the
    # same budget governs the verification retry.
    step.budget = step_budget(step, steps, config)
    sub_run_config = replace(config, max_steps=step.budget)

    def persist(status: str):
        _record_attempt_metrics(step, attempts)
        _write_step_to_store(run_store, step, attempts, status)

    # Indexing primitive: when the step carries an `indexes` clause,
    # the harness tries to build the target itself (enumerate the
    # source step's items, one description call per file, write the
    # index) before any executor sub-run. Success skips the sub-run
    # entirely; a declined build (unreadable file, empty description)
    # or a failed verification of the step's other Thens falls
    # through to the normal loop below, with any verification failure
    # as its feedback. Placed here, after persist() exists.
    clause = _indexing.clause_for(step)
    if clause is not None:
        target_path, source_step_id = clause
        source_step = next((candidate_step for candidate_step in steps if candidate_step.id == source_step_id), None)
        source_items = list(source_step.source_items) if source_step is not None else []
        records = None
        if source_items:
            records = _indexing.build_index(
                jail.root, source_items,
                lambda name, content: _index_describe(chat_fn, name,
                                                      content))
        if records is not None:
            destination_path = jail.resolve(target_path)
            destination_path.parent.mkdir(parents=True, exist_ok=True)
            destination_path.write_text(_indexing.render_index(records))
            step.result = (f"DONE: indexed {len(records)} files into "
                           f"{target_path} with the harness indexing "
                           f"primitive.")
            step.source_items = list(source_items)
            import json as _json
            for name in source_items:
                step.touched_args.add(_json.dumps({"path": name}))
            step.touched_args.add(_json.dumps({"path": target_path}))
            verification_result = _verify.verify_step(then_clauses, verify_context)
            step.verify = [{"then": check.then, "verifier": check.verifier,
                            "ok": check.ok, "detail": check.detail}
                           for check in verification_result.checks]
            attempts.append({"sub_steps": [], "answer": step.result,
                             "verify_checks": list(step.verify)})
            emit_event("step_verify", step, verification_result)
            if verification_result.ok:
                step.status = "done"
                persist("done")
                emit_event("step_done", step)
                return True
            feedback_message = "; ".join(check.detail for check in verification_result.checks
                                         if not check.ok)

    # Issue #48: files the step's Thens name that its When does not.
    # The prompt presents them as the step's output file, and a
    # refused write's feedback names them, so a plan that reaches
    # execution with the gap still gives the step a way to recover.
    output_files = _then_named_outputs(step)
    refusal_hint = None
    if output_files:
        listed_outputs = ", ".join(f"'{output_file}'" for output_file in output_files)
        refusal_hint = (f"HARNESS: this step's output file is "
                        f"{listed_outputs} (named in the step's Then). Write "
                        f"to that file instead.")

    for _ in range(1 + config.verify_retries):
        prompt = (f"Overall goal: {task}\n"
                  f"You are executing step {step.id} of {total_steps}. "
                  f"Do ONLY this step, nothing else.\n\n")
        prompt += FRAME_TEXT + "\n\n"
        prompt += _plan_outline(steps, step.id) + "\n"
        if step.given:
            prompt += f"Starting state: {step.given}\n"
        if step.intent:
            prompt += f"Intent of this step: {step.intent}\n"
        prompt += (f"Step: {step.instruction}\n"
                   f"This step is done when: {step.done_when or 'its instruction is complete'}\n")
        if output_files:
            listed_outputs = ", ".join(f"'{output_file}'" for output_file in output_files)
            prompt += (f"Output file for this step: {listed_outputs} (named "
                       f"in this step's Then). Write this step's "
                       f"result to that file.\n")
        if contract:
            prompt += contract
        if run_store is not None and step.output_path:
            # Wording matters (field, 2026-10-03): "Your full output will be
            # stored at..." read as a write instruction and a step wrote its
            # deliverable into its own record file. Say who does the storing.
            prompt += (f"The harness will store your full output at "
                       f"{step.output_path} for later steps. Do not write to "
                       f"that file yourself, and do not try to read it: it "
                       f"does not exist yet. The harness writes it after you "
                       f"finish. The files you can read are the earlier "
                       f"steps' outputs, named beside the [done] steps "
                       f"above.\n")
        if feedback_message:
            prompt += (f"Your previous attempt failed verification: {feedback_message} "
                       f"Fix exactly this and try again.\n")
        prompt += 'When this step is complete, answer starting with "DONE:".'

        try:
            run_result = loop.run(prompt, sub_run_config, chat_fn=chat_fn,
                                  on_step=lambda sub_step: emit_event("step_sub", step, sub_step),
                                  done_gate=gate, only_tools=only_tools,
                                  refusal_hint=refusal_hint)
        except Exception as error:
            step.status = "failed"
            step.result = f"harness error: {error}"
            attempts.append({"sub_steps": [], "answer": step.result,
                             "verify_checks": []})
            persist("failed")
            emit_event("step_failed", step)
            return False
        step.result = (run_result.answer or "")[:800]
        if run_result.status != "done":
            # World-state override (Ansel's ruling, 2026-10-04, the
            # Tomas protocol question): a sub-run that ended at the
            # turn limit with a real final answer missed the DONE
            # protocol, but the protocol token adds no information
            # about the world. Judge the step on the world instead:
            # run verification exactly as after DONE, and if every
            # clause passes, the step is done, recorded as such.
            # Attested clauses count when they pass under the current
            # rules (existence floor included): a DONE ending with
            # the same verification is already accepted, so the same
            # verification after a protocol miss stands identically.
            # A dead run gets no override: consecutive tool errors
            # (error_limit), a model error, or a final summary call
            # that itself failed leaves nothing finished to judge.
            verification_result = None
            answer = (run_result.answer or "").strip()
            if (run_result.status == "max_steps" and answer
                    and not answer.startswith("(summary call failed")
                    and not answer.startswith("(no summary)")):
                step.source_items = _verify.collect_step_items(
                    list(run_result.steps))
                verification_result = _verify.verify_step(then_clauses, verify_context)
                step.verify = [{"then": check.then, "verifier": check.verifier,
                                "ok": check.ok, "detail": check.detail}
                               for check in verification_result.checks]
                emit_event("step_verify", step, verification_result)
            attempts.append({"sub_steps": list(run_result.steps),
                             "answer": run_result.answer or "",
                             "verify_checks": list(step.verify)
                             if verification_result is not None else []})
            if verification_result is not None and verification_result.ok:
                step.status = "done"
                step.done_via_world_state = True
                persist("done")
                emit_event("step_done_world_state", step, verification_result)
                emit_event("step_done", step)
                return True
            step.status = "failed"
            persist("failed")
            emit_event("step_failed", step)
            return False
        step.source_items = _verify.collect_step_items(list(run_result.steps))
        verification_result = _verify.verify_step(then_clauses, verify_context)
        step.verify = [{"then": check.then, "verifier": check.verifier,
                        "ok": check.ok, "detail": check.detail} for check in verification_result.checks]
        attempts.append({"sub_steps": list(run_result.steps), "answer": run_result.answer or "",
                         "verify_checks": list(step.verify)})
        emit_event("step_verify", step, verification_result)
        if verification_result.ok:
            step.status = "done"
            persist("done")
            emit_event("step_done", step)
            return True
        last_verification_result = verification_result
        feedback_message = "; ".join(check.detail for check in verification_result.checks if not check.ok)
    # Retries exhausted: downgrade to a warning and continue. The step ran;
    # only its acceptance criteria are disputed (often a wrong plan, not
    # wrong work) — the task-level checker remains the backstop.
    step.status = "done"
    step.verify_waived = True
    persist("done")
    emit_event("step_verify_waived", step, last_verification_result)
    emit_event("step_done", step)
    return True


def execute_plan(task: str, steps: list[PlanStep], config: Config, chat_fn=None,
                 on_event=None, only_tools=None) -> PlannedRun:
    """Execute an approved plan, one scoped step at a time. on_event(kind, *args)
    reports ("step_start", step), ("step_sub", step, sub), ("step_verify", step,
    StepVerify), ("step_verify_waived", step, StepVerify), ("step_done", step),
    ("step_failed", step). A waived verification still counts the step done.
    Issue #22 adds ("deliverables_missing", [paths]): after all steps, the
    promised-deliverable audit can still fail the run (see below). The
    world-state override adds ("step_done_world_state", step, StepVerify):
    a sub-run that missed DONE but whose verification passes counts the
    step done, recorded on the step as done_via_world_state."""
    chat_fn, jail, _registry = _chat_and_tools(config, chat_fn)

    def emit_event(kind, *args):
        if on_event:
            on_event(kind, *args)

    run_store = None
    try:
        from .runstore import RunStore
        run_store = RunStore(config.workspace)
        for plan_step in steps:
            plan_step.output_path = run_store.rel_path(plan_step.id)
    except Exception:
        run_store = None  # record-keeping must never block execution

    for step in steps:
        emit_event("step_start", step)
        if not _run_step_verified(task, step, steps,
                                  config, chat_fn, jail, emit_event, run_store=run_store,
                                  only_tools=only_tools):
            break

    status = "done" if steps and all(plan_step.status == "done" for plan_step in steps) else "step_failed"

    # Issue #22: the deliverable audit. Steps can finish with their
    # verification waived, and a waived step still counts done, so the
    # per-step machinery alone cannot promise the user a deliverable.
    # Before the run may report done, every file the plan's Thens
    # promised must exist. This judges existence only, neither plan
    # adherence nor content quality, so it does not reopen the
    # warn-downgrade decision: a warning can excuse a wrong check,
    # never a missing delivery.
    missing_deliverables: list[str] = []
    if status == "done":
        from . import verify as _verify
        missing_deliverables = _verify.missing_deliverables(steps, jail)
        if missing_deliverables:
            status = "step_failed"
            emit_event("deliverables_missing", missing_deliverables)
    if missing_deliverables:
        answer = ("The plan's steps finished, but this run is a "
                  "failure: promised deliverable(s) were never "
                  "created: " + ", ".join(missing_deliverables) + ". A run only "
                  "reports done when every file the plan promises "
                  "exists.")
        return PlannedRun(status, answer, steps,
                          run_id=run_store.run_id if run_store is not None else "")

    # Issue #37: a run that finished with a deliverable resting
    # entirely on attested checks must not report plain done. The
    # trust-only steps are named in the final status, while the run
    # still counts as completed (see run.py's exit mapping).
    trust_only_entries: list = []
    if status == "done":
        from . import verify as _verify
        trust_only_entries = _verify.trust_only_steps(steps)
        if trust_only_entries:
            status = "done_on_trust"

    summary = (f"Overall goal: {task}\nPlan execution ended: {status}.\n"
               + "\n".join(f"Step {plan_step.id} [{plan_step.status}]: {plan_step.instruction}"
                            + (f" -> {plan_step.result[:200]}" if plan_step.result else "")
                            for plan_step in steps))
    if trust_only_entries:
        names = "; ".join(
            f"step {entry['step']} ({', '.join(entry['deliverables'])})"
            for entry in trust_only_entries)
        summary += (f"\nSteps finished on trust: {names}. Their Then "
                    f"clauses were attested, not machine-checked, so "
                    f"their deliverables' contents were never verified. "
                    f"Say plainly which steps finished on trust.")
    summary += ('\nSummarize the outcome in 2-4 sentences, starting with "DONE:".')
    try:
        response_message = chat_fn([{"role": "user", "content": summary}], None)
        answer = (response_message.get("content") or "").strip() or "(no summary)"
    except Exception as error:
        answer = f"(summary call failed: {error})"
    return PlannedRun(status, answer, steps,
                      run_id=run_store.run_id if run_store is not None else "",
                      trust_only=trust_only_entries)


def run_planned(task: str, config: Config, chat_fn=None, on_event=None) -> PlannedRun:
    """Auto mode: propose a plan and execute it in one go. The eval uses this."""
    gherkin_text, planned_steps = propose(task, config, chat_fn)
    if gherkin_text is None:
        return PlannedRun("plan_failed", f"Could not get a valid plan: {planned_steps}", [])
    if on_event:
        on_event("plan", planned_steps)
    return execute_plan(task, planned_steps, config, chat_fn, on_event)
