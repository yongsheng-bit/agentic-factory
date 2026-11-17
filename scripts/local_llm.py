#!/usr/bin/env python3
"""Cheap-tier agent: a tool-coding loop against a local OpenAI-compatible server.

Why native tool calling instead of the earlier fenced-```sh protocol: this
server (llama.cpp serving a Qwen GGUF) returns real `tool_calls` with valid JSON
arguments, so we get a parseable contract for free. Text-protocol parsing was
the most fragile part of the system, and shell-only tools made the agent behave
differently on Windows than on Linux.

Safety posture
--------------
* Every path is resolved and must land inside the worktree. No escapes, no
  writes into .git or the orchestrator's own directories.
* File tools are implemented in Python, not shell, so behaviour is identical on
  Windows and Linux.
* run_command is the one shell surface: capped, cwd-pinned, and it refuses
  VCS-mutating, destructive and network commands. The orchestrator owns commits
  and pushes; an agent that can `git push` is an agent that can ship.
* Step and token budgets are hard, because a small model in a loop is limited
  only by how long you let it be wrong.

Usage:  python local_llm.py <prompt-file>
Env:    LLM_BASE, LLM_MODEL, LLM_MAX_STEPS, LLM_MAX_TOKENS, LLM_TIMEOUT
Exit:   0 after finish(), 1 on error/budget. Prints a JSON summary that
        factory/agent.py parses for token counts and cost.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

BASE = os.environ.get("LLM_BASE", "http://192.168.68.73:8080/v1").rstrip("/")
MODEL = os.environ.get("LLM_MODEL", "qwen3.8-flash-next-unsloth-ud-iq4_xs")
MAX_STEPS = int(os.environ.get("LLM_MAX_STEPS", "20"))
MAX_TOTAL_TOKENS = int(os.environ.get("LLM_MAX_TOKENS", "60000"))
TIMEOUT = int(os.environ.get("LLM_TIMEOUT", "240"))
TEMPERATURE = float(os.environ.get("LLM_TEMPERATURE", "0.2"))
MAX_TOOL_CHARS = 6000
MAX_FILES_LISTED = 200

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".factory-worktrees",
             "dist", "build", ".next", ".pytest_cache", ".mypy_cache", "vendor", ".idea"}

# The orchestrator commits and pushes. The agent must not.
DENY = [
    (r"^\s*git\s+(commit|push|pull|fetch|checkout|switch|reset|clean|merge|rebase|apply|rm)\b",
     "git history is owned by the orchestrator"),
    (r"\brm\s+-[rf]{1,2}\b", "recursive delete"),
    (r"\bdel\s+/s\b|\brmdir\s+/s\b", "recursive delete"),
    (r"\bformat\s+[a-z]:", "disk format"),
    (r"\b(curl|wget|Invoke-WebRequest|iwr)\b", "network access"),
    (r"\b(ssh|scp|rsync|\bnc)\b", "remote access"),
    (r"\b(shutdown|taskkill|Stop-Process)\b", "process/system control"),
    (r"\b(sudo|runas|net\s+user)\b", "privilege change"),
    (r"\bmkfs\b|\bdd\s+if=", "device write"),
]

SYSTEM = """You are an autonomous coding agent working inside one git worktree. \
Your working directory IS the whole repository you may touch.

Rules:
- Use the tools. Never describe a file you have not read with read_file.
- Read before you write. Do not guess at file contents.
- Make the smallest change that satisfies the task. Do not refactor neighbours.
- Do NOT run git commands, install packages, or use the network. The orchestrator \
commits, pushes and opens the pull request.
- Verify with run_command when tests exist (python -m pytest, npm test, go test, ...).
- When the task is complete, call finish with a one-paragraph summary.
- If the task is impossible or the bug does not reproduce, call finish and say so \
plainly. A correct "cannot do this, because X" beats a fabricated change.
"""

TOOLS = [
    {"type": "function", "function": {
        "name": "read_file",
        "description": "Read a text file from the repository. Returns numbered lines.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "path relative to the repo root"},
            "offset": {"type": "integer", "description": "1-indexed first line (optional)"},
            "limit": {"type": "integer", "description": "max lines (optional)"}},
            "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "write_file",
        "description": "Overwrite a file with complete new contents. Prefer edit_file for "
                       "small changes to large files.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"},
            "content": {"type": "string", "description": "the ENTIRE new file contents"}},
            "required": ["path", "content"]}}},
    {"type": "function", "function": {
        "name": "edit_file",
        "description": "Replace one exact occurrence of old_text with new_text.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"},
            "old_text": {"type": "string", "description": "must match exactly, once"},
            "new_text": {"type": "string"}},
            "required": ["path", "old_text", "new_text"]}}},
    {"type": "function", "function": {
        "name": "list_files",
        "description": "List repository files, optionally filtered by a glob like "
                       "'**/*.py'. Paths only.",
        "parameters": {"type": "object", "properties": {
            "pattern": {"type": "string"}}}}},
    {"type": "function", "function": {
        "name": "search",
        "description": "Search file contents for a literal substring. Returns file:line.",
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string"},
            "glob": {"type": "string", "description": "optional filename glob"}},
            "required": ["text"]}}},
    {"type": "function", "function": {
        "name": "run_command",
        "description": "Run a build/test command in the repo root. No git, no network.",
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string"}}, "required": ["command"]}}},
    {"type": "function", "function": {
        "name": "finish",
        "description": "End the task. Call this exactly once when done.",
        "parameters": {"type": "object", "properties": {
            "summary": {"type": "string"},
            "changed_files": {"type": "array", "items": {"type": "string"}}},
            "required": ["summary"]}}},
]


class Ctx:
    def __init__(self, root):
        self.root = os.path.realpath(root)
        self.touched = set()

    def resolve(self, path):
        p = os.path.realpath(os.path.join(self.root, str(path).lstrip("/\\")))
        if p != self.root and not p.startswith(self.root + os.sep):
            raise ValueError(f"path escapes the worktree: {path}")
        rel = os.path.relpath(p, self.root).replace("\\", "/")
        if rel.startswith(".git") or rel.startswith(".factory-worktrees"):
            raise ValueError(f"refusing to touch VCS/orchestrator internals: {rel}")
        return p, rel


CTX = Ctx(os.getcwd())


def _resolve(path):
    """Resolve or return an error string. Tools must never raise: an exception
    that escapes a tool kills the whole agent run, while an error string at
    least lets the model correct itself."""
    try:
        return CTX.resolve(path), None
    except ValueError as exc:
        return None, f"error: {exc}"


def _glob_match(rel, pat):
    """fnmatch has no real `**`, so `**/*.py` never matches a root-level file.
    Test the stripped and prefixed forms too."""
    cands = {pat}
    if pat.startswith("**/"):
        cands.add(pat[3:])
    else:
        cands.add("**/" + pat)
    return any(fnmatch.fnmatch(rel, c) for c in cands)


# ------------------------------------------------------------------ tools

def t_read(a):
    r, err = _resolve(a.get("path", ""))
    if err:
        return err
    p, rel = r
    if not os.path.isfile(p):
        return f"error: not a file: {rel}"
    lines = open(p, "r", encoding="utf-8", errors="replace").read().splitlines()
    off = max(1, int(a.get("offset") or 1))
    lim = int(a.get("limit") or 400)
    chunk = lines[off - 1:off - 1 + lim]
    body = "\n".join(f"{off + i:>5}| {l}" for i, l in enumerate(chunk))
    more = f"\n[{len(lines) - (off - 1 + lim)} more lines]" if len(lines) > off - 1 + lim else ""
    return (body + more)[:MAX_TOOL_CHARS] or "(empty file)"


def t_write(a):
    r, err = _resolve(a.get("path", ""))
    if err:
        return err
    p, rel = r
    os.makedirs(os.path.dirname(p) or CTX.root, exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="") as fh:
        fh.write(str(a.get("content", "")))
    CTX.touched.add(rel)
    return f"wrote {rel} ({len(str(a.get('content', '')))} chars)"


def t_edit(a):
    r, err = _resolve(a.get("path", ""))
    if err:
        return err
    p, rel = r
    if not os.path.isfile(p):
        return f"error: not a file: {rel}"
    text = open(p, "r", encoding="utf-8", errors="replace").read()
    old, new = str(a.get("old_text", "")), str(a.get("new_text", ""))
    if not old:
        return "error: old_text is empty"
    n = text.count(old)
    if n == 0:
        return (f"error: old_text not found in {rel}. Read the file again and copy the exact "
                f"text including indentation.")
    if n > 1:
        return f"error: old_text matches {n} times in {rel}; include more context."
    with open(p, "w", encoding="utf-8", newline="") as fh:
        fh.write(text.replace(old, new, 1))
    CTX.touched.add(rel)
    return f"edited {rel}"


def _walk():
    for d, dirs, files in os.walk(CTX.root):
        dirs[:] = [x for x in dirs if x not in SKIP_DIRS and not x.startswith(".")]
        for f in files:
            yield os.path.relpath(os.path.join(d, f), CTX.root).replace("\\", "/")


def t_list(a):
    pat = a.get("pattern") or "**/*"
    out = [r for r in _walk() if _glob_match(r, pat)]
    head = sorted(out)[:MAX_FILES_LISTED]
    return "\n".join(head) + (f"\n[{len(out) - len(head)} more]" if len(out) > len(head) else "")


def t_search(a):
    needle = str(a.get("text", ""))
    glob = a.get("glob")
    hits = []
    for rel in _walk():
        if glob and not _glob_match(rel, glob):
            continue
        p = os.path.join(CTX.root, rel)
        try:
            if os.path.getsize(p) > 2_000_000:
                continue
            for i, line in enumerate(open(p, "r", encoding="utf-8", errors="replace"), 1):
                if needle in line:
                    hits.append(f"{rel}:{i}: {line.strip()[:160]}")
                    if len(hits) >= 80:
                        return "\n".join(hits) + "\n[truncated at 80 matches]"
        except (OSError, UnicodeError):
            continue
    return "\n".join(hits) or f"no matches for {needle!r}"


def t_run(a):
    cmd = str(a.get("command", "")).strip()
    for rx, why in DENY:
        if re.search(rx, cmd, re.I):
            return f"error: command refused ({why}): {cmd[:120]}"
    try:
        r = subprocess.run(cmd, shell=True, cwd=CTX.root, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=300)
    except subprocess.TimeoutExpired:
        return "error: command timed out after 300s"
    except OSError as exc:
        return f"error: {exc}"
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    return f"exit {r.returncode}\n{out[-MAX_TOOL_CHARS:] or '(no output)'}"


DISPATCH = {"read_file": t_read, "write_file": t_write, "edit_file": t_edit,
            "list_files": t_list, "search": t_search, "run_command": t_run}


# ------------------------------------------------------------------ loop

def chat(messages, attempts=3):
    body = json.dumps({"model": MODEL, "messages": messages, "tools": TOOLS,
                       "tool_choice": "auto", "temperature": TEMPERATURE,
                       "max_tokens": 1600}).encode()
    last = None
    for i in range(attempts):
        try:
            req = urllib.request.Request(f"{BASE}/chat/completions", data=body,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return json.loads(r.read())
        except Exception as exc:                 # transient reset / server busy
            last = exc
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"LLM request failed after {attempts} tries: {last}")


def repo_tree(limit=120):
    files = sorted(_walk())
    shown = files[:limit]
    return "\n".join(shown) + (f"\n[{len(files) - len(shown)} more files]"
                               if len(files) > len(shown) else "")


def main():
    if len(sys.argv) < 2:
        print("usage: local_llm.py <prompt-file>", file=sys.stderr)
        return 2
    task = open(sys.argv[1], "r", encoding="utf-8", errors="replace").read()

    messages = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": f"## Repository files\n{repo_tree()}\n\n{task}"}]

    tin = tout = steps = 0
    summary = ""
    finished = False
    seen = set()

    for steps in range(1, MAX_STEPS + 1):
        try:
            resp = chat(messages)
        except RuntimeError as exc:
            print(json.dumps({"status": "error", "error": str(exc), "steps": steps,
                              "input_tokens": tin, "output_tokens": tout}))
            return 1
        u = resp.get("usage") or {}
        tin += int(u.get("prompt_tokens", 0))
        tout += int(u.get("completion_tokens", 0))
        msg = resp["choices"][0]["message"]
        calls = msg.get("tool_calls") or []

        if not calls:
            # No tool call: nudge once, then accept prose as the final answer.
            if steps >= MAX_STEPS - 1:
                summary = (msg.get("content") or "").strip()
                break
            messages.append({"role": "assistant", "content": msg.get("content") or ""})
            messages.append({"role": "user",
                             "content": "Call a tool now. Use finish when the task is done."})
            continue

        messages.append({"role": "assistant", "content": msg.get("content") or "",
                         "tool_calls": calls})

        for c in calls:
            name = c["function"]["name"]
            raw = c["function"].get("arguments") or "{}"
            try:
                args = json.loads(raw) if isinstance(raw, str) else dict(raw)
            except json.JSONDecodeError:
                args, result = {}, f"error: malformed JSON arguments: {raw[:200]}"
            else:
                if name == "finish":
                    finished = True
                    summary = str(args.get("summary", ""))
                    for f in args.get("changed_files") or []:
                        try:
                            CTX.touched.add(CTX.resolve(f)[1])
                        except ValueError:
                            pass
                    result = "finished"
                elif name in DISPATCH:
                    key = (name, json.dumps(args, sort_keys=True)[:400])
                    if key in seen:
                        result = ("error: you already called this with identical arguments. "
                                  "Do something different or call finish.")
                    else:
                        seen.add(key)
                        try:
                            result = DISPATCH[name](args)
                        except ValueError as exc:
                            result = f"error: {exc}"
                        except Exception as exc:
                            result = f"error in {name}: {exc}"
                else:
                    result = f"error: unknown tool {name!r}"

            messages.append({"role": "tool", "tool_call_id": c.get("id", ""),
                             "name": name, "content": str(result)[:MAX_TOOL_CHARS]})

        if finished:
            break
        if tin + tout > MAX_TOTAL_TOKENS:
            summary = "stopped: token budget exceeded"
            break

    print(json.dumps({"status": "completed" if finished else "budget",
                      "result": summary[:2000], "steps": steps,
                      "changed_files": sorted(CTX.touched),
                      "input_tokens": tin, "output_tokens": tout}))
    return 0 if finished else 1


if __name__ == "__main__":
    sys.exit(main())
