"""Configuration for the harness. Pure stdlib, no dependencies."""
from __future__ import annotations

import argparse
import os
from dataclasses import dataclass, field


@dataclass
class Config:
    model: str = "gemma4:e2b"
    endpoint: str = "http://localhost:11434"
    workspace: str = "./sandbox"
    max_steps: int = 12
    temperature: float = 0.2
    top_p: float = 0.9
    step_max_steps: int = 6  # per-step cap in plan mode
    verify_retries: int = 1  # per-step Then-verification retries (v0.7)
    request_timeout: int = 180
    exec_timeout: int = 30
    max_output_chars: int = 2000
    max_consecutive_errors: int = 3
    trace_path: str | None = None
    quiet: bool = False
    # Safety: shell commands matching any of these substrings are refused.
    blocked_substrings: tuple = field(default_factory=lambda: (
        "rm -rf /", "rm -rf ~", "mkfs", ":(){", "dd if=", "dd of=/dev",
        "shutdown", "reboot", "halt", "poweroff", "> /dev/sd", "curl | sh",
        "wget | sh",
    ))


def from_args(argv: list[str] | None = None) -> tuple[Config, str]:
    p = argparse.ArgumentParser(
        description="Minimal agent harness for small local models via Ollama.")
    p.add_argument("task", nargs="?", help="Task for the agent.")
    p.add_argument("--task-file", help="Read the task from a file.")
    p.add_argument("--model", default=os.environ.get("HARNESS_MODEL", "gemma4:e2b"))
    p.add_argument("--endpoint", default=os.environ.get("HARNESS_ENDPOINT", "http://localhost:11434"))
    p.add_argument("--workspace", default=os.environ.get("HARNESS_WORKSPACE", "./sandbox"))
    p.add_argument("--max-steps", type=int, default=12)
    p.add_argument("--step-max-steps", type=int, default=6,
                   help="Max steps per plan step in --plan mode.")
    p.add_argument("--plan", action="store_true",
                   help="Propose a Gherkin plan, write it to plan.feature, and stop "
                        "for human review/editing. Does not execute.")
    p.add_argument("--run", metavar="FEATURE",
                   help="Execute an approved .feature plan file.")
    p.add_argument("--auto", action="store_true",
                   help="Plan and execute in one go (no approval step).")
    p.add_argument("--temperature", type=float, default=0.2)
    p.add_argument("--trace", default=None, help="Write step trace JSONL here.")
    p.add_argument("--quiet", action="store_true", help="Only print the final answer.")
    args = p.parse_args(argv)

    task = args.task
    if args.task_file:
        with open(args.task_file) as f:
            task = f.read().strip()
    if not task and not args.run:
        p.error("provide a task as an argument or via --task-file "
                "(or --run an approved .feature file)")
    # Callers read the task back off the namespace (run.py uses args.task),
    # so the resolved text, file content included, is what they must get.
    # (Field bug 2026-10-03: this hand-back was missing and --task-file
    # silently planned for no task at all.)
    args.task = task

    cfg = Config(
        model=args.model,
        endpoint=args.endpoint.rstrip("/"),
        workspace=args.workspace,
        max_steps=args.max_steps,
        step_max_steps=args.step_max_steps,
        temperature=args.temperature,
        trace_path=args.trace,
        quiet=args.quiet,
    )
    return cfg, args
