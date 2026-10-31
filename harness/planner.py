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
        entries = sorted(Path(workspace).iterdir(), key=lambda p: p.name)
    except OSError:
        return ""
    visible = [p for p in entries if p.name != ".harness"]
    if not visible:
        body = "(the workspace folder is currently empty)"
    else:
        lines = [_format_digest_entry(p) for p in visible[:limit]]
        body = "\n".join(lines)
        if len(visible) > limit:
            body += f"\n- ... and {len(visible) - limit} more entries"
    return ("\nWorkspace contents right now (listed by the harness itself):\n"
            + body
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
    found = _sug.for_task(task)
    if not found:
        return ""
    return ("\nKnown idioms that apply to this task — use them instead of "
            "hardcoding:\n" + _sug.render(found) + "\n")


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
    for i, p in enumerate(plan.steps, 1):
        steps.append(PlanStep(
            id=i,
            instruction="\n".join(p.when)[:500],
            # done_when is the step's verification contract: badges, lint,
            # executor prompts, and verification all consume it. It is NOT
            # length-capped. A 300-char cap silently truncated combined
            # Then clauses mid-clause (issue #7: a six-clause step lost its
            # tail), so every clause is preserved exactly.
            done_when="\n".join(p.then),
            given="\n".join(p.given)[:300],
            title=(p.title or "")[:120],
            intent=(p.intent or "")[:300],
        ))
    for s in steps:
        s.budget = step_budget(s, steps)
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
    lo, hi = min(start, end), max(start, end)
    if hi - lo <= 64:
        return set(range(lo, hi + 1))
    return {start, end}


def _parse_step_segment_tokens(tokens: list[str]) -> set[int]:
    refs: set[int] = set()
    prev: int | None = None
    connector: str | None = None
    for tok in tokens:
        if not tok.isdigit():
            connector = tok.lower()
            continue
        n = int(tok)
        if prev is not None and connector in _RANGE_CONNECTORS:
            refs.update(_expand_step_range(prev, n))
        else:
            refs.add(n)
            if prev is not None:
                refs.add(prev)
        prev, connector = n, None
    if prev is not None:
        refs.add(prev)
    return refs


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
    refs = {int(m) for m in _re.findall(r"\bstep\s+(\d+)", text,
                                        flags=_re.IGNORECASE)}
    segment = _re.compile(
        r"\bsteps?\s+(\d+(?:(?:\s*(?:,|and|through|to|[-–—])\s*)+\d+)+)",
        _re.IGNORECASE)
    token = _re.compile(r"\d+|through|to|and|,|[-–—]", _re.IGNORECASE)
    for m in segment.finditer(text):
        tokens = token.findall(m.group(1))
        refs.update(_parse_step_segment_tokens(tokens))
    return refs


def _step_quoted_operands(text: str) -> set[str]:
    import re as _re
    return {t for t in _re.findall(r'"([^"\n]+)"', text)
            if t.strip() and not any(c.isspace() for c in t)}


def _prior_step_cost(prior: PlanStep) -> int:
    return 1 + len(prior.source_items or [])


def step_budget(step: PlanStep, steps: list[PlanStep], cfg=None) -> int:
    """Effective turn budget for one plan step, from its planned work."""
    base = getattr(cfg, "step_max_steps", 6) if cfg is not None else 6
    ceiling = (getattr(cfg, "step_budget_ceiling", 24)
               if cfg is not None else 24)
    text = "\n".join([step.given or "", step.intent or "",
                      step.instruction or "", step.done_when or ""])
    ops = len(_step_quoted_operands(text))
    by_id = {s.id: s for s in steps}
    for ref in _step_refs(text):
        prior = by_id.get(ref)
        if prior is not None and prior.id < step.id:
            ops += _prior_step_cost(prior)
    return max(base, min(ceiling, ops + _BUDGET_SLACK))


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


def request_plan(task: str, tool_names: list[str], chat_fn, cfg: Config,
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
                                                 workspace=cfg.workspace,
                                                 discovery=discovery)}]
    last_problem = "no response"
    for attempt in (1, 2):
        try:
            msg = chat_fn(messages, None)
        except Exception as e:
            return None, f"planner call failed: {e}"
        content = (msg.get("content") or "").strip()
        plan, err = gherkin.parse_feature(content)
        if plan is None:
            feedback = _parse_feedback(err, content, discovery)
            last_problem = err
        else:
            steps = to_plan_steps(plan)
            findings = _planlint.lint_plan(
                task, steps,
                workspace_files=_root_workspace_files(cfg.workspace))
            by_step: dict[int, list] = {}
            for f in findings:
                by_step.setdefault(f.step_id, []).append(f.as_dict())
            for s in steps:
                s.lint = by_step.get(s.id, [])
            errors = [f for f in findings if f.level == "error"]
            if not errors or attempt == 2:
                return content, steps
            feedback = ("That plan has design flaws: "
                        + "; ".join(f.detail for f in errors)
                        + ". Return ONLY the corrected Gherkin, "
                        "no other text.")
            last_problem = "; ".join(f.detail for f in errors)
        messages.append({"role": "assistant", "content": content})
        messages.append({"role": "user", "content": feedback})
    return None, last_problem


def _default_chat_fn(cfg: Config):
    from . import client as _client

    def chat_fn(messages, tool_defs):
        tools = _client.to_ollama_tools(tool_defs) if tool_defs else None
        return _client.chat(
            cfg.endpoint, cfg.model, messages,
            tools=tools,
            temperature=cfg.temperature, top_p=cfg.top_p,
            timeout=cfg.request_timeout)

    return chat_fn


def _chat_and_tools(cfg: Config, chat_fn=None):
    from . import tools as _tools
    effective_chat_fn = chat_fn or _default_chat_fn(cfg)
    jail = _tools.Jail(cfg.workspace)
    registry = _tools.make_tools(jail, cfg.blocked_substrings, cfg.exec_timeout,
                                 cfg.max_output_chars)
    return effective_chat_fn, jail, registry


def propose(task: str, cfg: Config, chat_fn=None
            ) -> tuple[str | None, list[PlanStep] | str]:
    """Propose a Gherkin plan without executing it. Returns (gherkin_text,
    steps) or (None, error_string)."""
    chat_fn, jail, registry = _chat_and_tools(cfg, chat_fn)
    return request_plan(task, list(registry), chat_fn, cfg)


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
    low = (task or "").lower()
    return bool(_EXHAUSTIVE_WORDS_RE.search(low) or _EXHAUSTIVE_SCOPE_RE.search(low))


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


def request_discovery_plan(task: str, cfg: Config, chat_fn
                           ) -> tuple[str | None, list[PlanStep] | str]:
    """Ask the model for a read-only discovery plan; one retry on a
    structural rejection, mirroring request_plan's shape (no lint:
    discovery steps make no commitments worth linting)."""
    messages = [{"role": "user",
                 "content": build_discovery_prompt(task,
                                                   workspace=cfg.workspace)}]
    last_problem = "no response"
    for attempt in (1, 2):
        try:
            msg = chat_fn(messages, None)
        except Exception as e:
            return None, f"discovery planner call failed: {e}"
        content = (msg.get("content") or "").strip()
        plan, err = gherkin.parse_feature(content)
        if plan is not None:
            return content, to_plan_steps(plan)
        last_problem = err
        messages.append({"role": "assistant", "content": content})
        messages.append({"role": "user", "content":
                         _parse_feedback(err, content)})
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
        entries = sorted(Path(workspace).iterdir(), key=lambda p: p.name)
    except OSError:
        return []
    return [p.name for p in entries if p.name != ".harness" and p.is_file()]


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
    for s in discovery_steps or []:
        texts.append("\n".join([s.instruction or "", s.done_when or "",
                                s.given or "", s.title or "", s.intent or ""]))
    accounted = "\n".join(texts)
    return [name for name in _root_workspace_files(workspace)
            if name not in accounted]


def discovery_findings_text(workspace: str, run_id: str, n_steps: int,
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
    parts: list[str] = []
    full_records: list[str] = []
    used = 0
    for i in range(1, n_steps + 1):
        rel = f".harness/runs/{run_id}/step-{i:02d}.md"
        p = Path(workspace) / rel
        if not p.is_file():
            continue
        text = p.read_text(errors="replace")
        full_records.append(text)
        if len(text) > per_step_cap:
            text = text[:per_step_cap] + f"\n[truncated; full record: {rel}]"
        if used + len(text) > total_cap:
            text = text[:max(0, total_cap - used)]
        if text:
            parts.append(text)
            used += len(text)
        if used >= total_cap:
            break
    body = "\n\n".join(parts)
    if _root_workspace_files(workspace):
        missing = unexamined_workspace_files(
            workspace, discovery_steps=discovery_steps,
            findings="\n".join(full_records))
        if missing:
            section = ("Unexamined workspace files (the harness compared "
                       "the workspace root against the discovery plan and "
                       "records; these files were not listed, read, "
                       "searched, named, or explicitly excluded during "
                       "discovery, so nothing is known about them):\n"
                       + "\n".join(f"- {name}" for name in missing))
        else:
            section = ("Unexamined workspace files: none. Every file at "
                       "the workspace root was accounted for by the "
                       "discovery plan or its records.")
        body = (body + "\n\n" + section) if body else section
    return body


def plan_with_discovery(task: str, cfg: Config, chat_fn=None, on_event=None
                        ) -> tuple[str | None, list[PlanStep] | str, dict]:
    """Tier 2: discovery pass, then the real plan written with the
    findings in hand. Returns (gherkin_text, steps, info) or
    (None, error, info); info carries the discovery run id, its status,
    and the findings text. A failed discovery execution still replans
    with whatever findings completed steps produced."""
    chat_fn, jail, registry = _chat_and_tools(cfg, chat_fn)
    dtext, dsteps = request_discovery_plan(task, cfg, chat_fn)
    if dtext is None:
        return None, dsteps, {"discovery_run_id": "",
                              "discovery_status": "plan_failed",
                              "findings": ""}
    run = execute_plan(task, dsteps, cfg, chat_fn=chat_fn,
                       on_event=on_event, only_tools=list(READ_ONLY_TOOLS))
    findings = discovery_findings_text(cfg.workspace, run.run_id, len(dsteps),
                                       discovery_steps=dsteps)
    text, steps = request_plan(task, list(registry), chat_fn, cfg,
                               discovery=findings)
    info = {"discovery_run_id": run.run_id,
            "discovery_status": run.status,
            "findings": findings,
            "discovery_steps": dsteps}
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
            {"id": s.id, "instruction": s.instruction, "done_when": s.done_when,
             "intent": s.intent, "budget": s.budget,
             "status": s.status, "result": s.result[:500],
             "verify": s.verify, "lint": s.lint,
             "verify_waived": s.verify_waived,
             "done_via_world_state": s.done_via_world_state,
             "output_path": s.output_path} for s in self.steps]}


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


def _format_outline_step(step: PlanStep, current_id: int) -> list[str]:
    lines = []
    if step.id == current_id:
        mark = "YOUR STEP"
    elif step.status == "done":
        mark = "done"
    else:
        mark = "pending"

    title = _re.sub(r"^Step\s+\d+\s*[-:]\s*", "", (step.title or "").strip())
    label = title or (step.instruction.split("\n")[0][:100] if step.instruction else "")
    lines.append(f"  Step {step.id} [{mark}] {label}")

    if step.id != current_id and step.instruction and title:
        lines.append(f"    When: {step.instruction.split(chr(10))[0][:160]}")

    if step.status == "done" and step.id != current_id:
        res_line = _format_outline_step_result(step)
        if res_line:
            lines.append(res_line)
        if step.output_path:
            lines.append(f"    Full output: {step.output_path}")
    return lines


def _plan_outline(steps: list[PlanStep], current_id: int) -> str:
    """The shape of the whole plan, one line per step, with statuses.

    Done steps are annotated with their (short) result and, when the run
    store is active, the exact file holding their full output."""
    lines = ["The plan:"]
    for s in steps:
        lines.extend(_format_outline_step(s, current_id))
    return "\n".join(lines) + "\n"


def _index_describe(chat_fn, name: str, content: str) -> str:
    """One per-file description call for the indexing primitive.

    Returns the model's raw reply, or "" on any failure (the indexing
    build treats an empty description as a decline, and the step
    falls back to its normal executor sub-run)."""
    try:
        msg = chat_fn([{"role": "user",
                        "content": _indexing.describe_prompt(name,
                                                             content)}],
                      None)
    except Exception:
        return ""
    if not isinstance(msg, dict):
        return ""
    return (msg.get("content") or "")


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
    node = next((n for n in graph.nodes if n.step_id == step.id), None)
    if node is None or (not node.inputs and not node.outputs):
        return ""
    producers = {e.field: e.producer for e in graph.edges
                 if e.consumer == step.id}

    def describe(label: str) -> str:
        if label in producers:
            return f'"{label}" (produced by step {producers[label]})'
        if label.startswith("files from step ") \
                or (label.startswith("step ") and label.endswith(" output")):
            return f'"{label}"'
        return f'"{label}" (from the task or the workspace)'

    lines = ["Your place in the plan graph (computed from the plan, "
             "deterministic):"]
    if node.inputs:
        lines.append("  Inputs available to you: "
                     + "; ".join(describe(i) for i in node.inputs) + ".")
    if node.outputs:
        lines.append("  Outputs this step must produce: "
                     + "; ".join(f'"{o}"' for o in node.outputs) + ".")
        lines.append("  This step is complete only when every output "
                     "above exists with its content in place, under "
                     "exactly that name. Later steps consume these "
                     "outputs as named.")
    return "\n".join(lines) + "\n"


def _sub_step_names_prior(sub_step, priors: list[str]) -> bool:
    import json as _json
    if not (sub_step.tool and sub_step.args):
        return False
    dumped = _json.dumps(sub_step.args, default=str)
    return any(p in dumped for p in priors)


def _find_prior_read_index(sub_steps, priors: list[str]) -> int | None:
    return next((i for i, s in enumerate(sub_steps) if _sub_step_names_prior(s, priors)), None)


def _unreferenced_priors_feedback(step: PlanStep, priors: list[str]) -> str:
    return (f"HARNESS: you are finishing step {step.id} without having read "
            f"the output of the earlier step(s) this plan builds on: "
            f"{', '.join(priors)}. Those files are the record of what the "
            f"earlier steps actually produced. Read the file now with "
            f"read_file and redo this step using its real contents. If this "
            f"step genuinely does not depend on that output, answer DONE: "
            f"again and say why in one sentence. Your own step's output "
            f"file ({step.output_path}) is not one of these sources: the "
            f"harness writes it after you finish, it does not exist yet, "
            f"so do not try to read it.")


def _collect_covers_targets_and_items(step: PlanStep, steps: list[PlanStep]) -> tuple[list[str], list[str]]:
    from . import verify as _verify
    by_id = {s.id: s for s in steps}
    items: list[str] = []
    targets: list[str] = []
    for then in (step.done_when or "").split("\n"):
        then = then.strip()
        if not then or _verify.match_verifier(then) != "covers":
            continue
        m = _re.search(r"step\s+(\d+)", then)
        if not m:
            continue
        src = by_id.get(int(m.group(1)))
        if src is None:
            continue
        parsed = _verify.parse_then(then)
        if parsed and parsed[1]:
            targets.append(parsed[1])
        for item in src.source_items or []:
            if item not in items:
                items.append(item)
    return targets, items


def _find_missing_covered_items(items: list[str], steps: list[PlanStep],
                                current_step: PlanStep, sub_steps) -> list[str]:
    import json as _json
    blobs = set()
    for s in steps:
        if s.id < current_step.id:
            blobs.update(s.touched_args or set())
    for s in sub_steps:
        if s.tool and s.args:
            blobs.add(_json.dumps(s.args, default=str))
    return [i for i in items if not any(i in b for b in blobs)]


def _missing_covers_feedback(targets: list[str], missing: list[str]) -> str:
    claim = f'"{targets[0]}" covers' if targets else "This step covers"
    return (f"HARNESS: {claim} the files from an earlier "
            f"listing step, but these files have not been read "
            f"yet in this run: {', '.join(missing)}. Work about "
            f"a file must come from its actual contents, not "
            f"from its name in a list. Read each named file now "
            f"with read_file, then redo this step from what "
            f"they contain. If a named file genuinely is not a "
            f"source for this step, answer DONE: again and say "
            f"why in one sentence.")


def _has_work_after_prior_read(sub_steps, read_idx: int, priors: list[str]) -> bool:
    return any(s.tool and not _sub_step_names_prior(s, priors) for s in sub_steps[read_idx + 1:])


def _unchanged_after_read_feedback(step: PlanStep, priors: list[str]) -> str:
    return (f"HARNESS: you read {', '.join(priors)} but you have not changed "
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
    priors = [s.output_path for s in steps
              if s.id < step.id and s.status == "done" and s.output_path]
    if not priors:
        return None

    def gate(sub_steps) -> str | None:
        read_idx = _find_prior_read_index(sub_steps, priors)
        if read_idx is None:
            return _unreferenced_priors_feedback(step, priors)

        targets, items = _collect_covers_targets_and_items(step, steps)
        if items:
            missing = _find_missing_covered_items(items, steps, step, sub_steps)
            if missing:
                return _missing_covers_feedback(targets, missing)

        if _has_work_after_prior_read(sub_steps, read_idx, priors):
            return None

        if verify_now is not None:
            try:
                if verify_now():
                    return None
            except Exception:
                pass

        return _unchanged_after_read_feedback(step, priors)

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


def _is_content_bearing_pass(res) -> bool:
    if not res.checks:
        return False
    if any(not c.ok or c.verifier == "attest" for c in res.checks):
        return False
    return any(c.verifier in _CONTENT_BEARING_VERIFIERS for c in res.checks)


def _record_attempt_metrics(step: PlanStep, attempts: list[dict]):
    if not attempts:
        return
    import json as _json
    for att in attempts:
        for s in att.get("sub_steps") or []:
            if s.tool and s.args:
                step.touched_args.add(_json.dumps(s.args, default=str))

    last_sub = attempts[-1].get("sub_steps") or []
    counts: dict[str, int] = {}
    n_lines = 0
    for s in last_sub:
        if s.tool:
            counts[s.tool] = counts.get(s.tool, 0) + 1
            n_lines += len((s.result or "").splitlines())
    if counts:
        step.tool_summary = (
            ", ".join(f"{t} x{n}" for t, n in counts.items())
            + f"; {n_lines} lines of tool output")


def _write_step_to_store(store, step: PlanStep, attempts: list[dict], status: str):
    if store is None:
        return
    try:
        store.write_step(
            step_id=step.id, instruction=step.instruction,
            given=step.given, done_when=step.done_when,
            answer=step.result, sub_steps=[], status=status,
            verify_checks=step.verify, verify_waived=step.verify_waived,
            attempts=attempts or None)
    except Exception:
        pass


def _run_step_verified(task: str, step: PlanStep, steps: list[PlanStep],
                     cfg: Config, chat_fn, jail, emit,
                     store=None, only_tools=None) -> bool:
    """Run one plan step; after the sub-run reports DONE, verify the Then
    clauses in code before advancing. A failed verification warns and retries
    the step once (bounded by cfg.verify_retries) with the exact failure as
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
    vctx = _verify.VerifyContext(
        jail=jail, step_id=step.id, task=task,
        prior_items={s.id: list(s.source_items) for s in steps if s.source_items},
        intent=step.intent or "")
    feedback = ""
    last_vres = None
    attempts: list[dict] = []
    n_steps = len(steps)
    thens = [t.strip() for t in step.done_when.split("\n") if t.strip()]

    def verify_now() -> bool:
        """True only when the step's Thens machine-check as passing right
        now AND at least one passing check is content-bearing.

        Attested clauses are not evidence, and neither is a bare
        existence pass: the stub that founded stage 2 satisfied
        `"file" exists` while promising its content "to be filled in".
        The checks that answer stage 2's suspicion measure content:
        contains, contains_exactly, indexes (against the source items),
        has_lines. See _grounding_gate."""
        if not thens:
            return False
        res = _verify.verify_step(thens, vctx)
        return _is_content_bearing_pass(res)

    gate = _grounding_gate(step, steps, verify_now=verify_now)
    # Experimental: the step's plan-graph node contract rides in its
    # prompt when enabled. Computed once here; the graph derives from
    # plan text, so the verification retry sees the same contract.
    contract = (_graph_contract_block(task, step, steps)
                if getattr(cfg, "graph_contracts", False) else "")
    # Issue #14: this step's turn budget is computed from its planned
    # operations now that prior steps' source items are known, and the
    # same budget governs the verification retry.
    step.budget = step_budget(step, steps, cfg)
    sub_cfg = replace(cfg, max_steps=step.budget)

    def persist(status: str):
        _record_attempt_metrics(step, attempts)
        _write_step_to_store(store, step, attempts, status)

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
        target, source_id = clause
        src_step = next((s for s in steps if s.id == source_id), None)
        items = list(src_step.source_items) if src_step is not None else []
        records = None
        if items:
            records = _indexing.build_index(
                jail.root, items,
                lambda name, content: _index_describe(chat_fn, name,
                                                      content))
        if records is not None:
            dest = jail.resolve(target)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(_indexing.render_index(records))
            step.result = (f"DONE: indexed {len(records)} files into "
                           f"{target} with the harness indexing "
                           f"primitive.")
            step.source_items = list(items)
            import json as _json
            for name in items:
                step.touched_args.add(_json.dumps({"path": name}))
            step.touched_args.add(_json.dumps({"path": target}))
            vres = _verify.verify_step(thens, vctx)
            step.verify = [{"then": c.then, "verifier": c.verifier,
                            "ok": c.ok, "detail": c.detail}
                           for c in vres.checks]
            attempts.append({"sub_steps": [], "answer": step.result,
                             "verify_checks": list(step.verify)})
            emit("step_verify", step, vres)
            if vres.ok:
                step.status = "done"
                persist("done")
                emit("step_done", step)
                return True
            feedback = "; ".join(c.detail for c in vres.checks
                                  if not c.ok)

    # Issue #48: files the step's Thens name that its When does not.
    # The prompt presents them as the step's output file, and a
    # refused write's feedback names them, so a plan that reaches
    # execution with the gap still gives the step a way to recover.
    outputs = _then_named_outputs(step)
    refusal_hint = None
    if outputs:
        listed = ", ".join(f"'{o}'" for o in outputs)
        refusal_hint = (f"HARNESS: this step's output file is "
                        f"{listed} (named in the step's Then). Write "
                        f"to that file instead.")

    for _ in range(1 + cfg.verify_retries):
        prompt = (f"Overall goal: {task}\n"
                  f"You are executing step {step.id} of {n_steps}. "
                  f"Do ONLY this step, nothing else.\n\n")
        prompt += FRAME_TEXT + "\n\n"
        prompt += _plan_outline(steps, step.id) + "\n"
        if step.given:
            prompt += f"Starting state: {step.given}\n"
        if step.intent:
            prompt += f"Intent of this step: {step.intent}\n"
        prompt += (f"Step: {step.instruction}\n"
                   f"This step is done when: {step.done_when or 'its instruction is complete'}\n")
        if outputs:
            listed = ", ".join(f"'{o}'" for o in outputs)
            prompt += (f"Output file for this step: {listed} (named "
                       f"in this step's Then). Write this step's "
                       f"result to that file.\n")
        if contract:
            prompt += contract
        if store is not None and step.output_path:
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
        if feedback:
            prompt += (f"Your previous attempt failed verification: {feedback} "
                       f"Fix exactly this and try again.\n")
        prompt += 'When this step is complete, answer starting with "DONE:".'

        try:
            r = loop.run(prompt, sub_cfg, chat_fn=chat_fn,
                         on_step=lambda s: emit("step_sub", step, s),
                         done_gate=gate, only_tools=only_tools,
                         refusal_hint=refusal_hint)
        except Exception as e:
            step.status = "failed"
            step.result = f"harness error: {e}"
            attempts.append({"sub_steps": [], "answer": step.result,
                             "verify_checks": []})
            persist("failed")
            emit("step_failed", step)
            return False
        step.result = (r.answer or "")[:800]
        if r.status != "done":
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
            vres = None
            answer = (r.answer or "").strip()
            if (r.status == "max_steps" and answer
                    and not answer.startswith("(summary call failed")
                    and not answer.startswith("(no summary)")):
                step.source_items = _verify.collect_step_items(
                    list(r.steps))
                vres = _verify.verify_step(thens, vctx)
                step.verify = [{"then": c.then, "verifier": c.verifier,
                                "ok": c.ok, "detail": c.detail}
                               for c in vres.checks]
                emit("step_verify", step, vres)
            attempts.append({"sub_steps": list(r.steps),
                             "answer": r.answer or "",
                             "verify_checks": list(step.verify)
                             if vres is not None else []})
            if vres is not None and vres.ok:
                step.status = "done"
                step.done_via_world_state = True
                persist("done")
                emit("step_done_world_state", step, vres)
                emit("step_done", step)
                return True
            step.status = "failed"
            persist("failed")
            emit("step_failed", step)
            return False
        step.source_items = _verify.collect_step_items(list(r.steps))
        vres = _verify.verify_step(thens, vctx)
        step.verify = [{"then": c.then, "verifier": c.verifier,
                        "ok": c.ok, "detail": c.detail} for c in vres.checks]
        attempts.append({"sub_steps": list(r.steps), "answer": r.answer or "",
                         "verify_checks": list(step.verify)})
        emit("step_verify", step, vres)
        if vres.ok:
            step.status = "done"
            persist("done")
            emit("step_done", step)
            return True
        last_vres = vres
        feedback = "; ".join(c.detail for c in vres.checks if not c.ok)
    # Retries exhausted: downgrade to a warning and continue. The step ran;
    # only its acceptance criteria are disputed (often a wrong plan, not
    # wrong work) — the task-level checker remains the backstop.
    step.status = "done"
    step.verify_waived = True
    persist("done")
    emit("step_verify_waived", step, last_vres)
    emit("step_done", step)
    return True


def execute_plan(task: str, steps: list[PlanStep], cfg: Config, chat_fn=None,
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
    chat_fn, jail, _registry = _chat_and_tools(cfg, chat_fn)

    def emit(kind, *args):
        if on_event:
            on_event(kind, *args)

    store = None
    try:
        from .runstore import RunStore
        store = RunStore(cfg.workspace)
        for s in steps:
            s.output_path = store.rel_path(s.id)
    except Exception:
        store = None  # record-keeping must never block execution

    for step in steps:
        emit("step_start", step)
        if not _run_step_verified(task, step, steps,
                                  cfg, chat_fn, jail, emit, store=store,
                                  only_tools=only_tools):
            break

    status = "done" if steps and all(s.status == "done" for s in steps) else "step_failed"

    # Issue #22: the deliverable audit. Steps can finish with their
    # verification waived, and a waived step still counts done, so the
    # per-step machinery alone cannot promise the user a deliverable.
    # Before the run may report done, every file the plan's Thens
    # promised must exist. This judges existence only, neither plan
    # adherence nor content quality, so it does not reopen the
    # warn-downgrade decision: a warning can excuse a wrong check,
    # never a missing delivery.
    missing: list[str] = []
    if status == "done":
        from . import verify as _verify
        missing = _verify.missing_deliverables(steps, jail)
        if missing:
            status = "step_failed"
            emit("deliverables_missing", missing)
    if missing:
        answer = ("The plan's steps finished, but this run is a "
                  "failure: promised deliverable(s) were never "
                  "created: " + ", ".join(missing) + ". A run only "
                  "reports done when every file the plan promises "
                  "exists.")
        return PlannedRun(status, answer, steps,
                          run_id=store.run_id if store is not None else "")

    # Issue #37: a run that finished with a deliverable resting
    # entirely on attested checks must not report plain done. The
    # trust-only steps are named in the final status, while the run
    # still counts as completed (see run.py's exit mapping).
    trust_only: list = []
    if status == "done":
        from . import verify as _verify
        trust_only = _verify.trust_only_steps(steps)
        if trust_only:
            status = "done_on_trust"

    summary = (f"Overall goal: {task}\nPlan execution ended: {status}.\n"
               + "\n".join(f"Step {s.id} [{s.status}]: {s.instruction}"
                            + (f" -> {s.result[:200]}" if s.result else "")
                            for s in steps))
    if trust_only:
        names = "; ".join(
            f"step {e['step']} ({', '.join(e['deliverables'])})"
            for e in trust_only)
        summary += (f"\nSteps finished on trust: {names}. Their Then "
                    f"clauses were attested, not machine-checked, so "
                    f"their deliverables' contents were never verified. "
                    f"Say plainly which steps finished on trust.")
    summary += ('\nSummarize the outcome in 2-4 sentences, starting with "DONE:".')
    try:
        msg = chat_fn([{"role": "user", "content": summary}], None)
        answer = (msg.get("content") or "").strip() or "(no summary)"
    except Exception as e:
        answer = f"(summary call failed: {e})"
    return PlannedRun(status, answer, steps,
                      run_id=store.run_id if store is not None else "",
                      trust_only=trust_only)


def run_planned(task: str, cfg: Config, chat_fn=None, on_event=None) -> PlannedRun:
    """Auto mode: propose a plan and execute it in one go. The eval uses this."""
    text, result = propose(task, cfg, chat_fn)
    if text is None:
        return PlannedRun("plan_failed", f"Could not get a valid plan: {result}", [])
    if on_event:
        on_event("plan", result)
    return execute_plan(task, result, cfg, chat_fn, on_event)
