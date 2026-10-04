#!/usr/bin/env python3
"""CLI entry point.

Modes:
  python3 run.py "task"              direct loop (no planning)
  python3 run.py "task" --plan       propose a Gherkin plan -> plan.feature, stop for review
  python3 run.py "task" --discover   read-only discovery pass, then propose the plan -> plan.feature
  python3 run.py --run plan.feature  execute an approved plan
  python3 run.py "task" --auto       plan and execute in one go (no approval)
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import config, gherkin, graphcheck as _graphcheck, loop
from harness import verify as _verify
from harness import planner as _planner


def _then_lines(steps):
    """Yield (step, then_text, verifier_name_or_None) for the approval display."""
    for s in steps:
        for t in (s.done_when or "").split("\n"):
            t = t.strip()
            if t:
                yield s, t, _verify.match_verifier(t)


def _show_lint(steps):
    """Print deterministic plan-lint findings at approval time."""
    items = [(s.id, f) for s in steps for f in (s.lint or [])]
    if not items:
        print("Plan lint: clean — no design flaws found.")
        return
    print("Plan lint findings (deterministic, no model calls):")
    for sid, f in items:
        tag = "ERROR" if f["level"] == "error" else "warn"
        print(f"  step {sid} [{tag} {f['code']}]: {f['detail']}")


def _show_graph(task, steps, workspace):
    """Print the plan's data-flow graph at approval time (prototype).

    Display-only supplement to the lint findings: the graph shows what
    each step consumes and produces, and the graph checker's findings
    flag edges that point nowhere, point backward, or draw on a field
    the producer never makes. Nothing here blocks approval."""
    graph = _graphcheck.build_graph(task, steps, workspace_files=workspace)
    findings = _graphcheck.check_plan(task, steps, workspace_files=workspace)
    print("Plan graph (prototype, deterministic):")
    for n in graph.nodes:
        ins = ", ".join(n.inputs) if n.inputs else "nothing"
        outs = ", ".join(n.outputs) if n.outputs else "nothing"
        print(f"  step {n.step_id}: in [{ins}] -> out [{outs}]")
    if not findings:
        print("  graph check: clean, every input has a producer.")
    for f in findings:
        tag = "ERROR" if f.level == "error" else "warn"
        print(f"  step {f.step_id} [{tag} {f.code}]: {f.detail}")


def show_event(kind, *args, quiet=False):
    if quiet:
        return
    if kind == "plan":
        steps = args[0]
        print(f"plan ({len(steps)} steps):")
        for s in steps:
            budget = f" [budget: {s.budget} turns]" if getattr(s, "budget", 0) else ""
            print(f"  {s.id}. {s.instruction} [done when: {s.done_when}]{budget}")
    elif kind == "step_start":
        print(f"\n=== step {args[0].id}: {args[0].instruction} ===")
    elif kind == "step_sub":
        sub = args[1]
        print(f"  - sub-step {sub.n} [{sub.path}]", end="")
        if sub.tool:
            print(f" > {sub.tool}", end="")
        print()
        if sub.thought:
            print("   ", sub.thought[:200].replace("\n", " "))
    elif kind == "step_verify":
        step, vres = args[0], args[1]
        auto = [c for c in vres.checks if c.verifier != "attest"]
        att = len(vres.checks) - len(auto)
        bits = []
        if auto:
            bits.append("verified: " + ", ".join(c.verifier for c in auto))
        if att:
            bits.append(f"{att} attested")
        print(f"  check step {step.id}: {'ok' if vres.ok else 'FAILED'}"
              + (f" ({'; '.join(bits)})" if bits else ""))
        if not vres.ok:
            for c in vres.checks:
                if not c.ok:
                    print(f"    ! {c.detail}")
    elif kind == "step_done":
        tag = " done (verification warnings waived)." if args[0].verify_waived else " done."
        print(f"  step {args[0].id}{tag}")
        if getattr(args[0], "output_path", ""):
            print(f"    output: {args[0].output_path}")
    elif kind == "step_verify_waived":
        step, vres = args[0], args[1]
        print(f"  ! step {step.id}: verification failed twice — continuing with warning:")
        for c in vres.checks:
            if not c.ok:
                print(f"    ! {c.detail}")
    elif kind == "step_failed":
        print(f"  step {args[0].id} FAILED.")


def main() -> int:
    cfg, args = config.from_args()
    Path(cfg.workspace).mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")

    if args.run:
        return run_feature(cfg, args.run, stamp)
    task = args.task
    if args.discover:
        return run_discover(cfg, task)
    if args.plan:
        return propose_plan(cfg, task)
    if args.auto:
        return run_auto(cfg, task, stamp)
    return run_direct(cfg, task, stamp)


def run_discover(cfg, task) -> int:
    """Tier 2: read-only discovery pass first, then propose the plan with
    the findings in hand. Stops at plan.feature for review, like --plan."""
    print("discovery pass (read-only tools only):")
    text, result, info = _planner.plan_with_discovery(
        task, cfg, on_event=show_event)
    if info.get("discovery_run_id"):
        print(f"discovery records: .harness/runs/{info['discovery_run_id']} "
              f"(status: {info['discovery_status']})\n")
    if text is None:
        print(f"could not produce a valid plan: {result}")
        return 2
    return propose_plan(cfg, task, planned=(text, result))


def propose_plan(cfg, task, planned=None) -> int:
    """Ask the model for a Gherkin plan, write plan.feature, stop for review."""
    text, result = planned if planned is not None else _planner.propose(task, cfg)
    if text is None:
        print(f"could not produce a valid plan: {result}")
        return 2
    plan, err = gherkin.parse_feature(text)
    canonical = gherkin.render_feature(plan.title, plan.steps)
    path = Path("plan.feature")
    path.write_text(canonical)
    print(f"plan written to {path} — review it, edit if needed, then run:")
    print(f"  python3 run.py --run {path}\n")
    print("Read the Then lines first: they define what 'done' means for each step.")
    print("Badges show what the harness verifies itself vs takes on trust:\n")
    for s, t, v in _then_lines(result):
        badge = f"[checks itself: {v}]" if v else "[on trust]"
        print(f"  step {s.id}: Then {t} {badge}")
    _show_lint(result)
    _show_graph(task, result, cfg.workspace)
    print()
    print(canonical)
    return 0


def run_feature(cfg, path, stamp) -> int:
    """Parse, validate, and execute an approved .feature plan."""
    try:
        text = Path(path).read_text()
    except OSError as e:
        print(f"cannot read {path}: {e}")
        return 2
    plan, err = gherkin.parse_feature(text)
    if plan is None:
        print(f"rejected {path}: {err}")
        return 2
    steps = _planner.to_plan_steps(plan)
    if not cfg.quiet:
        print(f"approved plan: {plan.title} ({len(steps)} steps)")
        for s in steps:
            print(f"  {s.id}. {s.instruction}")
        print("\nThen clauses (what the harness checks itself vs takes on trust):")
        for s, t, v in _then_lines(steps):
            badge = f"[checks itself: {v}]" if v else "[on trust]"
            print(f"  step {s.id}: {t} {badge}")
        print()
    result = _planner.execute_plan(
        plan.title, steps, cfg,
        on_event=lambda k, *a: show_event(k, *a, quiet=cfg.quiet))
    if not cfg.quiet:
        print(f"\n== {result.status} ==")
    print(result.answer)
    return 0 if result.status == "done" else 1


def run_auto(cfg, task, stamp) -> int:
    """Plan and execute in one go (no approval step)."""
    if not cfg.quiet:
        print(f"model={cfg.model} endpoint={cfg.endpoint} workspace={cfg.workspace}")
        print(f"task: {task}\n")
    result = _planner.run_planned(
        task, cfg, on_event=lambda k, *a: show_event(k, *a, quiet=cfg.quiet))
    plan_path = cfg.trace_path or f"plan-{stamp}.json"
    if cfg.trace_path and not cfg.trace_path.endswith(".json"):
        plan_path = cfg.trace_path + ".plan.json"
    with open(plan_path, "w") as f:
        json.dump({"task": task, "model": cfg.model, "status": result.status,
                   **result.plan_dict()}, f, indent=2)
    if not cfg.quiet:
        print(f"\nplan: {plan_path}")
        print(f"== {result.status} ==")
    print(result.answer)
    return 0 if result.status == "done" else 1


def run_direct(cfg, task, stamp) -> int:
    def show(step: loop.Step):
        if cfg.quiet:
            return
        print(f"\n--- step {step.n} [{step.path}] ---")
        if step.thought:
            print(step.thought[:500])
        if step.tool:
            print(f"> {step.tool} {step.args}")
            res = step.result or ""
            print(res[:800] + ("..." if len(res) > 800 else ""))

    if not cfg.quiet:
        print(f"model={cfg.model} endpoint={cfg.endpoint} workspace={cfg.workspace}")
        print(f"task: {task}\n")

    result = loop.run(task, cfg, on_step=show)

    trace = cfg.trace_path or f"trace-{stamp}.jsonl"
    loop.write_trace(trace, task, cfg, result)
    if not cfg.quiet:
        print(f"\ntrace: {trace}")
        print(f"\n== {result.status} "
              f"(native={result.native_calls} fallback={result.fallback_calls} "
              f"steps={len(result.steps)}) ==")
    print(result.answer)
    return 0 if result.status == "done" else 1


if __name__ == "__main__":
    sys.exit(main())
