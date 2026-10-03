"""Tool registry. Small, sharply-defined tools, all jailed to a workspace root.

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

    def t_append_file(path: str, content: str) -> str:
        # v0.8.1: plans say "add it to <file>", and until now the toolset
        # had no verb for adding: write_file replaces the whole file, so a
        # step "adding" its entry silently destroyed earlier steps'
        # entries (index task, run 1, 2026-10-03). Append is verbatim: no
        # separator magic, the caller owns its line breaks.
        p = jail.resolve(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        created = not p.is_file()
        with p.open("a") as f:
            f.write(content)
        note = " (created the file)" if created else ""
        return f"appended {len(content)} bytes to {path}{note}"

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

    def t_list_dir(path: str = ".", depth: int = 1) -> str:
        # depth (v0.8.1): 1 lists just this folder (the original behavior);
        # higher values walk down that many levels. The walk lives in code,
        # the model only chooses how deep. Entries come back as
        # workspace-relative paths (folders with a trailing /), so every
        # line is a valid argument to the other tools — listing a subdir
        # used to return bare names that no tool could use as-is.
        try:
            depth = int(depth)
        except (TypeError, ValueError):
            raise ToolError(f"depth must be an integer, got: {depth!r}")
        if depth < 1 or depth > 10:
            raise ToolError(f"depth must be between 1 and 10, got: {depth}")
        p = jail.resolve(path)
        if not p.is_dir():
            raise ToolError(f"no such directory: {path!r}")
        max_entries = 500
        entries: list[str] = []
        capped = False

        def display(rel_to_base: Path) -> str:
            return (p / rel_to_base).relative_to(jail.root).as_posix()

        for root, dirnames, filenames in os.walk(p):
            rel_root = Path(root).relative_to(p)
            dirnames.sort()
            descend = []
            for name in dirnames:
                if name == ".harness":
                    continue  # harness bookkeeping (run records), not user data
                rel = name if str(rel_root) == "." else f"{rel_root.as_posix()}/{name}"
                if len(Path(rel).parts) <= depth:
                    entries.append(display(Path(rel)) + "/")
                    if len(entries) >= max_entries:
                        capped = True
                        break
                child = Path(root) / name
                # Symlinked folders are listed but never followed: following
                # one could walk straight out of the jail.
                if not child.is_symlink() and len(Path(rel).parts) < depth:
                    descend.append(name)
            dirnames[:] = descend
            if capped:
                break
            for name in sorted(filenames):
                rel = name if str(rel_root) == "." else f"{rel_root.as_posix()}/{name}"
                if len(Path(rel).parts) <= depth:
                    entries.append(display(Path(rel)))
                    if len(entries) >= max_entries:
                        capped = True
                        break
            if capped:
                dirnames[:] = []
                break
        entries.sort()
        if capped:
            entries.append(f"...[listing truncated at {max_entries} entries]")
        return _truncate("\n".join(entries) or "(empty)", max_output_chars)

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
        {"name": "append_file", "description":
         "Append content to the end of a text file, creating the file if it does "
         "not exist. Use this when adding to a file that already has content, "
         "such as an index, a list, or a log: unlike write_file, which replaces "
         "the whole file, append_file keeps everything already there. The "
         "content is added exactly as given, so include any line breaks you "
         "want at the start or end of your content.",
         "parameters": {"type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "content": {"type": "string"}},
                        "required": ["path", "content"]},
         "func": lambda a: t_append_file(a["path"], a["content"])},
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
         "List files and directories under a workspace path, as workspace-relative "
         "paths you can pass to the other tools. depth 1 lists just that folder "
         "(default); raise depth to also see inside subfolders, e.g. depth 3.",
         "parameters": {"type": "object",
                        "properties": {"path": {"type": "string", "default": "."},
                                       "depth": {"type": "integer", "default": 1,
                                                 "description": "Levels to list: 1 = "
                                                 "just this folder, 2 = plus its "
                                                 "subfolders' contents, up to 10."}},
                        "required": []},
         "func": lambda a: t_list_dir(a.get("path", "."), a.get("depth", 1))},
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
