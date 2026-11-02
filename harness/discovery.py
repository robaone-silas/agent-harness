"""Tier 2 discovery (issue #62).

Split out of planner.py: the read-only discovery pass that runs
before planning when a plan cannot be written until someone has
looked. Its prompt and sampling rule, the discovery plan request,
and the findings handed to the replan with the deterministic
unexamined-files reconciliation. planner.py keeps
plan_with_discovery, which runs the pass, and re-exports these
names, so existing imports keep working.
"""
from __future__ import annotations

import re as _re

from . import gherkin
from .config import Config
from .plan_model import PlanStep
from .plan_prompts import _root_workspace_files, workspace_digest
from .plan_request import _parse_feedback, to_plan_steps

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


def _discovery_records(workspace: str, run_id: str, step_count: int) -> list[tuple[str, str]]:
    """(relative path, full text) of each stored discovery step record."""
    from pathlib import Path
    records: list[tuple[str, str]] = []
    for step_number in range(1, step_count + 1):
        relative_path = f".harness/runs/{run_id}/step-{step_number:02d}.md"
        record_path = Path(workspace) / relative_path
        if record_path.is_file():
            records.append((relative_path, record_path.read_text(errors="replace")))
    return records


def _bounded_excerpts(records: list[tuple[str, str]], total_cap: int,
                      per_step_cap: int) -> tuple[list[str], int]:
    """Record excerpts within the per-step and total caps, and how
    many records were consumed before the total cap was reached. A
    per-step truncation is announced with a pointer to the full
    record."""
    excerpts: list[str] = []
    used_characters = 0
    consumed_count = 0
    for relative_path, record_text in records:
        consumed_count += 1
        if len(record_text) > per_step_cap:
            record_text = record_text[:per_step_cap] + f"\n[truncated; full record: {relative_path}]"
        record_text = record_text[:max(0, total_cap - used_characters)]
        if record_text:
            excerpts.append(record_text)
            used_characters += len(record_text)
        if used_characters >= total_cap:
            break
    return excerpts, consumed_count


_NONE_UNEXAMINED = (
    "Unexamined workspace files: none. Every file at "
    "the workspace root was accounted for by the "
    "discovery plan or its records.")

_UNEXAMINED_HEADING = (
    "Unexamined workspace files (the harness compared "
    "the workspace root against the discovery plan and "
    "records; these files were not listed, read, "
    "searched, named, or explicitly excluded during "
    "discovery, so nothing is known about them):\n")


def _unexamined_section(missing_files: list[str]) -> str:
    if not missing_files:
        return _NONE_UNEXAMINED
    return _UNEXAMINED_HEADING + "\n".join(f"- {name}" for name in missing_files)


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
    if not run_id:
        return ""
    records = _discovery_records(workspace, run_id, step_count)
    excerpts, consumed_count = _bounded_excerpts(records, total_cap, per_step_cap)
    body = "\n\n".join(excerpts)
    if not _root_workspace_files(workspace):
        return body
    full_records = "\n".join(record_text for _relative_path, record_text in records[:consumed_count])
    missing_files = unexamined_workspace_files(workspace, discovery_steps=discovery_steps,
                                               findings=full_records)
    section = _unexamined_section(missing_files)
    return (body + "\n\n" + section) if body else section
