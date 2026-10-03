# agent-harness

A minimal agent harness built for small local models, starting with
`gemma4:e2b` via Ollama. Zero dependencies — pure Python 3 stdlib.

The bet: if a 2B model can do real work when every problem is broken into
small enough pieces, the same harness scales straight up to larger models.

**Project page:** https://robaone-silas.github.io/agent-harness/

## Quickstart (on your Mac)

```bash
# 1. Ollama + model (one time)
brew install ollama
ollama pull gemma4:e2b

# 2. Run a task
cd agent-harness
python3 run.py "Create a file named hello.txt containing exactly: Hello, harness!"
```

Useful flags: `--model gemma4:e4b` to scale up, `--workspace ./my-sandbox`
to choose where the agent works, `--max-steps 20`, `--trace run.jsonl`
to save the full step trace, `--quiet` for just the final answer.
Env vars `HARNESS_MODEL`, `HARNESS_ENDPOINT`, `HARNESS_WORKSPACE` also work.

## Run the micro-eval

```bash
python3 eval/run_eval.py --model gemma4:e2b
```

Five tiny tasks (write, compute, search, fix, multi-step), each graded by a
checker on disk. Watch the `native=` vs `fallback=` counts: they tell you
whether the model's native function calling is actually engaging.

## Design: why a 2B model gets this shape

- **Five tools, one call per turn.** Small models get confused by wide tool
  surfaces and parallel calls. `exec`, `read_file`, `write_file`,
  `edit_file`, `list_dir` — plus `grep_files`, which moves the
  list-and-scan loop into code so the model never handles filenames it
  hasn't seen (it can't hallucinate what it can't invent).
- **Native tool calling first, text fallback second.** The harness sends
  Ollama-native `tools`; if the model instead emits a
  ` ```toolcall {"name": ..., "arguments": {...}} ``` ` block, the harness
  parses and runs it anyway. The trace records which path each step took,
  so you get real signal on native-call reliability per model.
- **Short system prompt with a worked example.** A 2B model copies a
  concrete example more reliably than it follows abstract rules. The example
  is deliberately schematic (no fake "Tool result:" line to recite) plus an
  explicit never-fabricate-results warning — v0.1 taught us the model will
  otherwise recite the example including an invented tool result.
- **Strict finish gating.** A response with no tool call only ends the run if
  it starts with `DONE:`. Narration without action gets a nudge back to work
  instead of a free pass.
- **Labeled tool results + success steer.** The model sees its own tool calls
  in history, each result is tagged with the call that produced it, and every
  success is followed by an explicit "that succeeded — if done, answer DONE:,
  otherwise call the next tool." Small models don't reliably parse bare exit
  codes or connect orphaned results to their actions.
- **Repeat-call circuit breaker.** An identical consecutive tool call is
  refused ("take a different action") instead of re-executed — the escape
  hatch for observation loops. If the repeated call had *succeeded*, the
  harness instead nudges the model to answer `DONE:` — the model reliably
  redoubles a successful action rather than declaring completion, so the
  harness translates the repeat into the termination it can't say itself.
- **Contradiction pushback.** If the model answers `DONE:` right after a
  tool call *failed* (e.g. a plan referencing a file that doesn't exist),
  the harness pushes back once and demands engagement with the error,
  instead of letting it declare victory over a contradiction. Safety
  refusals (blocked commands, workspace escapes) are exempt — stopping
  there is the correct behavior.
- **Validation before execution.** Unknown tools, missing args, and bad
  types come back as tool errors the model can recover from — they never
  crash the loop.
- **Jailed workspace.** All file access is confined under `--workspace`;
  `exec` runs there too, with a timeout and a denylist for destructive
  patterns (`rm -rf /`, forks bombs, `dd` to devices, piped shell downloads).
- **Error budget.** Three consecutive tool errors ends the run with a
  summary instead of spiraling. `max_steps` (default 12) bounds cost.
- **Full trace.** Every step — thought, tool, args, result, call path —
  prints live and saves to JSONL for post-mortems.

## Layout

```
run.py                 CLI entry point
harness/
  config.py            Config + argparse (env-overridable)
  client.py            Ollama /api/chat client (stdlib urllib)
  tools.py             Tool registry: jailed file ops + guarded exec
  prompts.py           Short system prompt + worked example
  loop.py              Agent loop: native-first, fallback-second
tests/test_loop.py     19 assertions, scripted fake model, no Ollama needed
eval/
  run_eval.py          5 checkable micro-tasks against a real model
  tasks.md             What each task measures
```

## Scaling up

Nothing in the loop is model-specific. Point `--model` at `gemma4:e4b`,
`gemma4:26b`, or anything else Ollama serves and the same tasks run.
Compare eval scores and the native/fallback ratio across sizes — that's
the experiment.

## Plan mode: Gherkin plans with human approval (v0.6)

Measured behavior on `gemma4:e2b`: reliable single atomic actions, no
sequencing, no completion tracking. Plan mode moves the sequencing into
the harness — and the plan itself is **Gherkin**, one Scenario per step,
so the human, the planner, and the executor all read the same language:

```
python3 run.py --plan "Create fibonacci.py that prints the first 12 Fibonacci numbers, run it, save the output to fib.txt"
# -> writes plan.feature and stops. Review it, edit it, then:
python3 run.py --run plan.feature
```

1. **Propose** (`--plan`): the model is asked once for a Gherkin plan
   (2–6 scenarios). The harness parses and validates it — a missing
   `Then`, or an unresolvable `<placeholder>`, is rejected here with the
   reason, never executed. One retry with the rejection explained.
2. **Approve** (human): read the `Then` lines first — they define what
   "done" means for each step, and that is what you are signing off on.
   Edit the `.feature` file freely; it re-validates on load.
3. **Execute** (`--run`): each scenario runs as one scoped instruction.
   The model never sees the whole job during execution, just the piece
   in front of it plus a compact summary of previous step results.
4. **Auto** (`--auto`): propose + execute in one go, no approval step.
   This is what the eval uses (`eval/run_eval.py --plan`).

Example plan:

```gherkin
Feature: Fibonacci to file
  Scenario: Step 1 - Write the script
    When I write fibonacci.py that prints the first 12 Fibonacci numbers
    Then fibonacci.py exists
  Scenario: Step 2 - Run it and save output
    When I run `python3 fibonacci.py > fib.txt`
    Then fib.txt contains 12 lines
```

Why Gherkin: every failure so far has been a language-agreement failure —
what "done" means, what a checker complaint refers to, what a step
instruction literally says. A shared, checkable contract removes that
whole class.

## Per-step Then verification (v0.7)

v0.7 makes `Then` clauses executable contracts. After a step's sub-run
reports DONE, the harness evaluates each `Then` **in code** before
advancing — zero model calls on the happy path:

- A registry of verifiers (`harness/verify.py`) matches `Then` phrasings:
  `exists`, `not_exists`, `contains`, `contains_exactly`, `has_lines`,
  and `covers` (v0.8.1, below).
  Adding a verifier is a `@verifier(name, pattern)` decorator + a check
  function — the engine never changes.
- A failed `Then` triggers one bounded step retry with the verifier's
  exact failure (`got` + `expected`) as feedback. If it still fails, the
  failure is downgraded to a recorded warning and the plan continues —
  the verifier checks plan *adherence* while the task checker judges
  *correctness*, so a wrong plan never blocks right work. Sub-run
  failures (model errors, limits) still hard-fail.
- A `Then` no verifier matches falls back to **attestation** — the
  model's word, explicitly recorded. Verifiers are strict by design:
  expected text must be quoted, paths must be quoted or whitespace-free,
  so prose like `found.txt contains the list of filenames` never
  false-positives as a literal assertion.
- `--plan` / `--run` badge each `Then`: `[checks itself: <verifier>]`
  vs `[on trust]`.

Measured on `gemma4:e2b` (plan mode, 3 attempts): **43–50% of Then
clauses verify deterministically**, the rest attest. The registry is
expandable — track hit rate per verifier (`eval/hit_rate.py`) rather
than assuming 100% coverage.

Honest limits: verification checks plan *adherence*, not plan
*correctness*. If the planner writes a wrong `Then` (e.g. a hardcoded
value), the verifier faithfully enforces it and the step fails even
when the artifact is right — the task-level checker remains the
backstop. Two e2b evals at v0.7: **4/5** on task checkers; multi-step
fails because the planner hardcodes `print('main.py')` and the v0.6
intent (smell) gate correctly rejects it. That's a planner knowledge
gap, not a verification bug.

## Plan lint: deterministic design checks (v0.7.1)

Prompt-only review of plans by the small model proved unreliable
(8/10 across prompt styles; the same prompt approved and rejected the
identical plan on different runs), so plan *coherence* is checked in
code, with zero model calls, in `harness/planlint.py`. It runs after
Gherkin validation, before the plan reaches the user:

- `dangling_file` (ERROR): a Then targets a file no step creates and
  the task doesn't mention — triggers one planner retry with the
  finding as feedback, mirroring the Gherkin validation flow.
- `value_from_nowhere` (WARN): a Then asserts an exact value appearing
  nowhere in the task or prior steps — computed or invented?
- `unparseable_check` (WARN): a Then reads like a check but matches no
  verifier, so it will be taken on trust.

Findings print at approval time next to the verifier badges. Known
limit, documented in tests: a When and Then that *consistently*
hallucinate the same invented value are not deterministically
catchable — that needs model or human judgment.

## Suggestion library: filling knowledge gaps (v0.8)

Some failures are missing idioms, not bad reasoning — e2b hardcoded
`print("main.py")` across five evals because it didn't know
`os.path.basename(__file__)`. `harness/suggestions.py` pairs trigger
phrases with copy-pasteable idioms; matches are injected into the
planner prompt (so the plan doesn't bake in the broken implementation)
and into TDD retry feedback (so a failed attempt gets the idiom next
to the checker's complaint). Pure data + substring matching, zero
model calls. Add entries as new gaps are observed — each needs the
idiom itself, not just advice.

## Step-output store: data survives the step boundary (v0.8.1)

First field failure of the shipped harness: a plan listed the files in
a documents folder in step 1, then forgot the list by the step that was
supposed to suggest an organization for them. Only a 250 character
summary of each step's final answer crossed the boundary; tool outputs,
where the real data lives, never did.

`harness/runstore.py` fixes that in the harness, not the model. After
every step, the harness writes the step's full record to a
deterministic file:

```
<workspace>/.harness/runs/<YYYYMMDD-HHMMSS>/step-01.md
<workspace>/.harness/latest.txt          (newest run folder name)
```

Each record holds the instruction, the Then clauses, every tool call
with its result verbatim, the final answer, and the verification
outcome, including earlier attempts when verification retried the step.
Each step's prompt then names the exact prior-step files and tells the
model to `read_file` the one it needs when it needs exact data instead
of the summary. No new tool was needed: storage is deterministic and
harness-written, access reuses the tool the model already knows.
`latest.txt` is the continuation hook for a later session, and one
`.gitignore` line (`.harness/`) keeps the records out of a git
workspace's history.

Same release, a smaller field fix: `list_dir` was shallow only, and
listing a subfolder returned bare names no other tool could use as a
path. It now takes an optional `depth` (default 1, the old behavior;
the walk itself lives in code, the model only chooses how deep).
Entries return as workspace-relative paths, folders with a trailing
`/`; the listing is sorted, capped at 500 entries with the truncation
announced, and symlinked folders are listed but never followed, so
the walk cannot leave the jail. The `.harness/` records folder is not
listed: it is bookkeeping, not user data.

Same release, the plan frame: the executor prompt used to show only
the current step, a v0.6 scoping decision made when the fear was a
small model freelancing across steps. The field failures ran the
other way: steps starved for context. Each step prompt now opens with
a short frame explaining that this is one step of a multi-step plan,
followed by the outline of the whole plan: every step with its status
(done, your step, pending), done steps annotated with their result
and their stored output file. One standing rule rides in the frame:
if a step's work is based on an earlier step's output, read that
step's file first. The operative instruction itself stays scoped to
the current step and sits last in the prompt, where it always did.

And the frame has a gate behind it. A later field run produced a
one-line placeholder `organization.md`: the run record showed the
writing step had never read the listing step's stored output at all.
So when a step answers DONE and no tool call in its sub-run touched
a prior step's output file, the harness pushes back, inside the
same run, naming the exact file: read it and redo the step from its
real contents. Reading through any tool counts (`read_file`, an
`exec` cat, anything whose arguments name the path). The gate has
two stages: stage one fires when the source was never read, and
stage two fires when the step read it but changed nothing afterward,
which is the exact sequence the next field run produced (write the
placeholder, get pushed, read the file, re-assert DONE). Reading is
not redoing. Pushbacks are capped at two per run; past the cap, DONE
is accepted as-is, so a genuinely independent step pays a sentence
or two, not a block. The loop stays generic about it: `loop.run`
accepts a `done_gate` hook, consulted on every DONE until it clears
or the cap is reached, and the planner supplies the plan-specific
check.

The same field run closed one more loop. Process checks can force
the read; only a content check can judge the deliverable. This
release adds the `covers` verifier: a `Then` of the form
`"organization.md" covers the files from step 1` is checked in
code against the source step's actual output. The harness extracts
the item list deterministically from listing-shaped tool results
(`list_dir` and `grep_files` output, `ls`/`find` output), counts
how many of those names the deliverable actually mentions, and
fails it below half, with the missing names as the retry feedback.
A document that defers its own content ("to be filled in", TODO,
and friends) fails on its own words. The planner is taught the
idiom for any document derived from an earlier step's listing, and
`VerifyContext` now carries prior steps' items, the seam future
output-aware verifiers build on. Tests replay the field failure
end to end: stub, failed coverage check, retry with the missing
names, real summary.

Also in this release: when a step's model answers a bare `DONE:`
with no summary text, the next step's outline no longer carries an
empty Result line. The harness computes one from its own record:
the tools the step used and how many lines of output they produced.
The summary channel can stay terse without going silent.

Two papercuts from the same live session, fixed: `--task-file` read
the task into a local variable that never reached the caller, so the
planner planned for no task at all ("No Task Provided"); the resolved
task now comes back on the args namespace where `run.py` reads it.
And the step prompt's stored-at line ("Your full output will be
stored at...") read as a write instruction to the model, which duly
wrote its deliverable into its own record file; the line now says the
harness does the storing and the model should not write there.

Last, Tier 1 discovery: planning no longer starts blind. The bare
prompt "organize files in this folder by category" produced a plan
from the model's priors, category folders for `.pdf` and `.jpg` files
in a folder containing neither, the real files (`.txt`, `.csv`, `.md`)
addressed by no step. The harness now lists the workspace itself
(names, kinds, sizes; no content read, no model calls, `.harness/`
never listed, capped with the truncation announced) and puts that
digest in the planner prompt with a plain instruction: plan against
what is actually here, do not invent files, extensions, or
categories. Rerunning the same bare prompt against the same folder
produced categories drawn from the real inventory (Finance, Planning,
Admin) and steps naming the real files. What the digest does not fix:
the planner's mechanics (it proposed moving files into a "new file
named Finance" rather than a folder) and its phrasing precision (it
reached for the `covers` idiom in the detailed-prompt run but wrote
"cover the files", which the strict verifier pattern does not match,
so the clause attested). Those are the next levers: plan examples for
shape, and idiom patterns widened to the phrasings the planner
actually produces.
