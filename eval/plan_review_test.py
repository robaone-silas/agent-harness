"""Test candidate plan-review prompts against e2b.

A plan-review gate would run BEFORE the plan reaches the user for approval,
catching design errors (hallucinated values, hardcoded implementations,
incoherent decomposition). This script tests 4 prompt styles against 2 known-bad
and 2 known-good plans from the eval logs, using gemma4:e2b as the reviewer.

Usage: python3 eval/plan_review_test.py   (writes plan-review-results.jsonl)
"""
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Route model traffic through the local proxy (set HARNESS_ENDPOINT for the model server).
_p = os.environ.get("HTTPS_PROXY", "")
if _p and ":3130" not in _p:
    _p = _p.rsplit(":", 1)[0] + ":3130" if ":" in _p else _p
    for _k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        os.environ[_k] = _p
for _k in ("all_proxy", "no_proxy", "ALL_PROXY"):
    os.environ.pop(_k, None)

from harness.client import chat  # noqa: E402

ENDPOINT = os.environ.get("HARNESS_ENDPOINT", "http://localhost:11434")
MODEL = "gemma4:e2b"

PLANS = {
    "BAD-1": {
        "task": "Read data.csv, sum the 'amount' column, and write just the number to total.txt",
        "plan": (
            "Step 1: When I execute `awk -F',' 'NR>1 {sum += $2} END {print sum}' data.csv`\n"
            "  Then: the file `total.txt` should not exist\n"
            "Step 2: When I execute `echo 12345 > total.txt`\n"
            "  Then: the file `total.txt` should contain exactly `12345`"
        ),
        "want": "REJECT",
        "flaws": "hallucinated value 12345; incoherent: step 1 computes but step 2 ignores it",
    },
    "BAD-2": {
        "task": ("Create a src/ directory. Inside it, write main.py that prints its own "
                 "filename. Run it with python3 and save its output to out.txt in the "
                 "workspace root."),
        "plan": (
            "Step 1: When I create the directory structure\n"
            "  Then: the directory \"src\" exists\n"
            "Step 2: When I write content to \"src/main.py\" with content \"print(\\\"main.py\\\")\"\n"
            "  Then: the file \"src/main.py\" contains exactly \"print(\\\"main.py\\\")\"\n"
            "Step 3: When I execute the command \"python3 src/main.py\" and save the output to \"out.txt\"\n"
            "  Then: the file \"out.txt\" contains exactly \"main.py\""
        ),
        "want": "REJECT",
        "flaws": "hardcoded implementation: plan bakes print(\"main.py\") instead of computing the filename",
    },
    "GOOD-1": {
        "task": "Write the text \"Hello, harness!\" to hello.txt",
        "plan": (
            "Step 1: When I write \"Hello, harness!\" to hello.txt\n"
            "  Then: \"hello.txt\" contains exactly \"Hello, harness!\""
        ),
        "want": "APPROVE",
        "flaws": "none",
    },
    "GOOD-2": {
        "task": "Read data.csv, sum the 'amount' column, and write just the number to total.txt",
        "plan": (
            "Step 1: When I execute `awk -F',' 'NR>1 {sum += $2} END {print sum}' data.csv`\n"
            "  Then: the output of the command contains the total sum of the 'amount' column\n"
            "Step 2: When I write the output of the previous step to total.txt\n"
            "  Then: the file total.txt contains the total sum of the 'amount' column"
        ),
        "want": "APPROVE",
        "flaws": "none (values flow from computation; Then is checkable by recomputing)",
    },
}

PROMPTS = {
    "P1-checklist": (
        "You are reviewing an agent's plan before it runs. REJECT the plan if it has "
        "ANY of these defects:\n"
        "1. HALLUCINATED VALUES: a Then states an exact expected value (number, string, "
        "filename) that cannot be known until the steps run. Values must be computed, not invented.\n"
        "2. HARDCODED IMPLEMENTATION: a When specifies exact code or content that bakes in "
        "the answer instead of describing what the step must achieve.\n"
        "3. INCOHERENT DECOMPOSITION: steps contradict each other, or a Then describes a "
        "state that conflicts with what the plan actually does.\n"
        "4. UNCHECKABLE THEN: a Then so vague that no one could verify it after the step runs.\n"
        "\nTask: {task}\nPlan:\n{plan}\n\n"
        "For each defect type (1-4), write PASS or FAIL with one line of evidence. "
        "Then write your final verdict on its own line, exactly: VERDICT: APPROVE or VERDICT: REJECT"
    ),
    "P2-adversarial": (
        "You are a hostile reviewer. Find every way the plan below could fail to accomplish "
        "the task — invented values, cheating shortcuts, steps that don't connect, acceptance "
        "criteria you couldn't actually check. Assume the plan author is sloppy and be specific "
        "about each flaw. If you genuinely find no flaws, say so.\n"
        "\nTask: {task}\nPlan:\n{plan}\n\n"
        "List each flaw on its own line, or write \"No flaws found.\" "
        "Then write your final verdict on its own line, exactly: VERDICT: APPROVE or VERDICT: REJECT"
    ),
    "P3-operational": (
        "You are checking an agent's plan before execution. For EACH Then clause, write exactly "
        "how you would verify it after the step runs: what file or output you would inspect, and "
        "what you would compare it against. Flag a step if:\n"
        "- its Then needs a value the plan never computes (it just invents it): HALLUCINATED\n"
        "- its When writes exact content that assumes the answer instead of computing it: HARDCODED\n"
        "- its Then is too vague to check: VAGUE\n"
        "\nTask: {task}\nPlan:\n{plan}\n\n"
        "Format per step: Step N — <how you would verify it> — <OK or FLAG: reason>. "
        "Then write your final verdict on its own line, exactly: VERDICT: APPROVE or VERDICT: REJECT"
    ),
    "P4-fewshot": (
        "You review agent plans for design errors. Example of a BAD plan:\n"
        "Task: Count the lines in notes.txt and write the count to count.txt\n"
        "Plan: Step 1: When I guess the file has about 50 lines / Then: count.txt should contain exactly \"50\"\n"
        "Review: REJECT — the Then hallucinates \"50\"; the plan never counts anything.\n"
        "\nExample of a GOOD plan:\n"
        "Task: Count the lines in notes.txt and write the count to count.txt\n"
        "Plan: Step 1: When I run `wc -l < notes.txt` / Then: the command output is a number\n"
        "Step 2: When I write the command output to count.txt / Then: count.txt contains exactly the number from Step 1\n"
        "Review: APPROVE — values flow from computation; each Then is checkable.\n"
        "\nNow review this plan the same way. Quote the offending Then or When if you reject.\n"
        "\nTask: {task}\nPlan:\n{plan}\n\n"
        "Write your final verdict on its own line, exactly: VERDICT: APPROVE or VERDICT: REJECT"
    ),
}


def parse_verdict(text: str) -> str:
    m = re.search(r"VERDICT:\s*(APPROVE|REJECT)", text, re.IGNORECASE)
    if m:
        return m.group(1).upper()
    # fallback: last strong signal in the text
    up = text.upper()
    if "REJECT" in up and "APPROVE" not in up:
        return "REJECT"
    if "APPROVE" in up and "REJECT" not in up:
        return "APPROVE"
    return "UNCLEAR"


def main():
    out = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "plan-review-results.jsonl"), "w")
    total = len(PROMPTS) * len(PLANS)
    n = 0
    for pname, tmpl in PROMPTS.items():
        for plan_name, p in PLANS.items():
            n += 1
            prompt = tmpl.format(task=p["task"], plan=p["plan"])
            print(f"[{n}/{total}] {pname} x {plan_name} ...", flush=True)
            t0 = time.time()
            try:
                msg = chat(ENDPOINT, MODEL,
                           [{"role": "user", "content": prompt}],
                           temperature=0.2, timeout=120)
                content = (msg.get("content") or "").strip()
                err = ""
            except Exception as e:  # noqa: BLE001
                content, err = "", f"{type(e).__name__}: {e}"
            dt = time.time() - t0
            verdict = parse_verdict(content)
            rec = {"prompt": pname, "plan": plan_name, "want": p["want"],
                   "verdict": verdict, "correct": verdict == p["want"],
                   "seconds": round(dt, 1), "error": err,
                   "response": content[:2000]}
            out.write(json.dumps(rec) + "\n")
            out.flush()
            print(f"    -> {verdict} (want {p['want']}) {'OK' if verdict == p['want'] else 'MISS'} [{dt:.0f}s]",
                  flush=True)
    out.close()
    print("done -> eval/plan-review-results.jsonl")


if __name__ == "__main__":
    main()
