"""Plan-then-execute mode, v0.6: plans are Gherkin.

Measured e2b behavior: reliable single atomic actions, no sequencing,
no completion tracking. So the harness takes over the sequencing, and the
plan itself is a Gherkin feature file — one Scenario per step — which is
the shared language between the planner, the human approving the plan,
and the executor:

  1. Propose: ask the model (once) for a Gherkin plan; parse and validate
     it. A plan with a missing Then or an unresolvable <placeholder> is
     rejected here, never executed.
  2. Approve (human): the .feature file is reviewed/edited, Then clauses
     first — they define what "done" means.
  3. Execute: each Scenario runs as one small scoped instruction, tracked
     step by step in code.

`run_planned` does propose+execute in one go (auto mode, what the eval
uses). `propose` + `execute_plan` are the separable halves for the
approval flow.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace

from . import gherkin
from . import loop
from .config import Config


def workspace_digest(workspace: str | None, limit: int = 100) -> str:
    """Deterministic pre-planning discovery (Tier 1, v0.8.1): the harness
    lists the workspace itself and shows the planner what is actually
    there, so plans are written against real files instead of the model's
    priors (field, 2026-10-03: a bare "organize by category" prompt got
    category folders for .pdf/.jpg files in a folder containing neither).

    Names, kinds, and sizes only: no content is read, no model calls.
    `.harness/` bookkeeping is never listed. Returns "" when no listing
    can be made: discovery must never block planning."""
    from pathlib import Path
    if not workspace:
        return ""
    try:
        entries = sorted(Path(workspace).iterdir(), key=lambda p: p.name)
    except OSError:
        return ""
    lines: list[str] = []
    total = 0
    for p in entries:
        if p.name == ".harness":
            continue
        total += 1
        if len(lines) >= limit:
            continue
        if p.is_dir():
            lines.append(f"- {p.name}/ (folder)")
        else:
            try:
                size = p.stat().st_size
            except OSError:
                size = 0
            lines.append(f"- {p.name} ({size} bytes)")
    if total == 0:
        body = "(the workspace folder is currently empty)"
    else:
        body = "\n".join(lines)
        if total > len(lines):
            body += f"\n- ... and {total - len(lines)} more entries"
    return ("\nWorkspace contents right now (listed by the harness itself):\n"
            + body
            + "\nPlan against what is actually here: do not invent files, "
              "extensions, or categories that are not present.\n")


_EXAMPLE_PLANS = """
Example plans (copy the shape, not the content):

Task: Make an index of the recipes in this folder
Feature: Recipe index
  Scenario: Step 1 - List the recipes
    When I list the files in the folder
    Then the output lists the recipe files
  Scenario: Step 2 - Write the index
    When I write recipe-index.md indexing the listed recipes
    Then "recipe-index.md" covers the files from step 1

Task: Move the 2025 meeting notes into an archive folder
Feature: Archive meeting notes
  Scenario: Step 1 - List the notes
    When I list the files in the folder
    Then the output lists the note files
  Scenario: Step 2 - Create the archive folder
    When I create the folder "archive"
    Then "archive" exists
  Scenario: Step 3 - Move one note
    When I run: mv notes-2025-03.txt archive/
    Then "archive/notes-2025-03.txt" exists

Task: Make a reading guide to the articles in this folder: each entry gives the article's title and a one-sentence summary of its argument
Feature: Reading guide
  Scenario: Step 1 - List the articles
    Intent: know exactly which articles the guide must cover
    When I list the files in the folder
    Then the output lists the article files
  Scenario: Step 2 - Write the guide
    Intent: each entry gives the article's title and a one-sentence summary of its argument, not the article text
    When I read each article from step 1 and write guide.md with one entry per article
    Then "guide.md" covers the files from step 1

"""


def build_planner_prompt(task: str, tool_names: list[str],
                         workspace: str | None = None,
                         discovery: str = "") -> str:
    idioms = _idiom_block(task)
    digest = workspace_digest(workspace)
    if discovery:
        digest += ("\nDiscovery findings (from a read-only discovery pass "
                   "the harness just ran):\n" + discovery
                   + "\nBase the plan's categories, values, and groupings on "
                     "these findings, not on assumptions about what such a "
                     "workspace usually contains.\n"
                   + "\nReconcile the findings against the workspace contents "
                     "listed above before planning: a workspace file that "
                     "is absent from the findings is unexamined, not "
                     "automatically out of scope. If an unexamined file "
                     "could belong to this task, account for it in the plan "
                     "(list it, read it, or name it) or explicitly exclude "
                     "it; do not describe a set drawn only from the findings "
                     "as complete, or as all of the files.\n")
    return f"""You are a planner. Break the task below into a short sequence of small steps.
Write the plan in Gherkin — one Scenario per step, in order.

Format:
Feature: <short title for this task>
  Scenario: Step 1 - <short name>
    Intent <what this step is for, in the task's terms — see the intent rule below>
    When <one concrete action naming exact files or commands>
    Then <how to tell the step worked, checkable in files or command output>
  Scenario: Step 2 - <short name>
    When <one concrete action>
    Then <checkable outcome>

Available tools for the steps: {", ".join(tool_names)}.

Rules:
- 2 to 6 scenarios, ordered so each step's output feeds the next.
- Each When is one concrete action; each Then is checkable in files or command output.
- Write Then clauses as direct verifiable phrasings the harness checks itself:
  `"path" exists`, `"path" contains "text"`, `"path" contains exactly "text"`,
  `"path" has 3 lines`. Prefer these over prose — the harness verifies them
  in code after each step, and only falls back to trust for other phrasings.
- When a step writes a document derived from an earlier step's file listing
  (a summary, strategy, report, or index of those files), write its Then as
  `"out.txt" covers the files from step N` (N = the listing step) — the
  harness checks the document actually mentions the source files.
- Do not substitute one `contains` Then per source file for `covers` on
  a document derived from a listing. Per-file clauses cannot prove the
  document covers the listing; use the single `covers` clause instead.
- Preserve the task's intent in every step. When the task specifies what a
  deliverable must contain or be like (for example: each entry gives a name
  and a one-line description), give every step that contributes to that
  deliverable an Intent line carrying the task's own words for it, and write
  its When to match the Intent: a description is not the content, and a
  summary is not the source text. Decomposition that drops the task's
  specification produces work that is finished and wrong.
- The Then must accurately describe THIS step's expected outcome. A precisely
  worded wrong expectation fails verification just as surely as a vague one.
- Concrete values come from the task statement only — never <angle-bracket
  placeholders> or phrases like "content from step 2". For values a step must
  COMPUTE, do not invent the result: write "total.txt contains the computed sum",
  never "total.txt contains exactly "12345"". Acceptance criteria state WHAT to
  check, not the implementation's literal output.
- A Given line is optional, for starting-state context a step needs.
- Steps may rely on data produced by earlier steps: the harness stores each
  step's full output (tool results included) in a file and tells the executor
  the exact path, so a later step can read an earlier step's real output.
  Still never invent the values themselves in a Then.
- Do NOT add verify or summarize scenarios — the harness handles finishing.
{_EXAMPLE_PLANS}- Return ONLY the Gherkin, no other text.
{idioms}{digest}Task: {task}"""


def _idiom_block(task: str) -> str:
    """Known idioms matching this task, for the planner prompt (may be empty)."""
    from . import suggestions as _sug
    found = _sug.for_task(task)
    if not found:
        return ""
    return ("\nKnown idioms that apply to this task — use them instead of "
            "hardcoding:\n" + _sug.render(found) + "\n")


@dataclass
class PlanStep:
    id: int
    instruction: str
    done_when: str
    given: str = ""
    title: str = ""  # Scenario title from the Gherkin (v0.8.1 plan frame)
    intent: str = ""  # the step's Intent: line (v0.8.1 intent preservation)
    status: str = "pending"  # pending | done | failed
    result: str = ""
    verify: list = field(default_factory=list)  # per-Then check records (v0.7)
    lint: list = field(default_factory=list)  # planlint findings (dicts)
    verify_waived: bool = False  # verification failed but step accepted anyway
    output_path: str = ""  # .harness/runs/<run>/step-NN.md (v0.8.1, issue #2)
    source_items: list = field(default_factory=list)  # names this step produced (v0.8.1 covers)
    tool_summary: str = ""  # harness-computed "tools used; output size" line


def to_plan_steps(plan: gherkin.FeaturePlan) -> list[PlanStep]:
    steps = []
    for i, p in enumerate(plan.steps, 1):
        steps.append(PlanStep(
            id=i,
            instruction="\n".join(p.when)[:500],
            # done_when is the step's verification contract: badges, lint,
            # executor prompts, and verification all consume it. It is NOT
            # length-capped. A 300-char cap silently truncated combined
            # Then clauses mid-clause (issue #7: a six-clause step lost its
            # tail), so every clause is preserved exactly.
            done_when="\n".join(p.then),
            given="\n".join(p.given)[:300],
            title=(p.title or "")[:120],
            intent=(p.intent or "")[:300],
        ))
    return steps


def request_plan(task: str, tool_names: list[str], chat_fn, cfg: Config,
                 discovery: str = ""
                 ) -> tuple[str | None, list[PlanStep] | str]:
    """Ask the model for a Gherkin plan; retry with rejections explained.

    Two validations run per proposal: structural (Gherkin parse) then design
    (deterministic plan lint). Either can trigger the one retry; findings are
    attached to the returned steps as s.lint for the approval display.
    Returns (gherkin_text, steps) or (None, error_string)."""
    from . import planlint as _planlint
    messages = [{"role": "user",
                 "content": build_planner_prompt(task, tool_names,
                                                 workspace=cfg.workspace,
                                                 discovery=discovery)}]
    last_problem = "no response"
    for attempt in (1, 2):
        try:
            msg = chat_fn(messages, None)
        except Exception as e:
            return None, f"planner call failed: {e}"
        content = (msg.get("content") or "").strip()
        plan, err = gherkin.parse_feature(content)
        if plan is None:
            feedback = f"That plan was rejected: {err}."
            last_problem = err
        else:
            steps = to_plan_steps(plan)
            findings = _planlint.lint_plan(
                task, steps,
                workspace_files=_root_workspace_files(cfg.workspace))
            by_step: dict[int, list] = {}
            for f in findings:
                by_step.setdefault(f.step_id, []).append(f.as_dict())
            for s in steps:
                s.lint = by_step.get(s.id, [])
            errors = [f for f in findings if f.level == "error"]
            if not errors or attempt == 2:
                return content, steps
            feedback = ("That plan has design flaws: "
                        + "; ".join(f.detail for f in errors) + ".")
            last_problem = "; ".join(f.detail for f in errors)
        messages.append({"role": "assistant", "content": content})
        messages.append({"role": "user", "content":
                         feedback + " Return ONLY the corrected Gherkin, no other text."})
    return None, last_problem


def _chat_and_tools(cfg: Config, chat_fn=None):
    from . import client as _client
    from . import tools as _tools
    if chat_fn is None:
        def chat_fn(messages, tool_defs):
            return _client.chat(
                cfg.endpoint, cfg.model, messages,
                tools=_client.to_ollama_tools(tool_defs) if tool_defs else None,
                temperature=cfg.temperature, top_p=cfg.top_p,
                timeout=cfg.request_timeout)
    jail = _tools.Jail(cfg.workspace)
    registry = _tools.make_tools(jail, cfg.blocked_substrings, cfg.exec_timeout,
                                 cfg.max_output_chars)
    return chat_fn, jail, registry


def propose(task: str, cfg: Config, chat_fn=None
            ) -> tuple[str | None, list[PlanStep] | str]:
    """Propose a Gherkin plan without executing it. Returns (gherkin_text,
    steps) or (None, error_string)."""
    chat_fn, jail, registry = _chat_and_tools(cfg, chat_fn)
    return request_plan(task, list(registry), chat_fn, cfg)


# ------------------------------------------------------- Tier 2 discovery
#
# Some plans cannot be written well until someone has looked: the task's
# categories, values, or structure depend on what the workspace actually
# contains. Tier 1 (the digest) covers names and sizes. Tier 2 runs a
# model-planned discovery pass first, restricted at the registry level
# to read-only tools so it cannot change anything, stores its records
# like any run, and then plans again with the findings in the prompt.
# The plan the human approves is the informed one.

READ_ONLY_TOOLS = ("list_dir", "read_file", "grep_files")


def _is_exhaustive_task(task: str) -> bool:
    """True when the task asks for an account of the whole workspace:
    a list, inventory, index, or catalogue, or all / every / each of the
    files (or documents, papers, records). Those tasks make every
    root-level file potentially in scope, so discovery for them must be
    exhaustive rather than a sample (issue #6). Category, strategy, and
    value tasks carry none of these cues and keep sampling."""
    import re
    low = (task or "").lower()
    if re.search(r"\b(list|listing|inventory|index|catalogue|catalog)\b",
                 low):
        return True
    return bool(re.search(
        r"\b(all|every|each)\b[^.\n]{0,40}"
        r"\b(files?|documents?|papers?|records?)\b", low))


def _discovery_steering(task: str) -> str:
    """The task-sensitive sampling rule for the discovery prompt.

    Exhaustive tasks (see _is_exhaustive_task) get a listing step and a
    full accounting of the workspace root; the sampling rule that is
    right for category, strategy, and value discovery is expressly
    withdrawn for them, because a sample silently drops files the task
    asked about (issue #6, Tomas's omitted water-heater manual)."""
    if _is_exhaustive_task(task):
        return ("- This task is exhaustive: it asks for a list, an "
                "inventory, or an index, or for all or every file. "
                "Sampling is not acceptable for this task.\n"
                "- Start with a listing step that lists the files at "
                "the workspace root.\n"
                "- Account for every file at the workspace root: each "
                "one must be read, searched, named in the plan, or "
                "explicitly excluded with a reason. No root file may "
                "be left unaccounted for.")
    return ("- Sampling is acceptable for this task: it asks what "
            "categories, a strategy, or values should be, not for an "
            "account of every file. Read the files most likely to "
            "decide the plan's categories or values; do not read "
            "everything.")


def build_discovery_prompt(task: str, workspace: str | None = None) -> str:
    digest = workspace_digest(workspace)
    steering = _discovery_steering(task)
    return f"""You are planning a read-only discovery pass. The task below
cannot be planned well yet: the planner first needs facts about the
workspace. Write a short Gherkin plan, 1 to 4 steps, whose steps only
LOOK at things. Discovery changes nothing.

Format:
Feature: <short title>
  Scenario: Step 1 - <short name>
    When <one concrete look: list, read, or search>
    Then <what will be known after this step>

Available discovery tools: {", ".join(READ_ONLY_TOOLS)}.
Rules:
- Only list, read, and search. No writes, no moves, no commands.
- Each step learns something the real plan needs: what files exist,
  what the important ones contain, how things are structured.
{steering}
- Return ONLY the Gherkin, no other text.
{digest}Task: {task}"""


def request_discovery_plan(task: str, cfg: Config, chat_fn
                           ) -> tuple[str | None, list[PlanStep] | str]:
    """Ask the model for a read-only discovery plan; one retry on a
    structural rejection, mirroring request_plan's shape (no lint:
    discovery steps make no commitments worth linting)."""
    messages = [{"role": "user",
                 "content": build_discovery_prompt(task,
                                                   workspace=cfg.workspace)}]
    last_problem = "no response"
    for attempt in (1, 2):
        try:
            msg = chat_fn(messages, None)
        except Exception as e:
            return None, f"discovery planner call failed: {e}"
        content = (msg.get("content") or "").strip()
        plan, err = gherkin.parse_feature(content)
        if plan is not None:
            return content, to_plan_steps(plan)
        last_problem = err
        messages.append({"role": "assistant", "content": content})
        messages.append({"role": "user", "content":
                         f"That plan was rejected: {err}. "
                         "Return ONLY the corrected Gherkin, no other text."})
    return None, last_problem


def _root_workspace_files(workspace: str | None) -> list[str]:
    """Sorted names of the regular files at the workspace root.

    Root only, matching the digest's view of the workspace: folders and
    their contents are not files the discovery pass was expected to read,
    and `.harness/` bookkeeping is never a workspace file."""
    from pathlib import Path
    if not workspace:
        return []
    try:
        entries = sorted(Path(workspace).iterdir(), key=lambda p: p.name)
    except OSError:
        return []
    return [p.name for p in entries if p.name != ".harness" and p.is_file()]


def unexamined_workspace_files(workspace: str | None, discovery_steps=None,
                               findings: str = "") -> list[str]:
    """Root workspace files the discovery pass never accounted for.

    Deterministic reconciliation at the Tier 2 handoff (issue #6): a
    file counts as accounted for when its name appears in the discovery
    plan (any step's instruction, done_when, given, title, or intent)
    or in the discovery records handed over as findings. That one test
    covers every way a file can be accounted for: listed (a list_dir
    record names it), read, searched, named in the plan, or explicitly
    excluded (an exclusion still names the file). A file named nowhere
    was not examined, whatever the plan claims about "all" files."""
    texts = [findings or ""]
    for s in discovery_steps or []:
        texts.append("\n".join([s.instruction or "", s.done_when or "",
                                s.given or "", s.title or "", s.intent or ""]))
    accounted = "\n".join(texts)
    return [name for name in _root_workspace_files(workspace)
            if name not in accounted]


def discovery_findings_text(workspace: str, run_id: str, n_steps: int,
                            total_cap: int = 8000,
                            per_step_cap: int = 2500,
                            discovery_steps=None) -> str:
    """The findings handed to the replan: excerpts of the discovery run's
    stored step records (tool results verbatim, where the facts live),
    bounded so discovery cannot become context-stuffing. Truncation is
    announced with a pointer to the full record.

    The excerpts are followed by a deterministic unexamined-files
    section (issue #6): the root workspace files compared against the
    FULL records and the discovery plan, so excerpt truncation cannot
    hide a file, and a file discovery never touched is named as
    unexamined instead of silently absent from the handoff."""
    from pathlib import Path
    if not run_id:
        return ""
    parts: list[str] = []
    full_records: list[str] = []
    used = 0
    for i in range(1, n_steps + 1):
        rel = f".harness/runs/{run_id}/step-{i:02d}.md"
        p = Path(workspace) / rel
        if not p.is_file():
            continue
        text = p.read_text(errors="replace")
        full_records.append(text)
        if len(text) > per_step_cap:
            text = text[:per_step_cap] + f"\n[truncated; full record: {rel}]"
        if used + len(text) > total_cap:
            text = text[:max(0, total_cap - used)]
        if text:
            parts.append(text)
            used += len(text)
        if used >= total_cap:
            break
    body = "\n\n".join(parts)
    if _root_workspace_files(workspace):
        missing = unexamined_workspace_files(
            workspace, discovery_steps=discovery_steps,
            findings="\n".join(full_records))
        if missing:
            section = ("Unexamined workspace files (the harness compared "
                       "the workspace root against the discovery plan and "
                       "records; these files were not listed, read, "
                       "searched, named, or explicitly excluded during "
                       "discovery, so nothing is known about them):\n"
                       + "\n".join(f"- {name}" for name in missing))
        else:
            section = ("Unexamined workspace files: none. Every file at "
                       "the workspace root was accounted for by the "
                       "discovery plan or its records.")
        body = (body + "\n\n" + section) if body else section
    return body


def plan_with_discovery(task: str, cfg: Config, chat_fn=None, on_event=None
                        ) -> tuple[str | None, list[PlanStep] | str, dict]:
    """Tier 2: discovery pass, then the real plan written with the
    findings in hand. Returns (gherkin_text, steps, info) or
    (None, error, info); info carries the discovery run id, its status,
    and the findings text. A failed discovery execution still replans
    with whatever findings completed steps produced."""
    chat_fn, jail, registry = _chat_and_tools(cfg, chat_fn)
    dtext, dsteps = request_discovery_plan(task, cfg, chat_fn)
    if dtext is None:
        return None, dsteps, {"discovery_run_id": "",
                              "discovery_status": "plan_failed",
                              "findings": ""}
    run = execute_plan(task, dsteps, cfg, chat_fn=chat_fn,
                       on_event=on_event, only_tools=list(READ_ONLY_TOOLS))
    findings = discovery_findings_text(cfg.workspace, run.run_id, len(dsteps),
                                       discovery_steps=dsteps)
    text, steps = request_plan(task, list(registry), chat_fn, cfg,
                               discovery=findings)
    info = {"discovery_run_id": run.run_id,
            "discovery_status": run.status,
            "findings": findings,
            "discovery_steps": dsteps}
    return text, steps, info


@dataclass
class PlannedRun:
    status: str  # done | plan_failed | step_failed | model_error
    answer: str
    steps: list[PlanStep] = field(default_factory=list)
    run_id: str = ""  # .harness/runs/<run_id>/ for this run (v0.8.1)

    def plan_dict(self) -> dict:
        return {"run_id": self.run_id,
                "steps": [
            {"id": s.id, "instruction": s.instruction, "done_when": s.done_when,
             "intent": s.intent,
             "status": s.status, "result": s.result[:500],
             "verify": s.verify, "lint": s.lint,
             "verify_waived": s.verify_waived,
             "output_path": s.output_path} for s in self.steps]}


# The plan frame (v0.8.1): every step prompt opens by explaining the
# situation the executor is in. v0.6 scoped the prompt to the current step
# only, fearing the model would freelance across steps; three field
# failures (a lost file list, an evaporated deliverable, an ungrounded
# write step) were all starvation instead. So the executor now sees the
# whole plan, its place in it, and where the data lives — while the
# operative instruction stays scoped and last.
FRAME_TEXT = (
    "How this works: this task is a multi-step plan, carried out one step "
    "at a time in separate runs like this one. Steps marked [done] are "
    "finished: do not redo them. Their full outputs, tool results "
    "included, are stored in the files named beside them, and the short "
    "summaries lose details. Steps marked [pending] run after you, in "
    "their own runs: do not do them, but they will read your stored "
    "output, so finish your step completely. If your step's work is based "
    "on an earlier step's output, read that step's file first with "
    "read_file instead of working from the summary. Your own step's "
    "output file is different: the harness writes it after you finish, "
    "it does not exist yet, and it is not a source. Do not try to read it."
)


def _plan_outline(steps: list[PlanStep], current_id: int) -> str:
    """The shape of the whole plan, one line per step, with statuses.

    Done steps are annotated with their (short) result and, when the run
    store is active, the exact file holding their full output."""
    import re as _re
    lines = ["The plan:"]
    for s in steps:
        if s.id == current_id:
            mark = "YOUR STEP"
        elif s.status == "done":
            mark = "done"
        else:
            mark = "pending"
        title = _re.sub(r"^Step\s+\d+\s*[-:]\s*", "", (s.title or "").strip())
        label = title or (s.instruction.split("\n")[0][:100] if s.instruction else "")
        lines.append(f"  Step {s.id} [{mark}] {label}")
        if s.id != current_id and s.instruction and title:
            lines.append(f"    When: {s.instruction.split(chr(10))[0][:160]}")
        if s.status == "done" and s.id != current_id:
            body = s.result or ""
            if body.strip().upper().startswith("DONE:"):
                body = body.strip()[5:].strip()
            if body:
                lines.append(f"    Result: {s.result[:200]}")
            elif s.tool_summary:
                # The model answered a bare "DONE:" — the summary channel
                # carried nothing. The harness computes a factual line from
                # its own record instead of leaving a blank.
                lines.append(f"    Result: (no summary given) {s.tool_summary}")
            elif s.result:
                lines.append(f"    Result: {s.result[:200]}")
            if s.output_path:
                lines.append(f"    Full output: {s.output_path}")
    return "\n".join(lines) + "\n"


def _grounding_gate(step: PlanStep, steps: list[PlanStep], verify_now=None):
    """Build the DONE gate for one step (v0.8.1), or None when the step has
    no completed prior steps with stored outputs.

    Field failure behind it: a step wrote its deliverable without ever
    reading the previous step's stored output, and produced a placeholder.
    The frame advises reading the source; the gate checks it. Any tool call
    whose arguments name a prior step's output file counts as grounded
    (read_file, an exec cat, anything). The gate fires once per sub-run;
    a second DONE is the escape hatch for genuinely independent steps.

    verify_now (the surgical fix, 2026-10-03): stage 2 used to fire blind,
    ordering a redo of a deliverable verification had never examined; in
    the field that redo briefly overwrote a good index with a worse one.
    When verify_now is supplied, stage 2 first runs the step's own Thens
    against the workspace: if they pass on content-bearing checks
    (contains, covers, and kin; a bare exists proves nothing, the
    founding stub passed it), the suspicion is answered and the gate
    stays silent. An attested or failing Then keeps the pushback
    exactly as before."""
    priors = [s.output_path for s in steps
              if s.id < step.id and s.status == "done" and s.output_path]
    if not priors:
        return None

    def gate(sub_steps) -> str | None:
        import json as _json

        def names_prior(s) -> bool:
            return bool(s.tool and s.args and any(
                p in _json.dumps(s.args, default=str) for p in priors))

        read_idx = next((i for i, s in enumerate(sub_steps) if names_prior(s)),
                        None)
        if read_idx is None:
            # Stage 1: never touched the source at all.
            return (f"HARNESS: you are finishing step {step.id} without having read "
                    f"the output of the earlier step(s) this plan builds on: "
                    f"{', '.join(priors)}. Those files are the record of what the "
                    f"earlier steps actually produced. Read the file now with "
                    f"read_file and redo this step using its real contents. If this "
                    f"step genuinely does not depend on that output, answer DONE: "
                    f"again and say why in one sentence. Your own step's output "
                    f"file ({step.output_path}) is not one of these sources: the "
                    f"harness writes it after you finish, it does not exist yet, "
                    f"so do not try to read it.")
        if any(s.tool and not names_prior(s) for s in sub_steps[read_idx + 1:]):
            return None  # real work happened after the read: grounded
        # Before demanding the redo, look at the deliverable (surgical
        # fix): if the step's own Thens all machine-check as passing
        # right now, a redo can only risk making things worse.
        if verify_now is not None:
            try:
                if verify_now():
                    return None
            except Exception:
                pass  # a broken check suppresses nothing
        # Stage 2 (the 20261002-192409 field sequence): the source was read,
        # possibly only because stage 1 forced it, and then DONE was
        # re-asserted with nothing changed. Reading is not redoing.
        return (f"HARNESS: you read {', '.join(priors)} but you have not changed "
                f"anything since reading it. Redo this step now using what the "
                f"file contains: write or edit the deliverable from its real "
                f"contents. If nothing genuinely needs to change, answer DONE: "
                f"again and say why in one sentence. Your own step's output "
                f"file ({step.output_path}) is written by the harness after "
                f"you finish and does not exist yet; it is not a source to "
                f"read.")

    return gate


def _run_step_verified(task: str, step: PlanStep, steps: list[PlanStep],
                     cfg: Config, chat_fn, jail, emit,
                     store=None, only_tools=None) -> bool:
    """Run one plan step; after the sub-run reports DONE, verify the Then
    clauses in code before advancing. A failed verification warns and retries
    the step once (bounded by cfg.verify_retries) with the exact failure as
    feedback. If verification still fails, the failure is downgraded to a
    warning: the step is accepted and the plan continues, because the verifier
    checks plan *adherence* while the task-level checker judges *correctness* —
    a wrong plan must not block right work. Verification itself costs zero
    model calls. Sub-run failures (model errors, limits) still hard-fail.

    v0.8.1 (issue #2): when a RunStore is present, every attempt's full
    record (tool calls with verbatim results, answer, verification) is
    written to .harness/runs/<run>/step-NN.md.

    v0.8.1 (plan frame): the prompt leads with FRAME_TEXT and the plan
    outline (every step, its status, done steps' output files), then
    repeats the current step as the operative block at the end. The
    outline replaces both the old 250-char context list and the store's
    separate path block: summaries and paths now ride with the step they
    describe."""
    from . import verify as _verify
    vctx = _verify.VerifyContext(
        jail=jail, step_id=step.id, task=task,
        prior_items={s.id: list(s.source_items) for s in steps if s.source_items})
    feedback = ""
    last_vres = None
    attempts: list[dict] = []
    n_steps = len(steps)
    thens = [t.strip() for t in step.done_when.split("\n") if t.strip()]

    def verify_now() -> bool:
        """True only when the step's Thens machine-check as passing right
        now AND at least one passing check is content-bearing.

        Attested clauses are not evidence, and neither is a bare
        existence pass: the stub that founded stage 2 satisfied
        `"file" exists` while promising its content "to be filled in".
        The checks that answer stage 2's suspicion measure content:
        contains, contains_exactly, covers (against the source items),
        has_lines. See _grounding_gate."""
        if not thens:
            return False
        res = _verify.verify_step(thens, vctx)
        if not res.checks or not all(
                c.ok and c.verifier != "attest" for c in res.checks):
            return False
        return any(c.verifier in ("contains", "contains_exactly",
                                  "covers", "has_lines")
                   for c in res.checks)

    gate = _grounding_gate(step, steps, verify_now=verify_now)

    def persist(status: str):
        if attempts:
            counts: dict[str, int] = {}
            n_lines = 0
            for s in attempts[-1].get("sub_steps") or []:
                if s.tool:
                    counts[s.tool] = counts.get(s.tool, 0) + 1
                    n_lines += len((s.result or "").splitlines())
            if counts:
                step.tool_summary = (
                    ", ".join(f"{t} x{n}" for t, n in counts.items())
                    + f"; {n_lines} lines of tool output")
        if store is None:
            return
        try:
            store.write_step(
                step_id=step.id, instruction=step.instruction,
                given=step.given, done_when=step.done_when,
                answer=step.result, sub_steps=[], status=status,
                verify_checks=step.verify, verify_waived=step.verify_waived,
                attempts=attempts or None)
        except Exception:
            pass  # the store is a record, never a reason to fail real work

    for _ in range(1 + cfg.verify_retries):
        prompt = (f"Overall goal: {task}\n"
                  f"You are executing step {step.id} of {n_steps}. "
                  f"Do ONLY this step, nothing else.\n\n")
        prompt += FRAME_TEXT + "\n\n"
        prompt += _plan_outline(steps, step.id) + "\n"
        if step.given:
            prompt += f"Starting state: {step.given}\n"
        if step.intent:
            prompt += f"Intent of this step: {step.intent}\n"
        prompt += (f"Step: {step.instruction}\n"
                   f"This step is done when: {step.done_when or 'its instruction is complete'}\n")
        if store is not None and step.output_path:
            # Wording matters (field, 2026-10-03): "Your full output will be
            # stored at..." read as a write instruction and a step wrote its
            # deliverable into its own record file. Say who does the storing.
            prompt += (f"The harness will store your full output at "
                       f"{step.output_path} for later steps. Do not write to "
                       f"that file yourself, and do not try to read it: it "
                       f"does not exist yet. The harness writes it after you "
                       f"finish. The files you can read are the earlier "
                       f"steps' outputs, named beside the [done] steps "
                       f"above.\n")
        if feedback:
            prompt += (f"Your previous attempt failed verification: {feedback} "
                       f"Fix exactly this and try again.\n")
        prompt += 'When this step is complete, answer starting with "DONE:".'

        sub_cfg = replace(cfg, max_steps=cfg.step_max_steps)
        try:
            r = loop.run(prompt, sub_cfg, chat_fn=chat_fn,
                         on_step=lambda s: emit("step_sub", step, s),
                         done_gate=gate, only_tools=only_tools)
        except Exception as e:
            step.status = "failed"
            step.result = f"harness error: {e}"
            attempts.append({"sub_steps": [], "answer": step.result,
                             "verify_checks": []})
            persist("failed")
            emit("step_failed", step)
            return False
        step.result = (r.answer or "")[:800]
        if r.status != "done":
            step.status = "failed"
            attempts.append({"sub_steps": list(r.steps), "answer": r.answer or "",
                             "verify_checks": []})
            persist("failed")
            emit("step_failed", step)
            return False
        step.source_items = _verify.collect_step_items(list(r.steps))
        vres = _verify.verify_step(thens, vctx)
        step.verify = [{"then": c.then, "verifier": c.verifier,
                        "ok": c.ok, "detail": c.detail} for c in vres.checks]
        attempts.append({"sub_steps": list(r.steps), "answer": r.answer or "",
                         "verify_checks": list(step.verify)})
        emit("step_verify", step, vres)
        if vres.ok:
            step.status = "done"
            persist("done")
            emit("step_done", step)
            return True
        last_vres = vres
        feedback = "; ".join(c.detail for c in vres.checks if not c.ok)
    # Retries exhausted: downgrade to a warning and continue. The step ran;
    # only its acceptance criteria are disputed (often a wrong plan, not
    # wrong work) — the task-level checker remains the backstop.
    step.status = "done"
    step.verify_waived = True
    persist("done")
    emit("step_verify_waived", step, last_vres)
    emit("step_done", step)
    return True


def execute_plan(task: str, steps: list[PlanStep], cfg: Config, chat_fn=None,
                 on_event=None, only_tools=None) -> PlannedRun:
    """Execute an approved plan, one scoped step at a time. on_event(kind, *args)
    reports ("step_start", step), ("step_sub", step, sub), ("step_verify", step,
    StepVerify), ("step_verify_waived", step, StepVerify), ("step_done", step),
    ("step_failed", step). A waived verification still counts the step done."""
    chat_fn, jail, _registry = _chat_and_tools(cfg, chat_fn)

    def emit(kind, *args):
        if on_event:
            on_event(kind, *args)

    store = None
    try:
        from .runstore import RunStore
        store = RunStore(cfg.workspace)
        for s in steps:
            s.output_path = store.rel_path(s.id)
    except Exception:
        store = None  # record-keeping must never block execution

    for step in steps:
        emit("step_start", step)
        if not _run_step_verified(task, step, steps,
                                  cfg, chat_fn, jail, emit, store=store,
                                  only_tools=only_tools):
            break

    status = "done" if steps and all(s.status == "done" for s in steps) else "step_failed"

    summary = (f"Overall goal: {task}\nPlan execution ended: {status}.\n"
               + "\n".join(f"Step {s.id} [{s.status}]: {s.instruction}"
                            + (f" -> {s.result[:200]}" if s.result else "")
                            for s in steps)
               + '\nSummarize the outcome in 2-4 sentences, starting with "DONE:".')
    try:
        msg = chat_fn([{"role": "user", "content": summary}], None)
        answer = (msg.get("content") or "").strip() or "(no summary)"
    except Exception as e:
        answer = f"(summary call failed: {e})"
    return PlannedRun(status, answer, steps,
                      run_id=store.run_id if store is not None else "")


def run_planned(task: str, cfg: Config, chat_fn=None, on_event=None) -> PlannedRun:
    """Auto mode: propose a plan and execute it in one go. The eval uses this."""
    text, result = propose(task, cfg, chat_fn)
    if text is None:
        return PlannedRun("plan_failed", f"Could not get a valid plan: {result}", [])
    if on_event:
        on_event("plan", result)
    return execute_plan(task, result, cfg, chat_fn, on_event)
