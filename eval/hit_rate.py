"""Then-clause verifier hit-rate analysis for an eval JSONL file.

Usage: python3 eval/hit_rate.py eval-results-<ts>.jsonl
"""
import json
import sys

path = sys.argv[1]
total, verified, attested, vfailed = 0, 0, 0, 0
by_verifier: dict[str, int] = {}
task_ok: dict[str, bool] = {}

for line in open(path):
    r = json.loads(line)
    if r["attempt"] == 1:
        task_ok[r["task"]] = r.get("ok")
    if r["attempt"] != 1:
        continue  # hit rate measured on first attempts only
    for s in r.get("plan", []):
        for t in s.get("verify", []):
            total += 1
            v = t.get("verifier", "?")
            if v == "attest":
                attested += 1
            else:
                verified += 1
                by_verifier[v] = by_verifier.get(v, 0) + 1
            if not t.get("ok"):
                vfailed += 1

print(f"file: {path}")
print(f"checker pass (attempt 1): {sum(task_ok.values())}/{len(task_ok)} {task_ok}")
print(f"Then clauses (attempt 1): {total}")
if total:
    print(f"  verified deterministically: {verified} ({verified/total:.0%})")
    print(f"  attested (fallback):        {attested} ({attested/total:.0%})")
    print(f"  verification failures:      {vfailed}")
print(f"  per-verifier: {by_verifier}")
