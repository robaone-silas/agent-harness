---
layout: default
title: agent-harness
---

# agent-harness

A minimal agent harness built for small local models, starting with
`gemma4:e2b` via Ollama. Zero dependencies, pure Python 3 stdlib.

The bet: strategy and executive function belong in the harness, not in the
model. If a 2B model can do real work when every problem is broken into
small enough pieces, the same harness scales straight up to larger models.

## How it works

- **Gherkin plans.** The agent proposes a plan as a `.feature` file, one
  Scenario per step. The human reviews and edits it, then the harness
  executes the approved plan.
- **Per-step verification.** Deterministic verifiers check each step's
  `Then` clauses in code, at zero model cost.
- **Plan lint.** Deterministic design checks catch dangling files and
  values from nowhere before approval.
- **TDD retries.** When an acceptance check fails, the harness retries
  with the checker's exact failure message as feedback.
- **Suggestion library.** Trigger phrases map to copy-pasteable idioms
  that fill the model's knowledge gaps from the harness side.

Both `gemma4:e2b` and `gemma4:e4b` score 5/5 on the five-task checkable
eval, running locally. No cluster required.

## Quickstart

```bash
# 1. Ollama + model (one time)
brew install ollama
ollama pull gemma4:e2b

# 2. Run a task
cd agent-harness
python3 run.py "Create a file named hello.txt containing exactly: Hello, harness!"
```

## Links

- [Source code](https://github.com/robaone-silas/agent-harness)
- [Latest release: v0.8.0](https://github.com/robaone-silas/agent-harness/releases/tag/v0.8.0)
- [Blog post: A Practical Agent Harness for Small Language Models](https://site.robaone.com/blog/a-practical-agent-harness-for-small-language-models)
