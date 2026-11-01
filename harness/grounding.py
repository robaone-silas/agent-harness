"""DONE-gate grounding (issue #62).

Split out of planner.py: the gate that checks a finishing step
actually read the earlier steps' stored outputs before it claims
DONE, plus the covers-coverage stage and the Then-named-outputs
bridge (issue #48). planner.py re-exports _grounding_gate and
_then_named_outputs, so existing imports keep working.
"""
from __future__ import annotations

import logging
import re as _re

from .plan_model import PlanStep

logger = logging.getLogger(__name__)


def _sub_step_names_prior(sub_step, prior_output_paths: list[str]) -> bool:
    import json as _json
    if not (sub_step.tool and sub_step.args):
        return False
    dumped_arguments = _json.dumps(sub_step.args, default=str)
    return any(prior_output_path in dumped_arguments for prior_output_path in prior_output_paths)


def _find_prior_read_index(sub_steps, prior_output_paths: list[str]) -> int | None:
    return next((step_index for step_index, sub_step in enumerate(sub_steps)
                 if _sub_step_names_prior(sub_step, prior_output_paths)), None)


def _unreferenced_priors_feedback(step: PlanStep, prior_output_paths: list[str]) -> str:
    return (f"HARNESS: you are finishing step {step.id} without having read "
            f"the output of the earlier step(s) this plan builds on: "
            f"{', '.join(prior_output_paths)}. Those files are the record of what the "
            f"earlier steps actually produced. Read the file now with "
            f"read_file and redo this step using its real contents. If this "
            f"step genuinely does not depend on that output, answer DONE: "
            f"again and say why in one sentence. Your own step's output "
            f"file ({step.output_path}) is not one of these sources: the "
            f"harness writes it after you finish, it does not exist yet, "
            f"so do not try to read it.")


def _accumulate_covers_clause(then_clause: str, steps_by_id: dict,
                              target_names: list[str],
                              covered_items: list[str]) -> None:
    """Fold one Then clause into the covers targets/items, when it
    is a covers clause naming a known source step."""
    from . import verify as _verify
    then_clause = then_clause.strip()
    if not then_clause or _verify.match_verifier(then_clause) != "covers":
        return
    step_match = _re.search(r"step\s+(\d+)", then_clause)
    if not step_match:
        return
    source_step = steps_by_id.get(int(step_match.group(1)))
    if source_step is None:
        return
    parsed_clause = _verify.parse_then(then_clause)
    if parsed_clause and parsed_clause[1]:
        target_names.append(parsed_clause[1])
    for item in source_step.source_items or []:
        if item not in covered_items:
            covered_items.append(item)


def _collect_covers_targets_and_items(step: PlanStep, steps: list[PlanStep]) -> tuple[list[str], list[str]]:
    steps_by_id = {plan_step.id: plan_step for plan_step in steps}
    covered_items: list[str] = []
    target_names: list[str] = []
    for then_clause in (step.done_when or "").split("\n"):
        _accumulate_covers_clause(then_clause, steps_by_id,
                                  target_names, covered_items)
    return target_names, covered_items


def _find_missing_covered_items(covered_items: list[str], steps: list[PlanStep],
                                current_step: PlanStep, sub_steps) -> list[str]:
    import json as _json
    argument_blobs = set()
    for prior_step in steps:
        if prior_step.id < current_step.id:
            argument_blobs.update(prior_step.touched_args or set())
    for sub_step in sub_steps:
        if sub_step.tool and sub_step.args:
            argument_blobs.add(_json.dumps(sub_step.args, default=str))
    return [item for item in covered_items if not any(item in argument_blob for argument_blob in argument_blobs)]


def _missing_covers_feedback(targets: list[str], missing_items: list[str]) -> str:
    claim_prefix = f'"{targets[0]}" covers' if targets else "This step covers"
    return (f"HARNESS: {claim_prefix} the files from an earlier "
            f"listing step, but these files have not been read "
            f"yet in this run: {', '.join(missing_items)}. Work about "
            f"a file must come from its actual contents, not "
            f"from its name in a list. Read each named file now "
            f"with read_file, then redo this step from what "
            f"they contain. If a named file genuinely is not a "
            f"source for this step, answer DONE: again and say "
            f"why in one sentence.")


def _gate_missing_coverage(step: PlanStep, steps: list[PlanStep],
                           sub_steps) -> str | None:
    """Coverage-stage pushback for one gate call, or None.

    Issue #17: when the step carries a covers clause, the listing
    step's source items are compared against the files read so far
    in the run. An unread source file draws a pushback naming it.
    This stage ignores verify_now on purpose and is evaluated
    before stage 2."""
    targets, covered_items = _collect_covers_targets_and_items(step, steps)
    if not covered_items:
        return None
    missing_items = _find_missing_covered_items(covered_items, steps, step, sub_steps)
    if not missing_items:
        return None
    return _missing_covers_feedback(targets, missing_items)


def _has_work_after_prior_read(sub_steps, read_index: int, prior_output_paths: list[str]) -> bool:
    return any(sub_step.tool and not _sub_step_names_prior(sub_step, prior_output_paths)
               for sub_step in sub_steps[read_index + 1:])


def _unchanged_after_read_feedback(step: PlanStep, prior_output_paths: list[str]) -> str:
    return (f"HARNESS: you read {', '.join(prior_output_paths)} but you have not changed "
            f"anything since reading it. Redo this step now using what the "
            f"file contains: write or edit the deliverable from its real "
            f"contents. If nothing genuinely needs to change, answer DONE: "
            f"again and say why in one sentence. Your own step's output "
            f"file ({step.output_path}) is written by the harness after "
            f"you finish and does not exist yet; it is not a source to "
            f"read.")


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
    exactly as before.

    Coverage stage (issue #17): when the step carries a covers clause,
    the listing step's source items are compared against the files read
    so far in the run: completed prior steps' recorded tool calls plus
    this sub-run's. Step scope is soft, so an earlier gather step's
    reads count; what matters is that the run has actually opened every
    file the deliverable claims to cover. An unread source file draws a
    pushback naming it. This stage ignores verify_now on purpose: a
    deliverable can mention a file it never read, and mentions are not
    evidence. It shares the gate's existing budget of two pushbacks per
    sub-run (the loop cap is unchanged) and is evaluated before stage 2,
    because "read these files" is the more specific instruction when
    both apply."""
    prior_output_paths = [prior_step.output_path for prior_step in steps
                          if prior_step.id < step.id and prior_step.status == "done" and prior_step.output_path]
    if not prior_output_paths:
        return None

    def gate(sub_steps) -> str | None:
        read_index = _find_prior_read_index(sub_steps, prior_output_paths)
        if read_index is None:
            return _unreferenced_priors_feedback(step, prior_output_paths)

        coverage_feedback = _gate_missing_coverage(step, steps, sub_steps)
        if coverage_feedback is not None:
            return coverage_feedback

        if _has_work_after_prior_read(sub_steps, read_index, prior_output_paths):
            return None

        if verify_now is not None:
            try:
                if verify_now():
                    return None
            except Exception:
                # Issue #62: a raising verifier keeps the pushback
                # feedback (the old silent pass did the same), but the
                # failure is now logged instead of swallowed silently.
                logger.warning("verify_now raised in grounding gate; "
                               "keeping pushback feedback", exc_info=True)

        return _unchanged_after_read_feedback(step, prior_output_paths)

    return gate


def _constrained_subjects(clauses: list[str]) -> set[str]:
    """Files the step's clauses constrain rather than produce:
    absence clauses (the file is meant to be gone) and preservation
    clauses (the file is meant to be left as it was). Collected
    across the whole step first, so an existence clause naming the
    same file cannot resurrect it as a write target."""
    from . import verify as _verify
    constrained: set[str] = set()
    for clause in clauses:
        if _verify.absence_intent(clause) or _verify.preservation_intent(clause):
            subject_file = _verify.then_subject_file(clause)
            if subject_file:
                constrained.add(subject_file)
    return constrained


def _then_named_outputs(step: PlanStep) -> list[str]:
    """Files the step's Thens name as their subject that the step's
    When never names (issue #48): the deliverable exists only in the
    clause that checks the work, not in the instruction that does
    it, so the executing step has no stated place to write. The
    executor prompt and the blocked-write feedback both name these
    files explicitly, so the gap is bridged even when a plan that
    carries it reaches execution. Absence clauses name no output:
    the file they name is meant to be gone, not written. Neither
    does a preservation clause: the file it names is meant to be
    left exactly as it was. A file a step promises to leave alone
    or to remove is a constraint on the step, never its output,
    and that holds whichever clause states it: the constraint
    subjects are collected across the whole step first, so an
    existence clause naming the same file ("source.txt" exists
    beside "source.txt" remains unchanged) cannot resurrect it as
    a write target. "Named in the When" is judged on the
    When's extracted file mentions and its mv/cp destinations, not
    on substring matching: a destination like archive/<name> is
    named by the move even though the joined path never appears
    literally, and an output whose name sits inside a longer source
    name is not named at all."""
    from . import planlint as _planlint
    from . import verify as _verify
    named_in_when = _planlint._mentions(step.instruction or "") \
        | _planlint._move_destinations(step.instruction or "")
    clauses = [clause.strip()
               for clause in (step.done_when or "").split("\n")]
    clauses = [clause for clause in clauses if clause]
    constrained_subjects = _constrained_subjects(clauses)
    outputs: list[str] = []
    for clause in clauses:
        subject_file = _verify.then_subject_file(clause)
        if not subject_file:
            continue
        if subject_file in named_in_when or subject_file in constrained_subjects or subject_file in outputs:
            continue
        outputs.append(subject_file)
    return outputs

