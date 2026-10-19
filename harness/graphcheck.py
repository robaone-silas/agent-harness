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
# A whole covers clause, with the retired verifier's full target
# grammar: an optional contents-of prefix, an optional "the file"
# prefix, then a double-quoted, single-quoted, backticked, or bare
# path, "the files", a step reference, and nothing else. parse_then
# no longer knows this shape, so the graph recognizes it here to
# keep a saved plan's listing edge and its source validation.
_COVERS_CLAUSE = re.compile(
    r"^\s*(?:the\s+contents?\s+of\s+|contents?\s+of\s+)?"
    r"(?:the\s+file\s+)?"
    r"(?:\"([^\"]*)\"|'([^']*)'|`([^`]*)`|([\w./\\-]+))"
    r"\s+covers?\s+the\s+files\s+"
    r"(?:from|listed\s+(?:in|by))\s+step\s+(\d+)\s*[.!]?\s*$",
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
    for step in steps:
        produced, consumed = _classify_when(step.instruction or "",
                                            workspace_set)
        thens = [text.strip() for text in (step.done_when or "").split("\n")
                 if text.strip()]
        producing = bool(_PRODUCE_VERB.search(step.instruction or ""))
        step_inputs: list[tuple[str, str, str]] = []
        seen_inputs: set[tuple[str, str]] = set()

        def record_input(kind, source_key, label):
            if (kind, source_key) not in seen_inputs:
                seen_inputs.add((kind, source_key))
                step_inputs.append((kind, source_key, label))

        for clause in thens:
            parsed = _verify.parse_then(clause)
            if parsed and parsed[0] in ("covers", "indexes",
                                        "accounts_for"):
                subject_file = parsed[1]
                source_match = _COVERS_SOURCE.search(clause) \
                    or _INDEXES_SOURCE.search(clause) \
                    or _ACCOUNTS_SOURCE.search(clause)
                if source_match:
                    source_step_id = int(source_match.group(1))
                    record_input("listing", str(source_step_id),
                                 f"files from step {source_step_id}")
                if subject_file:
                    if producing and subject_file not in produced:
                        produced.append(subject_file)
                    elif not producing:
                        record_input("file", subject_file, subject_file)
                continue
            covers_match = _COVERS_CLAUSE.match(clause)
            if covers_match:
                # A covers clause in a saved plan: parse_then returns
                # None now that the verb is retired, but the clause
                # still consumes the listing step's files, and its
                # target is still the step's deliverable. The target
                # is whichever of the four grammar alternatives
                # matched (the retired verifier's _pick rule).
                source_step_id = int(covers_match.group(5))
                record_input("listing", str(source_step_id),
                             f"files from step {source_step_id}")
                target = next((alternative for alternative
                               in covers_match.groups()[:4]
                               if alternative is not None), None)
                if target:
                    if producing and target not in produced:
                        produced.append(target)
                    elif not producing:
                        record_input("file", target, target)
                continue
            # Step references in a non-covers Then still consume the
            # referenced step's stored output.
            for step_ref in _STEP_REF.finditer(clause):
                source_step_id = int(step_ref.group(1))
                record_input("record", str(source_step_id),
                             f"step {source_step_id} output")
            subject_file = parsed[1] if parsed else None
            if subject_file:
                if producing and subject_file not in produced:
                    produced.append(subject_file)
                elif not producing:
                    record_input("file", subject_file, subject_file)
        for text in (step.given or "", step.instruction or ""):
            for step_ref in _STEP_REF.finditer(text):
                source_step_id = int(step_ref.group(1))
                record_input("record", str(source_step_id),
                             f"step {source_step_id} output")
        for name in _planlint._mentions(step.given or ""):
            record_input("file", name, name)
        for name in consumed:
            record_input("file", name, name)
        if _is_listing(step):
            listing_steps.add(step.id)
            produced.append(f"files from step {step.id}")
        produced_by_step[step.id] = produced
        for name in produced:
            if not name.startswith("files from step "):
                produced_by_file.setdefault(name, []).append(step.id)
        raw_inputs[step.id] = step_inputs

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

    for step in steps:
        node = GraphNode(step_id=step.id,
                         title=getattr(step, "title", "") or "",
                         outputs=list(produced_by_step[step.id]))
        for kind, source_key, label in raw_inputs[step.id]:
            if label not in node.inputs:
                node.inputs.append(label)
            if kind in ("record", "listing"):
                source_step_id = int(source_key)
                if not 1 <= source_step_id < step.id:
                    if source_step_id == step.id:
                        reason = f"step {source_step_id} is itself"
                    elif source_step_id <= total:
                        reason = f"step {source_step_id} has not run yet"
                    else:
                        reason = f"there is no step {source_step_id}"
                    add_finding(
                        step.id, "error", "unresolved_step_reference",
                        f"Step {step.id}: it consumes '{label}', but "
                        f"{reason}, so that field does not exist when "
                        f"this step runs.")
                    continue
                if kind == "listing" and source_step_id not in listing_steps:
                    add_finding(
                        step.id, "error", "covers_source_not_a_listing",
                        f"Step {step.id}: a completeness clause draws "
                        f"its file list from step {source_step_id}, "
                        f"but step {source_step_id} produces no file "
                        f"list (its When does not list or gather "
                        f"files), so the completeness check has no "
                        f"source set to check against.")
                    continue
                add_edge(source_step_id, step.id, label, kind)
            else:  # a file field
                consumed_files.add(source_key)
                producers = produced_by_file.get(source_key, [])
                earlier = [producer_id for producer_id in producers
                           if producer_id < step.id]
                later = [producer_id for producer_id in producers
                         if producer_id > step.id]
                if earlier:
                    add_edge(earlier[-1], step.id, source_key, "file")
                elif later:
                    add_edge(later[0], step.id, source_key, "file")
                    add_finding(
                        step.id, "warn", "consumed_before_produced",
                        f"Step {step.id}: it consumes '{source_key}', "
                        f"which only step {later[0]} produces, and "
                        f"that step runs later. The data flow points "
                        f"backward: reorder the steps or move the "
                        f"consumption.")
                elif source_key in task_mentions or (
                        workspace_set is not None
                        and source_key in workspace_set):
                    pass  # an external input: the task or the workspace
                elif workspace_set is not None and "/" not in source_key:
                    add_finding(
                        step.id, "warn", "unsatisfied_input",
                        f"Step {step.id}: it reads '{source_key}', "
                        f"which no earlier step produces, the task "
                        f"does not name, and the workspace does not "
                        f"contain. The step's input has no source.")
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
