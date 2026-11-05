"""Plan history: save successful plans, query them by similarity.

A saved plan is an example for a new plan, never a plan to execute
verbatim. When a run finishes fully verified, its approved plan is
saved under the workspace's own `.harness/plans/` directory, beside an
`index.json` that records its title, task, intents, deliverables, and
run id. A later request scores saved plans by deterministic token
overlap and, when one clears the threshold, the planner prompt shows
that plan as a labelled example and the model still writes a new plan.

Rules, decided 2026-11-04 and pinned by tests/test_plan_history.py:

- Only fully verified `done` runs save. Callers enforce the status;
  this module saves whatever record it is given, so the run paths
  must check status before calling save_plan.
- History is workspace local only. Nothing here reads or writes
  outside the workspace passed in.
- At most one plan is presented, and only at or above an overlap of
  0.25. With no saved plans, or no scorer above threshold, the caller
  presents nothing and planning proceeds unchanged.
- At most 50 plans per workspace. Saving past the cap drops the
  oldest record and its file.
- Deduplication: saving a plan for a task already in the index, or
  with identical plan text, removes the old record and its file and
  saves the new one, so the latest version is the one that stays.
- A missing or corrupt index is treated as empty, never as a reason
  for planning to fail.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime
from pathlib import Path

from . import gherkin

logger = logging.getLogger(__name__)

SIMILARITY_THRESHOLD = 0.25
MAX_HISTORY_PLANS = 50
MAX_PRESENTED_PLANS = 1
PLANS_DIRECTORY_NAME = "plans"
INDEX_FILE_NAME = "index.json"

STOPWORDS = {
    "a", "an", "the", "and", "or", "of", "to", "in", "for", "with",
    "my", "me", "i", "you", "it", "is", "are", "on", "at", "by",
    "from", "this", "that", "these", "those", "please", "do", "not",
    "every", "each", "all", "when", "then",
}

_DELIVERABLE_PATTERN = re.compile(r'"([^"]+\.(?:md|txt|csv|json))"')
_EXTENSION_PATTERN = re.compile(r"\.([a-z]{2,4})\b")
_TOKEN_PATTERN = re.compile(r"[a-z0-9]+")


def _plans_directory(workspace: str | Path | None) -> Path | None:
    if not workspace:
        return None
    return Path(workspace) / ".harness" / PLANS_DIRECTORY_NAME


def _index_path(workspace: str | Path | None) -> Path | None:
    plans_directory = _plans_directory(workspace)
    if plans_directory is None:
        return None
    return plans_directory / INDEX_FILE_NAME


def _normalize_text(text: str) -> str:
    return " ".join((text or "").split()).lower()


def _tokenize(text: str) -> set[str]:
    return {token for token in _TOKEN_PATTERN.findall((text or "").lower())
            if token not in STOPWORDS}


def _deliverable_names(plan_text: str) -> list[str]:
    found_names: list[str] = []
    for deliverable_name in _DELIVERABLE_PATTERN.findall(plan_text or ""):
        if deliverable_name not in found_names:
            found_names.append(deliverable_name)
    return found_names


def _load_index(workspace: str | Path | None) -> list[dict]:
    """The index records, or an empty list when the index is missing,
    corrupt, or not a list of records. History must never break
    planning, so every failure mode here degrades to empty."""
    if not workspace:
        return []
    index_path = _index_path(workspace)
    if index_path is None:
        return []
    try:
        index_text = index_path.read_text()
    except OSError:
        return []
    try:
        index_records = json.loads(index_text)
    except ValueError:
        logger.debug("plan history index is not valid JSON, treating it as empty")
        return []
    if not isinstance(index_records, list):
        return []
    return [record for record in index_records
            if isinstance(record, dict) and record.get("file_name")]


def _write_index(workspace: str | Path | None, index_records: list[dict]) -> bool:
    plans_directory = _plans_directory(workspace)
    if plans_directory is None:
        return False
    try:
        plans_directory.mkdir(parents=True, exist_ok=True)
        temporary_path = plans_directory / (INDEX_FILE_NAME + ".tmp")
        temporary_path.write_text(json.dumps(index_records, indent=2))
        index_path = _index_path(workspace)
        if index_path is None:
            return False
        temporary_path.replace(index_path)
    except OSError:
        logger.debug("plan history index could not be written", exc_info=True)
        return False
    return True


def _remove_record_file(workspace: str | Path | None, record: dict) -> None:
    plans_directory = _plans_directory(workspace)
    if plans_directory is None:
        return
    try:
        (plans_directory / record["file_name"]).unlink(missing_ok=True)
    except OSError:
        logger.debug("plan history file could not be removed", exc_info=True)


def _unique_file_name(plans_directory: Path, base_name: str) -> str:
    candidate_name = base_name
    suffix_number = 2
    while (plans_directory / candidate_name).exists():
        stem, dot, extension = base_name.rpartition(".")
        candidate_name = f"{stem}-{suffix_number}.{extension}" if dot else f"{base_name}-{suffix_number}"
        suffix_number += 1
    return candidate_name


def save_plan(workspace: str | Path | None, plan_text: str, task_text: str = "",
              status: str = "done", run_id: str = "",
              now: datetime | None = None) -> dict | None:
    """Save a parsed plan and its metadata, enforcing deduplication
    and the cap. Returns the saved record, or None when the plan does
    not parse or the history cannot be written. The optional now
    stamps the record, so callers can pass their run timestamp and
    tests can assert timestamps deterministically."""
    if not workspace:
        return None
    parsed_plan, parse_error = gherkin.parse_feature(plan_text or "")
    if parsed_plan is None:
        logger.debug("plan history skipped an unparseable plan: %s", parse_error)
        return None
    canonical_text = gherkin.render_feature(parsed_plan.title, parsed_plan.steps)
    step_intents = [step.intent for step in parsed_plan.steps if step.intent]
    normalized_task = _normalize_text(task_text)
    plan_text_hash = hashlib.sha256(canonical_text.encode()).hexdigest()
    plans_directory = _plans_directory(workspace)
    if plans_directory is None:
        return None
    try:
        plans_directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        logger.debug("plan history directory could not be created", exc_info=True)
        return None
    index_records = _load_index(workspace)
    kept_records: list[dict] = []
    for existing_record in index_records:
        same_task = bool(normalized_task) and _normalize_text(existing_record.get("task_text", "")) == normalized_task
        same_plan = existing_record.get("plan_text_hash") == plan_text_hash
        if same_task or same_plan:
            _remove_record_file(workspace, existing_record)
        else:
            kept_records.append(existing_record)
    run_moment = now or datetime.now()
    timestamp = run_moment.strftime("%Y%m%d-%H%M%S")
    title_slug = re.sub(r"[^a-z0-9]+", "-", parsed_plan.title.lower()).strip("-")[:30] or "plan"
    file_name = _unique_file_name(
        plans_directory, f"{timestamp}-{title_slug}-{plan_text_hash[:8]}.feature")
    try:
        (plans_directory / file_name).write_text(canonical_text)
    except OSError:
        logger.debug("plan history file could not be written", exc_info=True)
        return None
    record = {
        "file_name": file_name,
        "title": parsed_plan.title,
        "task_text": task_text or "",
        "intents": step_intents,
        "status": status,
        "saved_at": run_moment.isoformat(timespec="seconds"),
        "step_count": len(parsed_plan.steps),
        "deliverable_names": _deliverable_names(canonical_text),
        "run_id": run_id or "",
        "plan_text_hash": plan_text_hash,
    }
    kept_records.append(record)
    while len(kept_records) > MAX_HISTORY_PLANS:
        oldest_record = kept_records.pop(0)
        _remove_record_file(workspace, oldest_record)
    if not _write_index(workspace, kept_records):
        return None
    return record


def save_successful_plan(workspace: str | Path | None, plan_text: str, task_text: str = "",
                         status: str = "", run_id: str = "",
                         now: datetime | None = None) -> dict | None:
    """Save a plan only when its run finished fully verified. Any
    other status, and any history write failure, returns None instead
    of raising, so bookkeeping can never fail a run."""
    if status != "done":
        return None
    try:
        return save_plan(workspace, plan_text, task_text=task_text, status=status, run_id=run_id, now=now)
    except Exception:
        logger.debug("plan history save failed, run status is unchanged", exc_info=True)
        return None


def find_similar(workspace: str | Path | None, task_text: str,
                 threshold: float = SIMILARITY_THRESHOLD,
                 limit: int = MAX_PRESENTED_PLANS) -> list[tuple[dict, float]]:
    """Saved plans ranked by token overlap with the new task, highest
    first, filtered to scores at or above the threshold. Returns an
    empty list when there is no history, the task has no content
    tokens, or nothing clears the threshold."""
    if not workspace:
        return []
    task_tokens = _tokenize(task_text)
    if not task_tokens:
        return []
    task_extensions = set(_EXTENSION_PATTERN.findall((task_text or "").lower()))
    scored_records: list[tuple[int, dict, float]] = []
    for record_position, record in enumerate(_load_index(workspace)):
        query_text = " ".join([
            record.get("task_text", ""),
            record.get("title", ""),
            " ".join(record.get("intents", [])),
        ])
        query_tokens = _tokenize(query_text)
        shared_tokens = task_tokens & query_tokens
        score = len(shared_tokens) / len(task_tokens)
        saved_extensions = set()
        for deliverable_name in record.get("deliverable_names", []):
            saved_extensions.update(_EXTENSION_PATTERN.findall(deliverable_name.lower()))
        if task_extensions & saved_extensions:
            score = min(score + 0.05, 1.0)
        if score >= threshold:
            scored_records.append((record_position, record, score))
    scored_records.sort(key=lambda scored_item: (-scored_item[2], -scored_item[0]))
    ranked_matches = [(record, score) for _, record, score in scored_records]
    if limit is None:
        return ranked_matches
    return ranked_matches[:limit]


def best_match(workspace: str | Path | None, task_text: str) -> tuple[dict | None, str, float]:
    """The single best matching saved plan and its text, or empty
    values when nothing clears the threshold or its file is gone."""
    matches = find_similar(workspace, task_text)
    if not matches:
        return None, "", 0.0
    record, score = matches[0]
    plans_directory = _plans_directory(workspace)
    if plans_directory is None:
        return None, "", 0.0
    try:
        plan_text = (plans_directory / record["file_name"]).read_text()
    except OSError:
        logger.debug("plan history file is missing for an index record", exc_info=True)
        return None, "", 0.0
    return record, plan_text, score


def format_example_section(plan_text: str) -> str:
    """The labelled prompt section for a matched past plan, with the
    adapt-or-ignore instruction. Empty when there is no plan text."""
    if not plan_text:
        return ""
    return (
        "A similar plan from an earlier successful run (example only):\n"
        f"{plan_text}\n"
        "Copy the shape, not the content. Adapt every filename, value, "
        "and description to this workspace and this request, using the "
        "workspace contents listed above. Do not reuse a filename or a "
        "value from the example unless it is also present in this "
        "workspace and this request. If the example does not fit this "
        "request, ignore it.\n"
    )
