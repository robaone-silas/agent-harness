# Micro-eval tasks

Five tasks, ordered by how many distinct capabilities they need. Each runs in a
fresh sandbox and is graded by a programmatic checker on disk — no vibes-based
grading. Run with `python3 eval/run_eval.py --model gemma4:e2b`.

| # | Task | Needs |
|---|------|-------|
| 1 | write-file | one tool call, exact content |
| 2 | read-and-compute | read CSV, arithmetic, write result |
| 3 | search | scan files, identify match, write filename |
| 4 | fix-bug | read code, diagnose, edit, verify by running |
| 5 | multi-step | mkdir, write, execute, capture output to file |

**How to read the results:** the per-task line reports `native=` vs `fallback=`
tool calls. If `native` is 0 across tasks, the model's native function calling
isn't engaging and everything rides on the text fallback — useful signal before
scaling to larger models. A task failing at step 1-2 usually means the prompt
format isn't landing; failing at step 8+ usually means the task needs
decomposing further (or a bigger model).
