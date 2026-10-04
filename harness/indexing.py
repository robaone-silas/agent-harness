"""Indexing as a harness primitive.

The list-and-compile shape kept failing in the field in the same
ways: an executor step spent its whole turn budget reading source
files one at a time, ran ahead into the next step's work, or wrote a
compiled document that silently omitted a source. `covers` checks a
model-written compilation after the fact; `indexes` is the
productive counterpart. When a step's Then carries
`"<target>" indexes the files from step N`, the harness builds the
target itself:

  1. enumerate step N's recorded source items (deterministic),
  2. read each file (deterministic),
  3. make one small model call per file for its one-line description
     (the only judgment in the shape, isolated per item),
  4. write the target with exactly one entry per file.

Completeness is by construction rather than by inspection. If any
part cannot complete (a source file unreadable, a description call
returning nothing), build_index declines by returning None and the
caller falls back to the normal executor sub-run for the step: the
primitive is an accelerator, never a gate.
"""
from __future__ import annotations

import re
from pathlib import Path

from . import verify as _verify

# The marker phrase opening every per-file description request, so
# callers and tests can recognize the call shape.
DESCRIBE_MARKER = "Describe this file for an index."
# How much of a file the description call sees.
MAX_CONTENT_CHARS = 6000
# Descriptions are one line; anything longer is prose, not an entry.
MAX_DESCRIPTION_CHARS = 240

_CLAUSE_SOURCE = re.compile(
    r"indexes\s+the\s+files\s+(?:from|listed\s+(?:in|by))\s+step\s+(\d+)",
    re.IGNORECASE,
)


def clause_for(step) -> tuple[str, int] | None:
    """(target, source_step_id) when the step carries an indexes
    clause, else None. Scans the step's Then lines."""
    for t in (step.done_when or "").split("\n"):
        t = t.strip()
        if not t:
            continue
        parsed = _verify.parse_then(t)
        if parsed and parsed[0] == "indexes" and parsed[1]:
            m = _CLAUSE_SOURCE.search(t)
            if m:
                return parsed[1], int(m.group(1))
    return None


def describe_prompt(name: str, content: str) -> str:
    """The per-file description request: one file, one line back."""
    return (
        f"{DESCRIBE_MARKER}\n"
        f"File: \"{name}\"\n"
        f"Content:\n{content}\n\n"
        "Reply with exactly one line describing what this file "
        "actually contains. No filename, no quotes, no preamble."
    )


def clean_description(raw: str, name: str) -> str:
    """Reduce a model reply to a single description line.

    Takes the first non-empty line, strips bullets and surrounding
    quotes, and drops a leading restatement of the filename. Empty
    stays empty: the caller treats that as a declined description."""
    text = (raw or "").strip()
    if not text:
        return ""
    line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    line = line.lstrip("-*• ").strip()
    if len(line) >= 2 and line[0] in "\"'`" and line[-1] == line[0]:
        line = line[1:-1].strip()
    if line.lower().startswith(name.lower()):
        line = line[len(name):].lstrip(" :-\u2014").strip()
    if len(line) > MAX_DESCRIPTION_CHARS:
        line = line[:MAX_DESCRIPTION_CHARS].rstrip()
    return line


def build_index(root, files: list[str], describe) -> list | None:
    """Build (name, description) records for every file, or None.

    `describe(name, content)` returns the model's raw reply for one
    file. Any unreadable file or empty cleaned description declines
    the whole build: a partial index produced by the primitive would
    be worse than none, because the fallback path can still try."""
    root = Path(root).resolve()
    records: list[tuple[str, str]] = []
    for name in files:
        p = (root / name).resolve()
        if p != root and root not in p.parents:
            return None
        if not p.is_file():
            return None
        try:
            content = p.read_text(errors="replace")
        except OSError:
            return None
        description = clean_description(
            describe(name, content[:MAX_CONTENT_CHARS]), name)
        if not description:
            return None
        records.append((name, description))
    return records


def render_index(records: list) -> str:
    """The target document: one entry line per record, in order."""
    lines = ["# Index", ""]
    lines += [f"- {name}: {description}" for name, description in records]
    return "\n".join(lines) + "\n"
