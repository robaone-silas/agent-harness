"""Round 2: stability + generalization check for the P1 checklist prompt.

- Reruns P1 on the original 4 plans (stability across runs).
- Tests 2 novel plans (generalization beyond the round-1 examples):
  BAD-3: Then names a file no step writes (wrong-file incoherence).
  GOOD-3: fix-bug style plan, should approve.
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_p = os.environ.get("HTTPS_PROXY", "")
if _p and ":3130" not in _p:
    _p = _p.rsplit(":", 1)[0] + ":3130" if ":" in _p else _p
    for _k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        os.environ[_k] = _p
for _k in ("all_proxy", "no_proxy", "ALL_PROXY"):
    os.environ.pop(_k, None)

from harness.client import chat  # noqa: E402
from eval.plan_review_test import PROMPTS, PLANS, parse_verdict  # noqa: E402

ENDPOINT = os.environ.get("HARNESS_ENDPOINT", "http://localhost:11434")
MODEL = "gemma4:e2b"

PLANS2 = {
    "BAD-3": {
        "task": "Write the text \"Hello\" to hello.txt in the workspace",
        "plan": (
            "Step 1: When I create the output directory\n"
            "  Then: the directory \"out\" exists\n"
            "Step 2: When I write \"Hello\" to hello.txt\n"
            "  Then: the file \"greeting.txt\" contains exactly \"Hello\""
        ),
        "want": "REJECT",
        "flaws": "Then checks greeting.txt but the plan writes hello.txt — incoherent",
    },
    "GOOD-3": {
        "task": "Fix broken.py so that running 'python3 broken.py' prints OK",
        "plan": (
            "Step 1: When I read broken.py\n"
            "  Then: I can see the syntax error in broken.py\n"
            "Step 2: When I write the corrected code to broken.py\n"
            "  Then: broken.py contains \"print('OK')\"\n"
            "Step 3: When I run `python3 broken.py`\n"
            "  Then: the command output is exactly \"OK\""
        ),
        "want": "APPROVE",
        "flaws": "none",
    },
}

CASES = [("P1-checklist", n, PLANS[n]) for n in ("BAD-1", "BAD-2", "GOOD-1", "GOOD-2")]
CASES += [("P1-checklist", n, PLANS2[n]) for n in ("BAD-3", "GOOD-3")]

out = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "plan-review-round2.jsonl"), "w")
for i, (pname, plan_name, p) in enumerate(CASES, 1):
    prompt = PROMPTS[pname].format(task=p["task"], plan=p["plan"])
    print(f"[{i}/{len(CASES)}] {pname} x {plan_name} ...", flush=True)
    t0 = time.time()
    try:
        msg = chat(ENDPOINT, MODEL, [{"role": "user", "content": prompt}],
                   temperature=0.2, timeout=120)
        content = (msg.get("content") or "").strip()
        err = ""
    except Exception as e:  # noqa: BLE001
        content, err = "", f"{type(e).__name__}: {e}"
    dt = time.time() - t0
    verdict = parse_verdict(content)
    rec = {"prompt": pname, "plan": plan_name, "want": p["want"],
           "verdict": verdict, "correct": verdict == p["want"],
           "seconds": round(dt, 1), "error": err, "response": content[:1500]}
    out.write(json.dumps(rec) + "\n")
    out.flush()
    print(f"    -> {verdict} (want {p['want']}) {'OK' if verdict == p['want'] else 'MISS'} [{dt:.0f}s]",
          flush=True)
out.close()
print("done -> eval/plan-review-round2.jsonl")
