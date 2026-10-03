"""v0.8.1: step-output store (issue #2).

Plan steps used to lose data produced by earlier steps: only a 250-char
summary of each step's DONE: prose crossed the boundary, and tool outputs
(the actual file list, the actual command output) never did. A step that
listed files in step 1 arrived at step 2 with no list, and failed silently.

The fix keeps storage and access separate, and puts both in the harness:

- Storage: after every step, the harness (never the model) writes the
  step's full record to a deterministic file:

      <workspace>/.harness/runs/<YYYYMMDD-HHMMSS>/step-NN.md
      <workspace>/.harness/latest.txt   newest run folder name

  The record holds the instruction, the Then clauses, every tool call
  with its result verbatim, the DONE: answer, and the verification
  outcome. Same-second runs collide safely: -2, -3, ...

- Access: each step's prompt names the exact prior-step files, so the
  model can read_file the one it needs. No new tool: read_file already
  exists, and naming exact paths means the model never guesses a
  filename it has not seen (the grep_files lesson).

latest.txt is the continuation hook: a later session (or a human) can
find the most recent run without scanning timestamps.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path


def make_run_id(now: datetime | None = None) -> str:
    """Run folder name from local time: YYYYMMDD-HHMMSS.

    Sorts lexicographically in chronological order, has no colons or
    spaces, and is safe on every filesystem the harness runs on."""
    return (now or datetime.now()).strftime("%Y%m%d-%H%M%S")


class RunStore:
    """Owns one run's folder under <workspace>/.harness/runs/."""

    def __init__(self, workspace: str | Path, now: datetime | None = None,
                 run_id: str | None = None):
        self.workspace = Path(workspace).resolve()
        self.harness_dir = self.workspace / ".harness"
        self.runs_dir = self.harness_dir / "runs"
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        base = run_id or make_run_id(now)
        candidate, n = base, 2
        while (self.runs_dir / candidate).exists():
            candidate = f"{base}-{n}"
            n += 1
        self.run_id = candidate
        self.run_dir = self.runs_dir / self.run_id
        self.run_dir.mkdir(parents=True, exist_ok=False)
        # Continuation hook: newest run wins.
        (self.harness_dir / "latest.txt").write_text(self.run_id + "\n")

    def rel_path(self, step_id: int) -> str:
        """Workspace-relative path of a step file, POSIX separators."""
        return f".harness/runs/{self.run_id}/step-{step_id:02d}.md"

    def step_path(self, step_id: int) -> Path:
        return self.run_dir / f"step-{step_id:02d}.md"

    def prior_paths(self, step_id: int) -> list[str]:
        """Exact relative paths of all step files before step_id."""
        return [self.rel_path(i) for i in range(1, step_id)]

    def write_step(self, step_id: int, instruction: str, given: str,
                   done_when: str, answer: str, sub_steps: list,
                   status: str, verify_checks: list | None = None,
                   verify_waived: bool = False,
                   attempts: list | None = None) -> Path:
        """Write one step's record. Deterministic, harness-written.

        `sub_steps` are harness.loop.Step objects from the (final) sub-run;
        `attempts`, when given, is a list of per-attempt dicts with keys
        sub_steps / answer / verify_checks so verification retries are all
        preserved, not just the last attempt."""
        out: list[str] = [f"# Step {step_id}", ""]
        out += ["## Instruction", instruction or "(none)", ""]
        if given:
            out += ["## Given", given, ""]
        out += ["## Done when", done_when or "(instruction complete)", ""]
        out += ["## Status", status + (" (verification waived)" if verify_waived else ""), ""]

        rounds = attempts if attempts else [
            {"sub_steps": sub_steps, "answer": answer,
             "verify_checks": verify_checks or []}]
        for idx, att in enumerate(rounds, 1):
            if len(rounds) > 1:
                out += [f"## Attempt {idx}", ""]
            out += ["### Tool calls", ""]
            calls = [s for s in (att.get("sub_steps") or []) if s.tool]
            if not calls:
                out += ["(no tool calls)", ""]
            for s in calls:
                args = json.dumps(s.args, default=str) if isinstance(s.args, dict) else str(s.args or "")
                out += [f"#### {s.tool} {args}", "", "Result:", "", "```",
                        s.result if s.result is not None else "", "```", ""]
            out += ["### Answer", "", att.get("answer") or "(no answer)", ""]
            checks = att.get("verify_checks") or []
            out += ["### Verification", ""]
            if checks:
                for c in checks:
                    mark = "ok" if c.get("ok") else "FAILED"
                    out.append(f"- [{mark}] ({c.get('verifier', 'attest')}) "
                               f"{c.get('then', '')}: {c.get('detail', '')}")
            else:
                out.append("- (not verified)")
            out.append("")

        path = self.step_path(step_id)
        path.write_text("\n".join(out))
        return path
