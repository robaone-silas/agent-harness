#!/usr/bin/env python3
"""Tests for issue #32: step_budget() missed plural and range step
references, so a compile step drawing on "steps 1 through 5" kept
the flat budget cap.

Field failure, daily persona run 2026-10-05: Tomas Rivera's plan
read five household records in steps 1 to 5, then step 6 compiled
them "from steps 1 through 5". step_budget() found earlier-step
references with the pattern `step\\s+(\\d+)`, which matches only the
singular form: the plural "steps" fails it and the range end "5"
carries no prefix, so step 6 counted no prior steps at all and got
the flat base of 6 turns for work needing at least 7 (five record
reads, the write, the final answer). The step died against the cap
with its deliverable unwritten. Marcus Bennett's compile step the
same day, phrased with singular references ("step 1", "step 2"),
computed 9 and passed: the gap is the phrasing, not the work.

The counter now recognizes the plural and range forms a planner
writes naturally: "steps 1 through 5", "steps 1 to 5", "steps 1-5",
"steps 1, 2, and 3", "steps 1 and 2", and a range hanging off a
singular head ("step 1 through 5"). Each referenced prior step
counts once, exactly as a singular reference does.

Run: python3 tests/test_plural_step_refs.py
"""
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness import gherkin, planner
from harness.config import Config

SOURCES = ["house-insurance-policy.txt", "property-tax-bill.txt",
           "roof-warranty.txt", "furnace-receipt.txt",
           "water-heater-manual.txt"]


def make_cfg(d, **kw):
    base = dict(workspace=d, max_steps=12, step_max_steps=6,
                max_consecutive_errors=3)
    base.update(kw)
    return Config(**base)


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name
          + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        raise AssertionError(name)


def parse(text):
    plan, err = gherkin.parse_feature(text)
    assert plan is not None, err
    return planner.to_plan_steps(plan)


def tomas_plan(draw_phrase, n=5):
    """n single-read steps plus a compile step drawing on them via
    draw_phrase (for example "steps 1 through 5")."""
    parts = ["Feature: Make a list of my house papers"]
    for i, name in enumerate(SOURCES[:n], start=1):
        parts.append(f"""  Scenario: Step {i} - Read {name}
    When I read "{name}"
    Then I know the details contained within {name}""")
    parts.append(f"""  Scenario: Step {n + 1} - Compile the house paper list
    When I compile a list of all house papers and their purposes from {draw_phrase} and write the result to house_papers_list.txt
    Then "house_papers_list.txt" contains a list of all house papers and their purposes""")
    return "\n".join(parts)


def compile_budget(draw_phrase, items_per_prior=0, n=5):
    steps = parse(tomas_plan(draw_phrase, n=n))
    for s in steps[:-1]:
        s.source_items = [f"item-{k}" for k in range(items_per_prior)]
    cfg = make_cfg("unused")
    return planner.step_budget(steps[-1], steps, cfg)


def t_plural_range_counts_every_prior_step():
    b = compile_budget("steps 1 through 5")
    # 1 named operand + 5 record reads + slack 2 = 8; the issue's bar
    # is that the counted budget covers 5 reads, the write, and the
    # final answer (at least 7).
    check("plural range budget covers the planned work", b >= 7, str(b))
    check("plural range budget is the counted value", b == 8, str(b))
    b = compile_budget("steps 1 through 5", items_per_prior=1)
    check("plural range scales with source items", b == 13, str(b))


def t_plural_range_spellings():
    for phrase in ("steps 1 to 5", "steps 1-5",
                   "steps 1, 2, 3, 4, and 5", "step 1 through 5"):
        b = compile_budget(phrase)
        check(f"range spelling counts: {phrase}", b == 8, str(b))


def t_plural_pair_matches_singular_pair():
    plural = compile_budget("steps 1 and 2", items_per_prior=2, n=2)
    singular = compile_budget("step 1 and step 2", items_per_prior=2, n=2)
    check("plural pair budget is counted", plural == 9, str(plural))
    check("plural and singular phrasings agree",
          plural == singular, f"{plural} vs {singular}")


def t_unrecognized_and_forward_refs_count_nothing():
    b = compile_budget("the previous steps")
    check("prose without numbers keeps the flat base", b == 6, str(b))
    steps = parse(tomas_plan("steps 1 through 9"))
    cfg = make_cfg("unused")
    b = planner.step_budget(steps[-1], steps, cfg)
    check("range past the plan counts only real priors", b == 8, str(b))
    steps = parse("""Feature: Forward references
  Scenario: Step 1 - Plan ahead
    When I prepare from steps 2 through 3 and write "plan.txt"
    Then "plan.txt" exists
  Scenario: Step 2 - Later work
    When I write "later.txt"
    Then "later.txt" exists
  Scenario: Step 3 - Last work
    When I write "last.txt"
    Then "last.txt" exists
""")
    b = planner.step_budget(steps[0], steps, cfg)
    check("references to later steps count nothing", b == 6, str(b))


def t_replay_tomas_shape_completes():
    d = tempfile.mkdtemp(prefix="plural-refs-")
    try:
        for name in SOURCES:
            Path(d, name).write_text(f"contents of {name}")
        state = {"task_seen": None, "phase": 0}

        def tool(name, args):
            return {"role": "assistant", "content": "",
                    "tool_calls": [{"function": {"name": name,
                                                 "arguments": args}}]}

        def fake(messages, tool_defs):
            if tool_defs is None:
                return {"role": "assistant", "content": "DONE: ok."}
            task_msg = messages[1]["content"] if len(messages) > 1 else ""
            if task_msg != state["task_seen"]:
                state["task_seen"] = task_msg
                state["phase"] = 0
            phase = state["phase"]
            state["phase"] += 1
            for i, name in enumerate(SOURCES, start=1):
                if f"You are executing step {i} " in task_msg or \
                        f"You are executing step {i}:" in task_msg:
                    if phase == 0:
                        return tool("read_file", {"path": name})
                    return {"role": "assistant", "content": "DONE: read."}
            # Step 6: read the five step records, write, answer DONE.
            # Seven turns of planned work; the flat cap of 6 kills it.
            latest = Path(d, ".harness", "latest.txt").read_text().strip()
            records = [str(Path(d, ".harness", "runs", latest,
                                f"step-0{i}.md")) for i in range(1, 6)]
            script = [tool("read_file", {"path": p}) for p in records]
            script.append(tool("write_file", {
                "path": "house_papers_list.txt",
                "content": "\n".join(f"{n}: contents of {n}"
                                     for n in SOURCES)}))
            script.append({"role": "assistant",
                           "content": "DONE: list written."})
            return script[min(phase, len(script) - 1)]

        cfg = make_cfg(d)
        r = planner.execute_plan("make a list of my house papers",
                                 parse(tomas_plan("steps 1 through 5")),
                                 cfg, chat_fn=fake)
        check("replay plan completes", r.status == "done", r.status)
        check("compile step budget covered its planned turns",
              (r.steps[-1].budget or 0) >= 7, str(r.steps[-1].budget))
        check("the deliverable was written",
              Path(d, "house_papers_list.txt").exists())
    finally:
        shutil.rmtree(d)


def main():
    t_plural_range_counts_every_prior_step()
    t_plural_range_spellings()
    t_plural_pair_matches_singular_pair()
    t_unrecognized_and_forward_refs_count_nothing()
    t_replay_tomas_shape_completes()
    print("\nAll plural step reference budget tests passed.")


if __name__ == "__main__":
    main()
