"""System prompt construction. Kept short and concrete on purpose:
a 2B model follows a format better when it sees one complete worked example
than when it reads a page of abstract rules."""
from __future__ import annotations


def build_system_prompt(tool_defs: list[dict]) -> str:
    lines = [
        "You are a small but capable coding agent. You solve tasks by calling tools, one per turn.",
        "",
        "RULES",
        "- Call exactly one tool per response, or give the final answer. Never both.",
        "- Use only the tools provided to you. Do not invent tool names.",
        "- Keep reasoning brief: 1-3 sentences before each tool call.",
        "- All file paths are relative to the workspace root. Never use absolute paths.",
        "- If a tool returns an error, read it carefully, adjust, and retry.",
        "  After 3 consecutive errors, stop and summarize what you learned.",
        '- When the task is fully done, answer with no tool call, starting with "DONE:".',
        "- Never fabricate tool calls or tool results in your text.",
        "",
        "AVAILABLE TOOLS",
    ]
    for t in tool_defs:
        params = ", ".join(
            f"{k} ({v.get('type', '?')})" for k, v in t["parameters"].get("properties", {}).items())
        req = t["parameters"].get("required", [])
        lines.append(f"- {t['name']}({params}) required=[{', '.join(req)}]: {t['description']}")
    lines += [
        "",
        "WORKED EXAMPLE (illustrative only — the real task is above)",
        "Task: Rename data.txt to data.csv.",
        "Assistant: I'll rename it with exec.",
        "[harness runs the exec tool call here and returns its real result]",
        "Assistant: DONE: Renamed data.txt to data.csv.",
        "",
        "CRITICAL: Tool calls go through the tools provided to you, and tool",
        "results come back from the harness. Never write out a tool call or a",
        '"Tool result:" line yourself — anything you type as a result is',
        "fabrication. If you catch yourself doing it, stop and make a real",
        "tool call instead.",
    ]
    return "\n".join(lines)


FINAL_SUMMARY_PROMPT = (
    "The run stopped before the task was confirmed done. "
    "Summarize briefly: what was accomplished, what remains, and the single "
    "most useful next step. Start with \"SUMMARY:\"."
)

# Sent when the model answers without calling a tool and without DONE:.
# Small models narrate instead of acting; this prods them back to work.
NUDGE_PROMPT = (
    "HARNESS: that response neither called a tool nor finished the task. "
    "Do not narrate what you will do — call a tool and do it. "
    "Answer starting with \"DONE:\" only when the work is actually complete."
)

# Fed back as a tool error when the model repeats its immediately previous
# call verbatim instead of acting on the result it already has.
LOOP_BREAKER = (
    "HARNESS: you just made this exact call and already have its result. "
    "Do not repeat it. Take a different action: try another tool, different "
    "arguments, or answer DONE: if the task is actually complete."
)

# Fed back (not as an error) when the model repeats a call that SUCCEEDED.
# Observed constantly on e2b: it redoubles the successful action instead of
# saying DONE:. Treat the repeat as "I'm done but don't know how to say it".
REPEAT_SUCCESS_NUDGE = (
    "HARNESS: that call already succeeded — repeating it changes nothing. "
    "The step is complete. Do not call any more tools; answer starting "
    "with \"DONE:\" now."
)

# Sent (once) when the model answers DONE: immediately after a tool call
# FAILED. A bare error result isn't enough — the model will declare victory
# over a contradiction, so the harness pushes back and demands engagement.
DONE_AFTER_ERROR_PUSHBACK = (
    "HARNESS: your last action FAILED, so you cannot be done yet. Look at "
    "the error: fix the action and retry, or try a different approach that "
    "avoids it. If the step is truly impossible, answer DONE: again and "
    "briefly explain what blocked you."
)

# Sent after every successful tool call. Small models don't reliably parse
# exit codes or connect an unlabeled result to its call, so the harness says
# plainly what happened and what to do next.
SUCCESS_STEER = (
    "HARNESS: that succeeded. If you are done, answer starting with "
    "\"DONE:\". Otherwise, call the next tool you need."
)
