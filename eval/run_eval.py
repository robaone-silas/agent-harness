#!/usr/bin/env python3
"""Micro-eval for the harness: 5 tiny tasks, each with a programmatic checker.

Run against a real model:
    python3 eval/run_eval.py --model gemma4:e2b

Each task runs in a fresh sandbox under ./eval-sandboxes/<task>/. A task passes
only if its checker verifies the outcome on disk — no LLM-judged grading.

Checker message convention: every failure states what was found AND what was
expected, in plain words. These messages double as TDD retry feedback, so they
must disambiguate value vs. formatting vs. whitespace — "wrong sum: '100.0'"
once made a model trim a newline instead of dropping the ".0".
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness import config, loop


def _setup_write(sbox: Path):
    pass

def _check_write(sbox: Path):
    p = sbox / "hello.txt"
    if not p.is_file():
        return False, "hello.txt not created"
    if p.read_text().strip() != "Hello, harness!":
        return False, (f"wrong content: got {p.read_text()!r}, "
                       f"expected exactly 'Hello, harness!'")
    return True, "ok"


def _setup_csv(sbox: Path):
    (sbox / "data.csv").write_text("item,amount\napples,30\npears,45\nplums,25\n")

def _check_csv(sbox: Path):
    p = sbox / "total.txt"
    if not p.is_file():
        return False, "total.txt not created"
    if p.read_text().strip() != "100":
        return False, (f"wrong sum: got {p.read_text()!r}, expected exactly '100' "
                       f"— the plain integer, no decimal point, no extra text")
    return True, "ok"


def _setup_search(sbox: Path):
    (sbox / "a.txt").write_text("the quick brown fox")
    (sbox / "b.txt").write_text("I love pineapple on pizza")
    (sbox / "c.txt").write_text("nothing to see here")

def _check_search(sbox: Path):
    p = sbox / "found.txt"
    if not p.is_file():
        return False, "found.txt not created"
    if "b.txt" not in p.read_text():
        return False, (f"wrong file named: got {p.read_text()!r}, expected 'b.txt' "
                       f"— the .txt file containing the word 'pineapple'")
    return True, "ok"


def _setup_bug(sbox: Path):
    (sbox / "broken.py").write_text("print('OK'\n")  # missing paren

def _check_bug(sbox: Path):
    proc = subprocess.run([sys.executable, "broken.py"], cwd=sbox,
                          capture_output=True, text=True, timeout=15)
    if proc.returncode != 0:
        return False, (f"broken.py still fails to run: {proc.stderr[:200]} "
                       f"— expected it to run cleanly and print OK")
    if "OK" not in proc.stdout:
        return False, (f"wrong output: got {proc.stdout!r}, "
                       f"expected output containing 'OK'")
    return True, "ok"


def _setup_multi(sbox: Path):
    pass

def _check_multi(sbox: Path):
    main, out = sbox / "src" / "main.py", sbox / "out.txt"
    if not main.is_file():
        return False, "src/main.py not created"
    if not out.is_file():
        return False, "out.txt not created"
    proc = subprocess.run([sys.executable, str(main)], capture_output=True,
                          text=True, timeout=15)
    if proc.stdout.strip() != out.read_text().strip():
        return False, (f"out.txt does not match main.py output: "
                       f"out.txt contains {out.read_text().strip()!r} but running "
                       f"main.py produces {proc.stdout.strip()!r} — they must match exactly")
    # Intent check (code-smell pass): the filename must be computed, not hardcoded.
    # Run a renamed copy elsewhere; a genuine "prints its own filename" changes
    # its output, while print("<literal>") does not.
    with tempfile.TemporaryDirectory(prefix="smell-") as tmp:
        alt = Path(tmp) / "renamed_check.py"
        alt.write_text(main.read_text())
        proc2 = subprocess.run([sys.executable, str(alt)], capture_output=True,
                               text=True, timeout=15)
    if proc2.returncode != 0:
        return False, (f"smell: the renamed copy fails to run: "
                       f"{proc2.stderr[:200]} — the script should work under any filename")
    if proc2.stdout.strip() == proc.stdout.strip():
        return False, (f"smell: main.py prints {proc.stdout.strip()!r} even when run as "
                       f"renamed_check.py — the filename looks hardcoded, not computed. "
                       f"Compute it from __file__ or sys.argv instead of writing the literal.")
    return True, "ok"


TASKS = [
    {"name": "write-file",
     "task": "Create a file named hello.txt containing exactly the text: Hello, harness!",
     "setup": _setup_write, "check": _check_write},
    {"name": "read-and-compute",
     "task": "Read data.csv, sum the 'amount' column, and write just the number to total.txt",
     "setup": _setup_csv, "check": _check_csv},
    {"name": "search",
     "task": "Find which .txt file in the workspace mentions 'pineapple' and write its filename to found.txt",
     "setup": _setup_search, "check": _check_search},
    {"name": "fix-bug",
     "task": "Fix broken.py so that running 'python3 broken.py' prints OK",
     "setup": _setup_bug, "check": _check_bug},
    {"name": "multi-step",
     "task": ("Create a src/ directory. Inside it, write main.py that prints its own filename. "
              "Run it with python3 and save its output to out.txt in the workspace root."),
     "setup": _setup_multi, "check": _check_multi},
]


def _workspace_snapshot(sbox: Path, max_entries: int = 40) -> str:
    """Top-level workspace listing, so a retry knows what already exists."""
    try:
        names = sorted(p.name + ("/" if p.is_dir() else "") for p in sbox.iterdir())
    except OSError:
        return "(unreadable)"
    if not names:
        return "(empty)"
    if len(names) > max_entries:
        names = names[:max_entries] + ["..."]
    return ", ".join(names)


def _retry_task_text(task: str, failure_msg: str, sbox: Path) -> str:
    """Build the TDD retry prompt: original task + checker feedback + workspace
    state. The model must know its previous work is still on disk, so it plans
    the fix idempotently instead of recreating what already succeeded
    (e.g. `mkdir` on an existing directory)."""
    return (task
            + "\n\nPREVIOUS ATTEMPT FAILED the acceptance check: "
            + failure_msg
            + "\nThe workspace still contains everything from your previous attempt "
              "— files and directories you created are still there. "
              f"Current workspace contents: {_workspace_snapshot(sbox)}."
              "\nDo NOT redo work that already succeeded. Plan only the fix: "
              "inspect what exists, prefer idempotent operations "
              "(e.g. `mkdir -p`, overwrite files instead of recreating them), "
              "and address exactly the checker's complaint above.")


def main() -> int:
    import argparse, os
    p = argparse.ArgumentParser(description="Micro-eval for the agent harness.")
    p.add_argument("--model", default=os.environ.get("HARNESS_MODEL", "gemma4:e2b"))
    p.add_argument("--endpoint", default=os.environ.get("HARNESS_ENDPOINT", "http://localhost:11434"))
    p.add_argument("--max-steps", type=int, default=12)
    p.add_argument("--temperature", type=float, default=0.2)
    p.add_argument("--plan", action="store_true",
                   help="Plan-then-execute mode instead of the direct loop.")
    p.add_argument("--step-max-steps", type=int, default=6)
    p.add_argument("--attempts", type=int, default=1,
                   help="TDD-style attempts per task: after a failed acceptance "
                        "check, retry with the checker's failure message as "
                        "feedback, keeping the workspace as-is.")
    args = p.parse_args()
    cfg = config.Config(model=args.model, endpoint=args.endpoint.rstrip("/"),
                        max_steps=args.max_steps, temperature=args.temperature,
                        step_max_steps=args.step_max_steps, quiet=True)
    plan_mode = args.plan
    base = Path("eval-sandboxes")
    stamp = __import__("time").strftime("%Y%m%d-%H%M%S")
    results_path = Path(f"eval-results-{stamp}.jsonl")
    print(f"results log: {results_path} | attempts per task: {args.attempts}",
          flush=True)
    passed, failed = [], []
    for t in TASKS:
        sbox = base / t["name"]
        task_text = t["task"]
        ok, msg, attempts_used = False, "", 0
        for attempt in range(1, args.attempts + 1):
            attempts_used = attempt
            if attempt == 1:
                shutil.rmtree(sbox, ignore_errors=True)
                sbox.mkdir(parents=True)
                t["setup"](sbox)
            else:
                # TDD red-green: keep the workspace as the model left it and
                # tell it exactly what the acceptance check rejected, plus what
                # already exists so it plans the fix idempotently.
                task_text = _retry_task_text(t["task"], msg, sbox)
            cfg.workspace = str(sbox)
            print(f"--- {t['name']} (attempt {attempt}) ---", flush=True)
            try:
                if plan_mode:
                    from harness import planner as _planner
                    r = _planner.run_planned(task_text, cfg)
                    detail_src = f"status={r.status} plan_steps={len(r.steps)}"
                    record = {"task": t["name"], "mode": "plan", "status": r.status,
                              "attempt": attempt,
                              "plan": [{"id": s.id, "instruction": s.instruction,
                                        "done_when": s.done_when, "status": s.status,
                                        "result": s.result[:300],
                                        "verify": s.verify, "verify_waived": s.verify_waived,
                                        "lint": s.lint} for s in r.steps]}
                else:
                    r = loop.run(task_text, cfg)
                    detail_src = (f"status={r.status} native={r.native_calls} "
                                  f"fallback={r.fallback_calls} steps={len(r.steps)}")
                    record = {"task": t["name"], "mode": "direct", "status": r.status,
                              "attempt": attempt,
                              "native": r.native_calls, "fallback": r.fallback_calls,
                              "steps": len(r.steps)}
                ok, msg = t["check"](sbox)
                record.update({"ok": ok, "msg": msg})
                detail = f"{msg} | {detail_src}"
            except Exception as e:
                ok, detail = False, f"harness crashed: {e}"
                record = {"task": t["name"], "attempt": attempt,
                          "ok": False, "msg": detail}
            with open(results_path, "a") as f:
                f.write(__import__("json").dumps(record) + "\n")
            print(("PASS " if ok else "FAIL ") + detail, flush=True)
            if ok:
                break
        print(f"== {t['name']}: {'PASS' if ok else 'FAIL'} "
              f"after {attempts_used} attempt(s) ==", flush=True)
        (passed if ok else failed).append(t["name"])
    print(f"\n{len(passed)}/{len(TASKS)} passed: {passed}" +
          (f" | failed: {failed}" if failed else ""))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
