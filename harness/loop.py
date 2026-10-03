"""The agent loop: prompt -> model -> tool call -> result -> repeat.

Two tool-call paths, tried in order:
  1. native: Ollama's structured tool_calls (tests whether the model's
     native function calling actually works).
  2. fallback: a ```toolcall {json} block in the text (many small models
     emit this even when native tools are offered).

The trace records which path each step took, so you can measure native
vs fallback reliability per model.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field

from . import client as _client
from . import prompts
from . import tools as _tools

TOOLCALL_RE = re.compile(r"```toolcall\s*(\{.*?\})\s*```", re.DOTALL)


@dataclass
class Step:
    n: int
    thought: str
    tool: str | None
    args: dict | None
    result: str | None
    path: str  # native | fallback | final | error


@dataclass
class RunResult:
    status: str  # done | error_limit | max_steps | model_error
    answer: str
    steps: list[Step] = field(default_factory=list)
    native_calls: int = 0
    fallback_calls: int = 0


def _extract_fallback(content: str) -> tuple[str, dict | None]:
    m = TOOLCALL_RE.search(content or "")
    if not m:
        return content, None
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return content, None
    if not isinstance(data, dict) or "name" not in data:
        return content, None
    thought = TOOLCALL_RE.sub("", content).strip()
    return thought, {"name": data["name"], "arguments": data.get("arguments", {})}


def run(task: str, cfg, chat_fn=None, on_step=None, done_gate=None) -> RunResult:
    """Run the agent loop. chat_fn(messages, tools) -> assistant message dict;
    inject a fake for tests. on_step(step) receives each Step for live display.
    done_gate(steps) -> str | None: consulted once before a DONE: is accepted;
    returning text pushes back with that message instead (the caller decides
    what "ready to finish" requires — e.g. the planner's grounding gate)."""
    chat_fn = chat_fn or (lambda messages, tool_defs: _client.chat(
        cfg.endpoint, cfg.model, messages,
        tools=_client.to_ollama_tools(tool_defs) if tool_defs else None,
        temperature=cfg.temperature, top_p=cfg.top_p,
        timeout=cfg.request_timeout))

    jail = _tools.Jail(cfg.workspace)
    registry = _tools.make_tools(jail, cfg.blocked_substrings, cfg.exec_timeout,
                                 cfg.max_output_chars)
    tool_defs = [{k: d[k] for k in ("name", "description", "parameters")}
                 for d in registry.values()]

    messages = [
        {"role": "system", "content": prompts.build_system_prompt(tool_defs)},
        {"role": "user", "content": task},
    ]
    result = RunResult(status="done", answer="")
    errors = 0
    last_call_key: tuple | None = None  # (tool_name, canonical args) of previous step
    # What the previous tool interaction was: None (no calls yet), "ok",
    # "error", or "nudged" (repeat-after-success nudge, which asks for DONE:).
    last_outcome: str | None = None
    error_pushback_sent = False
    gate_fires = 0  # done_gate pushbacks so far (capped at 2, v0.8.1 gate v2)

    def emit(step: Step):
        result.steps.append(step)
        if on_step:
            on_step(step)

    for n in range(1, cfg.max_steps + 1):
        try:
            msg = chat_fn(messages, tool_defs)
        except Exception as e:  # model/transport failure ends the run
            result.status = "model_error"
            result.answer = f"Model call failed: {e}"
            return result

        content = (msg.get("content") or "").strip()
        native_calls = msg.get("tool_calls") or []

        tool_name, args, path = None, None, "final"
        if native_calls:
            fn = native_calls[0].get("function", {})
            tool_name, args, path = fn.get("name"), fn.get("arguments", {}), "native"
            if len(native_calls) > 1:
                content += f" [note: model made {len(native_calls)} calls; harness ran the first]"
        else:
            thought, fb = _extract_fallback(content)
            if fb:
                content, tool_name, args, path = thought, fb["name"], fb["arguments"], "fallback"

        assistant_msg: dict = {"role": "assistant", "content": msg.get("content") or ""}
        if native_calls:
            assistant_msg["tool_calls"] = native_calls
        elif tool_name is not None:
            # Fallback path: synthesize the call so history shows the action.
            assistant_msg["tool_calls"] = [{"function": {
                "name": tool_name,
                "arguments": args if isinstance(args, dict) else {}}}]
        messages.append(assistant_msg)

        if tool_name is None:
            if content.strip().upper().startswith("DONE:"):
                if last_outcome == "error" and not error_pushback_sent:
                    # The model is declaring victory over a failed action
                    # (e.g. a plan referencing a file that doesn't exist).
                    # Push back once and demand engagement with the error.
                    error_pushback_sent = True
                    emit(Step(n, content, None, None, None, "nudge"))
                    messages.append({"role": "user",
                                     "content": prompts.DONE_AFTER_ERROR_PUSHBACK})
                    continue
                if done_gate is not None and gate_fires < 2:
                    # Caller-supplied readiness check (e.g. grounding: the
                    # step never read the earlier output it builds on, or
                    # read it and changed nothing). Consulted on every
                    # DONE until it clears, capped at two pushbacks — the
                    # gate advises with teeth, it does not imprison.
                    try:
                        pushback = done_gate(list(result.steps))
                    except Exception:
                        pushback = None
                    if pushback:
                        gate_fires += 1
                        emit(Step(n, content, None, None, None, "nudge"))
                        messages.append({"role": "user", "content": pushback})
                        continue
                emit(Step(n, content, None, None, None, "final"))
                result.answer = content
                result.status = "done"
                return result
            # The model narrated instead of acting: nudge it back to work.
            emit(Step(n, content, None, None, None, "nudge"))
            messages.append({"role": "user", "content": prompts.NUDGE_PROMPT})
            continue

        # Validate and execute.
        succeeded = False
        tool_def = registry.get(tool_name)
        if tool_def is None:
            tool_result = f"ERROR: unknown tool: {tool_name!r}"
            errors += 1
            last_outcome = "error"
        else:
            try:
                args = _tools.validate_args(tool_def, args)
            except _tools.ToolError as e:
                tool_result = f"ERROR: {e}"
                errors += 1
                last_outcome = "error"
            else:
                call_key = (tool_name, json.dumps(args, sort_keys=True, default=str))
                if call_key == last_call_key and last_outcome == "ok":
                    # Repeating a success: the model means "done" but can't
                    # say it. Guide it to DONE: instead of erroring.
                    tool_result = prompts.REPEAT_SUCCESS_NUDGE
                    last_outcome = "nudged"  # further repeats are errors
                elif call_key == last_call_key:
                    tool_result = f"ERROR: {prompts.LOOP_BREAKER}"
                    errors += 1
                    last_outcome = "error"
                else:
                    last_call_key = call_key
                    try:
                        tool_result = tool_def["func"](args)
                    except _tools.Refusal as e:
                        # Safety boundary, not a failure: the model is right
                        # to stop, so DONE: afterwards is accepted.
                        tool_result = f"ERROR: {e}"
                        errors += 1
                        last_outcome = "refused"
                    except _tools.ToolError as e:
                        tool_result = f"ERROR: {e}"
                        errors += 1
                        last_outcome = "error"
                    except Exception as e:  # never let a tool crash the loop
                        tool_result = f"ERROR: tool crashed: {type(e).__name__}: {e}"[:500]
                        errors += 1
                        last_outcome = "error"
                    else:
                        last_outcome = "ok"
                        errors = 0
                        succeeded = True
                        error_pushback_sent = False

        if path == "native":
            result.native_calls += 1
        else:
            result.fallback_calls += 1
        emit(Step(n, content, tool_name, args if isinstance(args, dict) else None,
                  tool_result, path))
        # Label the result with the call that produced it — the model can't
        # connect an orphaned result to its action. Then, on success, say so
        # plainly and steer to the next move instead of leaving it guessing.
        arg_label = json.dumps(args, default=str)[:160] if isinstance(args, dict) else ""
        messages.append({"role": "tool",
                         "content": f"[result of {tool_name} {arg_label}]\n{tool_result}"})
        if succeeded:
            messages.append({"role": "user", "content": prompts.SUCCESS_STEER})

        if errors >= cfg.max_consecutive_errors:
            result.status = "error_limit"
            break
    else:
        result.status = "max_steps"

    # One final call so the run ends with a human-readable summary, not a trace.
    try:
        messages.append({"role": "user", "content": prompts.FINAL_SUMMARY_PROMPT})
        msg = chat_fn(messages, None)
        result.answer = (msg.get("content") or "").strip() or "(no summary)"
    except Exception as e:
        result.answer = f"(summary call failed: {e})"
    return result


def write_trace(path: str, task: str, cfg, result: RunResult) -> None:
    with open(path, "w") as f:
        header = {"task": task, "model": cfg.model, "endpoint": cfg.endpoint,
                  "status": result.status, "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}
        f.write(json.dumps(header) + "\n")
        for s in result.steps:
            f.write(json.dumps({"step": s.n, "path": s.path, "thought": s.thought,
                                "tool": s.tool, "args": s.args,
                                "result": (s.result or "")[:2000]}) + "\n")
