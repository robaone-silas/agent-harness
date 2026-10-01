"""Suggestion library: fill known model knowledge gaps when warnings fire.

Small models fail some tasks not from bad reasoning but from missing idioms
(e.g. e2b hardcodes `print("main.py")` instead of computing the filename —
it doesn't know `os.path.basename(__file__)`). This module pairs trigger
phrases with concrete, copy-pasteable idioms.

Surfaced in two places, both matched against the task text:
  1. Planner prompt — so the plan doesn't bake in a broken implementation.
  2. TDD retry feedback — so a failed attempt gets the idiom alongside the
     checker's complaint.

Pure data + case-insensitive substring matching. Zero model calls.
Add new entries as further gaps are observed; each needs the idiom, not
just advice.
"""
from __future__ import annotations

SUGGESTIONS: list[dict] = [
    {
        "id": "python-own-filename",
        "triggers": (
            "own filename",
            "its own filename",
            "the script's filename",
            "name of the script",
            "prints its own",
        ),
        "title": "A script's own filename, computed at runtime",
        "body": (
            "Never write the filename as a literal string — compute it at "
            "runtime so the script works under any name or path:\n"
            "    import os\n"
            "    print(os.path.basename(__file__))\n"
            "`__file__` is the running script's path; `os.path.basename()` "
            "strips the directories, leaving just e.g. `main.py`. Equivalent "
            "alternative:\n"
            "    import os, sys\n"
            "    print(os.path.basename(sys.argv[0]))"
        ),
    },
]


def for_task(task: str) -> list[dict]:
    """Suggestions whose trigger phrases appear in the task text."""
    t = (task or "").lower()
    return [s for s in SUGGESTIONS
            if any(tr in t for tr in s["triggers"])]


def render(suggestions: list[dict]) -> str:
    """Compact rendering for prompt injection."""
    return "\n".join(f"- {s['title']}:\n{s['body']}" for s in suggestions)
