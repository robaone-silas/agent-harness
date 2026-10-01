"""Tool registry. Five small tools, all jailed to a workspace root.

Small models do better with few, sharply-defined tools. Each tool has a
JSON-schema parameter spec so the model sees exact expectations, and the
harness validates arguments before executing anything.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path


class ToolError(RuntimeError):
    pass


class Refusal(ToolError):
    """Safety refusal, not a failure: the action was blocked by a guardrail
    (destructive pattern, workspace escape). A model that hits one and stops
    is behaving correctly, so DONE: afterwards is accepted outright."""


class Jail:
    """Confines all file access under a root directory."""

    def __init__(self, root: str):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def resolve(self, rel: str) -> Path:
        p = (self.root / rel).resolve()
        if p != self.root and self.root not in p.parents:
            raise Refusal(f"path escapes workspace: {rel!r}")
        return p


def _truncate(text: str, limit: int) -> str:
    if len(text) > limit:
        return text[:limit] + f"\n...[truncated {len(text) - limit} chars]"
    return text


def make_tools(jail: Jail, blocked: tuple[str, ...], exec_timeout: int,
               max_output_chars: int) -> dict:
    def t_exec(command: str) -> str:
        for b in blocked:
            if b in command:
                raise Refusal(f"blocked command pattern: {b!r}")
        try:
            proc = subprocess.run(
                command, shell=True, cwd=str(jail.root), capture_output=True,
                text=True, timeout=exec_timeout)
        except subprocess.TimeoutExpired:
            raise ToolError(f"command timed out after {exec_timeout}s")
        out = (proc.stdout or "") + (proc.stderr or "")
        out = _truncate(out.strip(), max_output_chars) or "(no output)"
        return f"exit={proc.returncode}\n{out}"

    def t_read_file(path: str, offset: int = 0, limit: int = 200) -> str:
        p = jail.resolve(path)
        if not p.is_file():
            raise ToolError(f"no such file: {path!r}")
        lines = p.read_text(errors="replace").splitlines()
        chunk = lines[offset:offset + limit]
        return _truncate("\n".join(chunk), max_output_chars) or "(empty file)"

    def t_write_file(path: str, content: str) -> str:
        p = jail.resolve(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return f"wrote {len(content)} bytes to {path}"

    def t_edit_file(path: str, old_text: str, new_text: str) -> str:
        p = jail.resolve(path)
        if not p.is_file():
            raise ToolError(f"no such file: {path!r}")
        text = p.read_text(errors="replace")
        count = text.count(old_text)
        if count == 0:
            raise ToolError("old_text not found in file")
        text = text.replace(old_text, new_text, 1)
        p.write_text(text)
        extra = f" ({count - 1} other occurrence(s) left untouched)" if count > 1 else ""
        return f"replaced 1 occurrence in {path}{extra}"

    def t_list_dir(path: str = ".") -> str:
        p = jail.resolve(path)
        if not p.is_dir():
            raise ToolError(f"no such directory: {path!r}")
        names = sorted(x.name + ("/" if x.is_dir() else "") for x in p.iterdir())
        return _truncate("\n".join(names) or "(empty)", max_output_chars)

    def t_grep_files(pattern: str, glob: str = "*.txt") -> str:
        # The list-and-scan loop lives in code, not in the model: enumerate
        # files, check contents, return matches. The model never handles
        # filenames it hasn't seen, so it can't hallucinate them.
        import fnmatch
        if not pattern:
            raise ToolError("pattern must not be empty")
        matches = []
        for p in sorted(jail.root.rglob("*")):
            if not p.is_file():
                continue
            rel = str(p.relative_to(jail.root))
            if not fnmatch.fnmatch(rel, glob):
                continue
            try:
                if pattern in p.read_text(errors="replace"):
                    matches.append(rel)
            except OSError:
                continue
            if len(matches) >= 50:
                matches.append("...[truncated at 50]")
                break
        return "\n".join(matches) if matches else "(no matches)"

    defs = [
        {"name": "exec", "description":
         "Run a shell command inside the workspace. Returns exit code and output.",
         "parameters": {"type": "object",
                        "properties": {"command": {"type": "string",
                                                  "description": "Shell command to run."}},
                        "required": ["command"]},
         "func": lambda a: t_exec(a["command"])},
        {"name": "read_file", "description":
         "Read a text file, optionally paged by line offset/limit.",
         "parameters": {"type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "offset": {"type": "integer", "default": 0},
                            "limit": {"type": "integer", "default": 200}},
                        "required": ["path"]},
         "func": lambda a: t_read_file(a["path"], int(a.get("offset", 0)),
                                        int(a.get("limit", 200)))},
        {"name": "write_file", "description":
         "Create or overwrite a text file with the given content.",
         "parameters": {"type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "content": {"type": "string"}},
                        "required": ["path", "content"]},
         "func": lambda a: t_write_file(a["path"], a["content"])},
        {"name": "edit_file", "description":
         "Replace the first occurrence of old_text with new_text in a file.",
         "parameters": {"type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "old_text": {"type": "string"},
                            "new_text": {"type": "string"}},
                        "required": ["path", "old_text", "new_text"]},
         "func": lambda a: t_edit_file(a["path"], a["old_text"], a["new_text"])},
        {"name": "list_dir", "description":
         "List files and directories under a workspace path.",
         "parameters": {"type": "object",
                        "properties": {"path": {"type": "string", "default": "."}},
                        "required": []},
         "func": lambda a: t_list_dir(a.get("path", "."))},
        {"name": "grep_files", "description":
         "Search file contents: return workspace files (filtered by glob, e.g. '*.txt') "
         "whose text contains the given string. Use this instead of listing and reading "
         "files one by one when hunting for content.",
         "parameters": {"type": "object",
                        "properties": {
                            "pattern": {"type": "string",
                                        "description": "Text to search for (case-sensitive)."},
                            "glob": {"type": "string", "default": "*.txt",
                                     "description": "Filename pattern, e.g. '*.txt' or '*.py'."}},
                        "required": ["pattern"]},
         "func": lambda a: t_grep_files(a["pattern"], a.get("glob", "*.txt"))},
    ]
    return {d["name"]: d for d in defs}


def validate_args(tool_def: dict, args: object) -> dict:
    """Lightweight validation against the tool's JSON schema. Raises ToolError."""
    if not isinstance(args, dict):
        raise ToolError(f"arguments must be an object, got: {args!r}"[:200])
    schema = tool_def["parameters"]
    for req in schema.get("required", []):
        if req not in args:
            raise ToolError(f"missing required argument: {req!r}")
    props = schema.get("properties", {})
    for key, spec in props.items():
        if key in args and spec.get("type") == "integer":
            try:
                args[key] = int(args[key])
            except (TypeError, ValueError):
                raise ToolError(f"argument {key!r} must be an integer")
    return args
