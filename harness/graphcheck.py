"""Prototype graph checker: a plan as a data-flow graph.

Supplements planlint. Where lint checks plan text rule by rule, this
module builds the graph the plan implies: each step is a node with
declared input and output fields, and an edge runs from node A to
node B when B consumes a field A produces. A field in this prototype
is a workspace file, the file list a listing step produces (the field
a covers clause consumes), or a step's stored output, which later
steps consume when they refer to "step N".

The engine idea behind the prototype: a node can only succeed when it
can produce what the next node needs. At plan time that becomes
structural checks on the graph:

  unresolved_step_reference (ERROR): a step refers to "step N" where
                     N is itself, a later step, or no step at all, so
                     the field it wants to consume does not exist when
                     the step runs.
  covers_source_not_a_listing (ERROR): a covers clause draws its file
                     list from a step that produces no list. The edge
                     kind is wrong: the consumer requires a listing
                     field and the producer offers none.
  consumed_before_produced (WARN): a step consumes a file whose only
                     producer is a later step. The graph has the edge;
                     the plan's order breaks it.
  unsatisfied_input  (WARN, workspace-aware): a step reads a root file
                     that no earlier step produces, the task does not
                     name, and the workspace does not contain. Needs
                     the workspace root file set; without it the check
                     stays off, like planlint's unaccounted_file.
  task_output_never_produced (WARN): the task names a file that is
                     not in the workspace and no step produces or
                     consumes, so the plan never makes the deliverable
                     the task asked for.

Scope limits, stated plainly. The graph is derived from plan text by
mentions, the same over-approximation planlint uses, with verb
locality (the nearest verb before a mention in a When decides whether
the step produces or consumes that file). And the checker is
plan-time only: it validates the graph a plan declares, not what a
run does with it. Issue #22 (the executor wrote the deliverable's
content to the wrong path in a well-formed plan) is invisible to this
module by design; catching that class needs the run-time side of the
engine, which this prototype does not build.

Integration is display-only: run.py prints the graph and its findings
at plan approval next to the lint findings. Nothing here retries the
planner or blocks execution.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import planlint as _planlint
from . import verify as _verify
from .planlint import LintFinding

_STEP_REF = re.compile(r"\bstep\s+(\d+)\b", re.IGNORECASE)
# The source step named by a covers clause (kept local to graphcheck:
# the graph still understands the clause shape when one appears in a
# saved plan, even though the verb is no longer offered).
_COVERS_SOURCE = re.compile(
    r"covers?\s+the\s+files\s+(?:from|listed\s+(?:in|by))\s+step\s+(\d+)",
    re.IGNORECASE,
)
# The source step named by an indexes clause (graphcheck's listing
# edge treats indexes exactly like covers: both consume the listing
# field a listing step produces).
_INDEXES_SOURCE = re.compile(
    r"indexes\s+the\s+files\s+(?:from|listed\s+(?:in|by))\s+step\s+(\d+)",
    re.IGNORECASE,
)
# The source step named by an accounts_for clause: same listing
# consumption, names only.
_ACCOUNTS_SOURCE = re.compile(
    r"accounts?\s+for\s+the\s+files\s+(?:from|listed\s+(?:in|by))\s+step\s+(\d+)",
    re.IGNORECASE,
)
# A step produces a file list when its When lists or gathers files.
# Wider than planlint's placement heuristic: gathering or scanning
# the files produces the same field a covers clause consumes.
_LISTING_PRODUCER = re.compile(
    r"\blist(?:s|ing|ed)?\b|"
    r"\b(?:gather|collect|enumerat\w*|scan|find)\b[^.\n]{0,30}\bfiles?\b",
    re.IGNORECASE,
)
# Verb locality sets for classifying a When's file mentions. The
# produce set is planlint's, plus "replace" (Maya's shape: replace the
# contents of a workspace file produces that file's new contents).
_PRODUCE_VERB = re.compile(
    r"\b(?:writ\w*|creat\w*|sav\w*|produc\w*|compil\w*|generat\w*|"
    r"draft\w*|append\w*|replac\w*|runs?|running|execut\w*)\b",
    re.IGNORECASE,
)
_READ_VERB = re.compile(
    r"\b(?:read\w*|us(?:e|es|ed|ing)\b|from|load\w*|open\w*|check\w*|"
    r"review\w*|inspect\w*|examin\w*)\b",
    re.IGNORECASE,
)


@dataclass
class GraphEdge:
    producer: int
    consumer: int
    field: str
    kind: str  # "file" | "listing" | "record"

    def as_dict(self) -> dict:
        return {"from": self.producer, "to": self.consumer,
                "field": self.field, "kind": self.kind}


@dataclass
class GraphNode:
    step_id: int
    title: str = ""
    inputs: list = field(default_factory=list)   # field labels, in order
    outputs: list = field(default_factory=list)  # field labels, in order

    def as_dict(self) -> dict:
        return {"step": self.step_id, "title": self.title,
                "inputs": list(self.inputs), "outputs": list(self.outputs)}


@dataclass
class Graph:
    nodes: list = field(default_factory=list)
    edges: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"nodes": [n.as_dict() for n in self.nodes],
                "edges": [e.as_dict() for e in self.edges]}


def _mention_spans(text: str) -> list[tuple[str, int]]:
    """(name, position) for each distinct file-ish mention, first seen.

    Same mention definition as planlint._mentions (quoted spans plus
    bare path-like tokens), with positions kept so a When's mentions
    can be classified by the verb nearest before them."""
    found: dict[str, int] = {}
    for m in _planlint._QUOTED.finditer(text or ""):
        name = m.group(1).strip()
        if name and name not in found:
            found[name] = m.start(1)
    for m in _planlint._BARE_PATH.finditer(text or ""):
        name = m.group(1).rstrip(".")
        if name and name not in found:
            found[name] = m.start(1)
    return sorted(found.items(), key=lambda kv: kv[1])


def _classify_when(when: str, workspace_set) -> tuple[list, list]:
    """Split a When's file mentions into (produced, consumed).

    The nearest verb before a mention decides: a produce verb makes
    the file an output of this step, a read verb makes it an input.
    With no verb before it, a mention is an output when the step
    produces anything at all, else an input. Workspace files are
    never outputs of a step that merely mentions them while writing
    (they are sources), matching planlint's output approximation.
    Move destinations are always outputs."""
    verbs: list[tuple[int, str]] = []
    for m in _PRODUCE_VERB.finditer(when or ""):
        verbs.append((m.start(), "out"))
    for m in _READ_VERB.finditer(when or ""):
        verbs.append((m.start(), "in"))
    verbs.sort()
    any_produce = any(kind == "out" for _pos, kind in verbs)
    produced: list[str] = []
    consumed: list[str] = []
    for name, pos in _mention_spans(when):
        kind = None
        for vpos, vkind in verbs:
            if vpos < pos:
                kind = vkind
            else:
                break
        if kind is None:
            kind = "out" if any_produce else "in"
        if kind == "out" and workspace_set is not None \
                and name in workspace_set:
            kind = "in"
        (produced if kind == "out" else consumed).append(name)
    for dest in sorted(_planlint._move_destinations(when or "")):
        if dest not in produced:
            produced.append(dest)
        if dest in consumed:
            consumed.remove(dest)
    return produced, consumed


def _is_listing(step) -> bool:
    return bool(_LISTING_PRODUCER.search(step.instruction or "")
                or _planlint._GENERIC_LISTING.search(step.instruction or "")
                or _planlint._GENERIC_LISTING.search(step.done_when or ""))


def _analyze(task: str, steps: list, workspace_files=None):
    workspace_set = _planlint._workspace_file_set(workspace_files)
    task_mentions = _planlint._mentions(task)
    total = len(steps)

    # First pass: what each step produces, and its raw consumptions.
    produced_by_step: dict[int, list] = {}
    produced_by_file: dict[str, list] = {}
    listing_steps: set[int] = set()
    raw_inputs: dict[int, list] = {}  # step id -> [(kind, key, label)]
    for s in steps:
        produced, consumed = _classify_when(s.instruction or "",
                                            workspace_set)
        thens = [t.strip() for t in (s.done_when or "").split("\n")
                 if t.strip()]
        producing = bool(_PRODUCE_VERB.search(s.instruction or ""))
        items: list[tuple[str, str, str]] = []
        seen: set[tuple[str, str]] = set()

        def add(kind, key, label):
            if (kind, key) not in seen:
                seen.add((kind, key))
                items.append((kind, key, label))

        for t in thens:
            parsed = _verify.parse_then(t)
            if parsed and parsed[0] in ("covers", "indexes",
                                        "accounts_for"):
                src = _COVERS_SOURCE.search(t) \
                    or _INDEXES_SOURCE.search(t) \
                    or _ACCOUNTS_SOURCE.search(t)
                if src:
                    n = int(src.group(1))
                    add("listing", str(n), f"files from step {n}")
                if parsed[1]:
                    if producing and parsed[1] not in produced:
                        produced.append(parsed[1])
                    elif not producing:
                        add("file", parsed[1], parsed[1])
                continue
            # Step references in a non-covers Then still consume the
            # referenced step's stored output.
            for m in _STEP_REF.finditer(t):
                n = int(m.group(1))
                add("record", str(n), f"step {n} output")
            if parsed and parsed[1]:
                if producing and parsed[1] not in produced:
                    produced.append(parsed[1])
                elif not producing:
                    add("file", parsed[1], parsed[1])
        for text in (s.given or "", s.instruction or ""):
            for m in _STEP_REF.finditer(text):
                n = int(m.group(1))
                add("record", str(n), f"step {n} output")
        for name in _planlint._mentions(s.given or ""):
            add("file", name, name)
        for name in consumed:
            add("file", name, name)
        if _is_listing(s):
            listing_steps.add(s.id)
            produced.append(f"files from step {s.id}")
        produced_by_step[s.id] = produced
        for name in produced:
            if not name.startswith("files from step "):
                produced_by_file.setdefault(name, []).append(s.id)
        raw_inputs[s.id] = items

    # Second pass: resolve consumptions into edges and findings.
    nodes: list[GraphNode] = []
    edges: list[GraphEdge] = []
    edge_keys: set[tuple] = set()
    findings: list[LintFinding] = []
    finding_keys: set[tuple] = set()
    consumed_files: set[str] = set()

    def add_edge(producer, consumer, label, kind):
        key = (producer, consumer, label, kind)
        if producer != consumer and key not in edge_keys:
            edge_keys.add(key)
            edges.append(GraphEdge(producer, consumer, label, kind))

    def add_finding(step_id, level, code, detail):
        key = (step_id, code, detail)
        if key not in finding_keys:
            finding_keys.add(key)
            findings.append(LintFinding(step_id, level, code, detail))

    for s in steps:
        node = GraphNode(step_id=s.id, title=getattr(s, "title", "") or "",
                         outputs=list(produced_by_step[s.id]))
        for kind, key, label in raw_inputs[s.id]:
            if label not in node.inputs:
                node.inputs.append(label)
            if kind in ("record", "listing"):
                n = int(key)
                if not 1 <= n < s.id:
                    if n == s.id:
                        why = f"step {n} is itself"
                    elif n <= total:
                        why = f"step {n} has not run yet"
                    else:
                        why = f"there is no step {n}"
                    add_finding(
                        s.id, "error", "unresolved_step_reference",
                        f"Step {s.id}: it consumes '{label}', but "
                        f"{why}, so that field does not exist when "
                        f"this step runs.")
                    continue
                if kind == "listing" and n not in listing_steps:
                    add_finding(
                        s.id, "error", "covers_source_not_a_listing",
                        f"Step {s.id}: a completeness clause draws "
                        f"its file list from step {n}, but step {n} "
                        f"produces no file list (its When does not "
                        f"list or gather files), so the completeness "
                        f"check has no source set to check against.")
                    continue
                add_edge(n, s.id, label, kind)
            else:  # a file field
                consumed_files.add(key)
                producers = produced_by_file.get(key, [])
                earlier = [p for p in producers if p < s.id]
                later = [p for p in producers if p > s.id]
                if earlier:
                    add_edge(earlier[-1], s.id, key, "file")
                elif later:
                    add_edge(later[0], s.id, key, "file")
                    add_finding(
                        s.id, "warn", "consumed_before_produced",
                        f"Step {s.id}: it consumes '{key}', which "
                        f"only step {later[0]} produces, and that "
                        f"step runs later. The data flow points "
                        f"backward: reorder the steps or move the "
                        f"consumption.")
                elif key in task_mentions or (
                        workspace_set is not None
                        and key in workspace_set):
                    pass  # an external input: the task or the workspace
                elif workspace_set is not None and "/" not in key:
                    add_finding(
                        s.id, "warn", "unsatisfied_input",
                        f"Step {s.id}: it reads '{key}', which no "
                        f"earlier step produces, the task does not "
                        f"name, and the workspace does not contain. "
                        f"The step's input has no source.")
        nodes.append(node)

    if steps:
        all_produced = set(produced_by_file)
        for name in sorted(task_mentions):
            # The task-output check is stricter about what counts as
            # a named file than the mention scan: a quoted value the
            # task asserts ('Total: 42') is a value, not a deliverable.
            # Only path-like mentions (no whitespace, and a slash or a
            # file extension) can be outputs nobody produced.
            if any(c.isspace() for c in name) or (
                    "/" not in name
                    and not re.search(r"\.[A-Za-z0-9]{1,8}$", name)):
                continue
            if workspace_set is not None and name in workspace_set:
                continue
            if name in all_produced or name in consumed_files:
                continue
            add_finding(
                steps[-1].id, "warn", "task_output_never_produced",
                f"Plan: the task names '{name}', but no step "
                f"produces it and no step consumes it. If '{name}' "
                f"is the deliverable, the plan never makes it.")
    return Graph(nodes=nodes, edges=edges), findings


def build_graph(task: str, steps: list, workspace_files=None) -> Graph:
    """Build the plan's data-flow graph (nodes, edges, fields).

    `steps` are PlanStep objects; `workspace_files` optionally gives
    the workspace root file set (names, or a workspace directory
    path), which decides which file inputs are external."""
    graph, _findings = _analyze(task, steps, workspace_files)
    return graph


def check_plan(task: str, steps: list,
               workspace_files=None) -> list[LintFinding]:
    """Check the plan's graph structure. Findings share planlint's
    shape (step_id, level, code, detail). Display-only in this
    prototype: an "error" marks a structurally broken edge, it does
    not trigger a planner retry."""
    _graph, findings = _analyze(task, steps, workspace_files)
    return findings
