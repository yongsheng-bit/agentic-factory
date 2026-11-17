#!/usr/bin/env python3
"""Cheap-tier agent: run a coding agent loop against a local OpenAI-compatible
endpoint (e.g. the llama.cpp / vLLM server on the 3090 box).

Point config.agent.cheap.command at this file. It reads the prompt file, then
runs a plain tool loop: the model asks for a shell command, we run it in the
worktree, we feed the output back, until it says DONE.

Stdlib only. No API key needed on a LAN endpoint.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request

BASE = os.environ.get("LLM_BASE", "http://192.168.68.73:8080/v1")
MODEL = os.environ.get("LLM_MODEL", "qwen3.8-flash-next-unsloth-ud-iq4_xs")
MAX_STEPS = int(os.environ.get("LLM_MAX_STEPS", "12"))
TIMEOUT = int(os.environ.get("LLM_TIMEOUT", "300"))

SYSTEM = (
    "You are an autonomous coding agent inside a git worktree. You may only "
    "modify files in the current directory.\n"
    "To act, reply with ONE fenced block:\n```sh\n<one shell command>\n```\n"
    "You will receive its output. When the task is complete, reply with DONE.\n"
    "Never run git commit, git push, or anything that touches the network."
)


def chat(messages):
    body = json.dumps({"model": MODEL, "messages": messages, "temperature": 0.2,
                       "max_tokens": 1200}).encode()
    req = urllib.request.Request(f"{BASE.rstrip('/')}/chat/completions", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        data = json.loads(r.read().decode())
    msg = data["choices"][0]["message"]["content"]
    usage = data.get("usage") or {}
    return msg, int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0))


def extract_cmd(text):
    for chunk in text.split("```")[1:]:
        lines = chunk.splitlines()
        if not lines:
            continue
        if lines[0].strip().lower() in ("sh", "bash", "shell", ""):
            cmd = "\n".join(lines[1:]).strip()
            if cmd:
                return cmd
    return None


def main():
    if len(sys.argv) < 2:
        print("usage: local_llm.py <prompt-file>", file=sys.stderr)
        return 2
    with open(sys.argv[1], "r", encoding="utf-8") as fh:
        prompt = fh.read()

    messages = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": prompt}]
    tin = tout = 0
    for step in range(MAX_STEPS):
        try:
            text, a, b = chat(messages)
        except Exception as exc:
            print(json.dumps({"status": "error", "error": str(exc),
                              "input_tokens": tin, "output_tokens": tout}))
            return 1
        tin += a
        tout += b
        messages.append({"role": "assistant", "content": text})
        if "DONE" in text.upper() and "```" not in text:
            break
        cmd = extract_cmd(text)
        if not cmd:
            messages.append({"role": "user", "content": "No command found. Reply with a "
                             "```sh block or DONE."})
            continue
        try:
            r = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=300)
            out = ((r.stdout or "") + (r.stderr or ""))[:4000]
        except Exception as exc:
            out = f"command failed: {exc}"
        messages.append({"role": "user", "content": f"$ {cmd}\n{out}"})

    # Emit in the shape factory/agent.py parses for cost.
    print(json.dumps({"result": "completed", "input_tokens": tin,
                      "output_tokens": tout, "steps": step + 1}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
