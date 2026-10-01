#!/usr/bin/env python3
"""Harness logic tests with a scripted fake model. No Ollama needed.

Run: python3 tests/test_loop.py
"""
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness import loop
from harness.config import Config


def make_cfg(**kw):
    d = tempfile.mkdtemp(prefix="harness-test-")
    base = dict(workspace=d, max_steps=8, max_consecutive_errors=3)
    base.update(kw)
    return Config(**base), d


def scripted(responses):
    """responses: list of assistant message dicts; the summary call gets a canned reply."""
    it = iter(responses)

    def fake(messages, tool_defs):
        try:
            return next(it)
        except StopIteration:
            return {"role": "assistant", "content": "SUMMARY: stopped."}
    return fake


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        raise AssertionError(name)


def t_direct_answer():
    cfg, d = make_cfg()
    fake = scripted([{"role": "assistant", "content": "DONE: nothing to do."}])
    r = loop.run("say hi", cfg, chat_fn=fake)
    check("direct-answer status", r.status == "done", r.status)
    check("direct-answer text", r.answer == "DONE: nothing to do.")
    check("direct-answer no tools", r.native_calls == 0 and r.fallback_calls == 0)
    shutil.rmtree(d)


def t_native_tool_then_done():
    cfg, d = make_cfg()
    fake = scripted([
        {"role": "assistant", "content": "I'll list the workspace.",
         "tool_calls": [{"function": {"name": "list_dir", "arguments": {"path": "."}}}]},
        {"role": "assistant", "content": "DONE: workspace is empty."},
    ])
    r = loop.run("list files", cfg, chat_fn=fake)
    check("native status", r.status == "done", r.status)
    check("native count", r.native_calls == 1, str(r.native_calls))
    check("native result fed back", "(empty)" in r.steps[0].result, r.steps[0].result)
    shutil.rmtree(d)


def t_fallback_block():
    cfg, d = make_cfg()
    body = ('I need to write the file.\n'
            '```toolcall {"name": "write_file", "arguments": '
            '{"path": "a.txt", "content": "xyz"}}```')
    fake = scripted([
        {"role": "assistant", "content": body},
        {"role": "assistant", "content": "DONE: wrote it."},
    ])
    r = loop.run("write a.txt", cfg, chat_fn=fake)
    check("fallback status", r.status == "done", r.status)
    check("fallback count", r.fallback_calls == 1, str(r.fallback_calls))
    check("fallback file written", Path(d, "a.txt").read_text() == "xyz")
    shutil.rmtree(d)


def t_unknown_tool_recovery():
    cfg, d = make_cfg()
    fake = scripted([
        {"role": "assistant", "content": "Trying.",
         "tool_calls": [{"function": {"name": "frobnicate", "arguments": {}}}]},
        # DONE: over an error -> pushback once, then accepted on reassertion.
        {"role": "assistant", "content": "DONE: recovered."},
        {"role": "assistant", "content": "DONE: giving up, no such tool."},
    ])
    r = loop.run("do thing", cfg, chat_fn=fake)
    check("unknown-tool status", r.status == "done", r.status)
    check("unknown-tool error surfaced", "unknown tool" in r.steps[0].result,
          r.steps[0].result)
    check("pushback fired once",
          sum(1 for s in r.steps if s.path == "nudge") == 1)
    shutil.rmtree(d)


def t_jail_escape_blocked():
    cfg, d = make_cfg()
    fake = scripted([
        {"role": "assistant", "content": "Reading secrets.",
         "tool_calls": [{"function": {"name": "read_file",
                                      "arguments": {"path": "../../etc/passwd"}}}]},
        {"role": "assistant", "content": "DONE: blocked, as expected."},
    ])
    r = loop.run("read /etc/passwd", cfg, chat_fn=fake)
    check("jail status", r.status == "done", r.status)
    check("jail blocked", "escapes workspace" in r.steps[0].result, r.steps[0].result)
    shutil.rmtree(d)


def t_blocked_command():
    cfg, d = make_cfg()
    fake = scripted([
        {"role": "assistant", "content": "Oops.",
         "tool_calls": [{"function": {"name": "exec",
                                      "arguments": {"command": "rm -rf / tmp"}}} ]},
        {"role": "assistant", "content": "DONE: refused."},
    ])
    r = loop.run("delete everything", cfg, chat_fn=fake)
    check("blocked-cmd refused", "blocked command pattern" in r.steps[0].result,
          r.steps[0].result)
    shutil.rmtree(d)


def t_error_limit():
    cfg, d = make_cfg()
    bad = {"role": "assistant", "content": "Again.",
           "tool_calls": [{"function": {"name": "nope", "arguments": {}}}]}
    fake = scripted([bad, bad, bad])  # 3rd error trips the limit; next call is summary
    r = loop.run("fail loop", cfg, chat_fn=fake)
    check("error-limit status", r.status == "error_limit", r.status)
    check("error-limit summary", "SUMMARY" in r.answer, r.answer)
    shutil.rmtree(d)


def t_max_steps():
    cfg, d = make_cfg(max_steps=2)
    busy = {"role": "assistant", "content": "Working.",
            "tool_calls": [{"function": {"name": "exec",
                                         "arguments": {"command": "echo hi"}}}]}
    fake = scripted([busy, busy])
    r = loop.run("busy work", cfg, chat_fn=fake)
    check("max-steps status", r.status == "max_steps", r.status)
    check("max-steps count", len([s for s in r.steps if s.tool]) == 2)
    shutil.rmtree(d)


def t_exec_roundtrip():
    cfg, d = make_cfg()
    fake = scripted([
        {"role": "assistant", "content": "Running.",
         "tool_calls": [{"function": {"name": "exec",
                                      "arguments": {"command": "echo hello && pwd"}}} ]},
        {"role": "assistant", "content": "DONE."},
    ])
    r = loop.run("echo test", cfg, chat_fn=fake)
    check("exec ran in jail", f"exit=0\nhello\n{d}" in r.steps[0].result,
          r.steps[0].result[:120])
    shutil.rmtree(d)


def t_nudge_on_chatter():
    cfg, d = make_cfg()
    fake = scripted([
        {"role": "assistant", "content": "I will create the file now."},
        {"role": "assistant", "content": "Creating.",
         "tool_calls": [{"function": {"name": "write_file",
                                      "arguments": {"path": "n.txt", "content": "x"}}}]},
        {"role": "assistant", "content": "DONE: created n.txt."},
    ])
    r = loop.run("write n.txt", cfg, chat_fn=fake)
    check("nudge status", r.status == "done", r.status)
    check("nudge step recorded", any(s.path == "nudge" for s in r.steps),
          str([s.path for s in r.steps]))
    check("nudge file written", Path(d, "n.txt").read_text() == "x")
    shutil.rmtree(d)


def t_done_prefix_still_finishes():
    cfg, d = make_cfg()
    fake = scripted([{"role": "assistant", "content": "done: all good."}])
    r = loop.run("say hi", cfg, chat_fn=fake)
    check("lowercase done: accepted", r.status == "done", r.status)
    shutil.rmtree(d)


def t_repeat_success_nudge():
    cfg, d = make_cfg()
    read = {"role": "assistant", "content": "Reading.",
            "tool_calls": [{"function": {"name": "list_dir", "arguments": {}}}]}
    fake = scripted([
        read,
        {"role": "assistant", "content": "Reading again.",
         "tool_calls": [{"function": {"name": "list_dir", "arguments": {}}}]},
        {"role": "assistant", "content": "DONE: listed."},
    ])
    r = loop.run("list", cfg, chat_fn=fake)
    check("repeat status", r.status == "done", r.status)
    check("repeat-after-success nudges to DONE",
          "already succeeded" in (r.steps[1].result or ""),
          (r.steps[1].result or "")[:80])
    check("nudge is not an error", not (r.steps[1].result or "").startswith("ERROR"))
    shutil.rmtree(d)


def t_repeat_after_nudge_errors():
    cfg, d = make_cfg()
    read = lambda txt: {"role": "assistant", "content": txt,
                        "tool_calls": [{"function": {"name": "list_dir",
                                                    "arguments": {}}}]}
    fake = scripted([
        read("Reading."),
        read("Reading again."),   # -> success nudge
        read("Reading once more."),  # -> breaker error
        # DONE: over the breaker error -> pushback once, then accepted.
        {"role": "assistant", "content": "DONE: listed."},
        {"role": "assistant", "content": "DONE: really listed."},
    ])
    r = loop.run("list", cfg, chat_fn=fake)
    check("second repeat errors", "Do not repeat it" in (r.steps[2].result or ""),
          (r.steps[2].result or "")[:80])
    check("run still completes", r.status == "done", r.status)
    shutil.rmtree(d)


def t_prompt_has_no_fake_result():
    from harness import prompts
    p = prompts.build_system_prompt([{"name": "exec", "description": "x",
                                      "parameters": {"properties": {}, "required": []}}])
    check("no fake tool result in prompt", "Tool result: wrote" not in p)
    check("fabrication warning present", "fabrication" in p)


def t_message_history_labels_calls():
    cfg, d = make_cfg()
    seen = []

    def fake(messages, tool_defs):
        seen.append([dict(m) for m in messages])
        if len(seen) == 1:
            return {"role": "assistant", "content": "",
                    "tool_calls": [{"function": {"name": "exec",
                                                "arguments": {"command": "echo hi"}}}]}
        return {"role": "assistant", "content": "DONE: ran it."}

    r = loop.run("run echo", cfg, chat_fn=fake)
    check("history status", r.status == "done", r.status)
    msgs = seen[1]
    assistant = [m for m in msgs if m["role"] == "assistant"][-1]
    check("assistant keeps tool_calls", "tool_calls" in assistant,
          str(list(assistant)))
    tool_msgs = [m for m in msgs if m["role"] == "tool"]
    check("tool result labeled",
          tool_msgs[0]["content"].startswith("[result of exec"),
          tool_msgs[0]["content"][:60])
    steer = [m for m in msgs if m["role"] == "user"
             and "HARNESS: that succeeded" in m["content"]]
    check("success steer sent", len(steer) == 1, str(len(steer)))
    shutil.rmtree(d)


def t_done_after_error_pushback():
    cfg, d = make_cfg()
    fake = scripted([
        {"role": "assistant", "content": "Reading.",
         "tool_calls": [{"function": {"name": "read_file",
                                      "arguments": {"path": "nope.txt"}}}]},
        {"role": "assistant", "content": "DONE: read it."},   # over a failure
        {"role": "assistant", "content": "DONE: truly blocked, no such file."},
    ])
    r = loop.run("read nope.txt", cfg, chat_fn=fake)
    check("pushback status", r.status == "done", r.status)
    pushbacks = [s for s in r.steps if s.path == "nudge"]
    check("exactly one pushback", len(pushbacks) == 1, str(len(pushbacks)))
    check("second DONE accepted", r.answer.startswith("DONE: truly"))
    shutil.rmtree(d)


def t_done_after_error_then_recovers():
    cfg, d = make_cfg()
    Path(d, "real.txt").write_text("hello")
    fake = scripted([
        {"role": "assistant", "content": "Reading wrong.",
         "tool_calls": [{"function": {"name": "read_file",
                                      "arguments": {"path": "nope.txt"}}}]},
        {"role": "assistant", "content": "DONE: read it."},   # over a failure
        {"role": "assistant", "content": "Retrying.",
         "tool_calls": [{"function": {"name": "read_file",
                                      "arguments": {"path": "real.txt"}}}]},
        {"role": "assistant", "content": "DONE: got it."},
    ])
    r = loop.run("read the file", cfg, chat_fn=fake)
    check("recovery status", r.status == "done", r.status)
    check("pushback fired once",
          sum(1 for s in r.steps if s.path == "nudge") == 1)
    shutil.rmtree(d)


def t_grep_files():
    from harness.tools import Jail, make_tools
    d = tempfile.mkdtemp(prefix="grep-test-")
    try:
        jail = Jail(d)
        reg = make_tools(jail, (), 30, 2000)
        grep = reg["grep_files"]["func"]
        Path(d, "a.txt").write_text("the quick brown fox")
        Path(d, "b.txt").write_text("I love pineapple on pizza")
        Path(d, "notes.md").write_text("pineapple notes")
        check("grep finds match", grep({"pattern": "pineapple"}) == "b.txt")
        check("grep respects glob",
              grep({"pattern": "pineapple", "glob": "*.md"}) == "notes.md")
        check("grep no match", grep({"pattern": "mango"}) == "(no matches)")
        Path(d, "sub").mkdir()
        Path(d, "sub", "c.txt").write_text("pineapple here too")
        check("grep recurses", grep({"pattern": "pineapple"}) == "b.txt\nsub/c.txt")
    finally:
        shutil.rmtree(d)


if __name__ == "__main__":
    for fn in [t_direct_answer, t_native_tool_then_done, t_fallback_block,
               t_unknown_tool_recovery, t_jail_escape_blocked, t_blocked_command,
               t_error_limit, t_max_steps, t_exec_roundtrip, t_nudge_on_chatter,
               t_done_prefix_still_finishes, t_repeat_success_nudge,
               t_repeat_after_nudge_errors, t_prompt_has_no_fake_result,
               t_message_history_labels_calls, t_done_after_error_pushback,
               t_done_after_error_then_recovers, t_grep_files]:
        fn()
    print("\nAll harness tests passed.")
