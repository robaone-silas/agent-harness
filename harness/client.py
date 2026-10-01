"""Minimal Ollama chat client (native /api/chat). Pure stdlib."""
from __future__ import annotations

import json
import urllib.request
import urllib.error


class OllamaError(RuntimeError):
    pass


def chat(endpoint: str, model: str, messages: list[dict], tools: list[dict] | None = None,
         temperature: float = 0.2, top_p: float = 0.9, timeout: int = 180) -> dict:
    """One non-streaming chat turn. Returns the assistant message dict.

    The dict has keys: role, content, and optionally tool_calls
    (list of {"function": {"name": str, "arguments": dict}}).
    """
    payload: dict = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": {"temperature": temperature, "top_p": top_p},
    }
    if tools:
        payload["tools"] = tools
    req = urllib.request.Request(
        f"{endpoint}/api/chat",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:500]
        raise OllamaError(f"HTTP {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise OllamaError(f"cannot reach {endpoint}: {e.reason}") from e
    msg = body.get("message", {})
    # Normalize tool_calls: ensure arguments is a dict.
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function", {})
        args = fn.get("arguments", {})
        if isinstance(args, str):
            try:
                fn["arguments"] = json.loads(args)
            except json.JSONDecodeError:
                fn["arguments"] = {"_raw": args}
    return msg


def to_ollama_tools(tool_defs: list[dict]) -> list[dict]:
    """Convert our tool definitions to Ollama's tools format."""
    return [
        {"type": "function", "function": {
            "name": t["name"],
            "description": t["description"],
            "parameters": t["parameters"],
        }}
        for t in tool_defs
    ]
