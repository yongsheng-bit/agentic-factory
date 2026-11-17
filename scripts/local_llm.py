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
import shlex
import shutil
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

SKIP_DIRS = {".git", ".factory-tmp", "node_modules", ".venv", "venv", "__pycache__", ".factory-worktrees",
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
- Verify with run_command when tests exist (python -m pytest, npm test, go test, ...). Run ONE command per call: there is no shell, so no pipes, no redirection, no &&.
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
        "description": "Run ONE build/test command in the repo root (no shell: no pipes, redirection or &&; no git, no network).",
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
        # lstrip("/\\") used to turn the POSIX absolute path "/etc/passwd" into
        # the worktree-relative "etc/passwd": the model asked for one file and
        # silently got another, with no refusal anywhere. Linux CI caught it.
        # Absolute paths are now allowed only when they land inside the worktree.
        raw = str(path)
        cand = raw if os.path.isabs(raw) else os.path.join(self.root, raw)
        p = os.path.realpath(cand)
        if p != self.root and not p.startswith(self.root + os.sep):
            raise ValueError(f"path escapes the worktree: {path} "
                             f"(use a path relative to the repo root)")
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
    with open(p, "r", encoding="utf-8", errors="replace") as fh:
        lines = fh.read().splitlines()
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
    with open(p, "r", encoding="utf-8", errors="replace") as fh:
        text = fh.read()
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
            with open(p, "r", encoding="utf-8", errors="replace") as fh:
                for i, line in enumerate(fh, 1):
                    if needle in line:
                        hits.append(f"{rel}:{i}: {line.strip()[:160]}")
                        if len(hits) >= 80:
                            return "\n".join(hits) + "\n[truncated at 80 matches]"
        except (OSError, UnicodeError):
            continue
    return "\n".join(hits) or f"no matches for {needle!r}"


def _run_env():
    """Environment for agent commands: an allowlist, nothing else.

    The orchestrator may well run with GITHUB_TOKEN, a cloud key or a registry
    password in its environment. A child inherits everything by default, so
    `run_command("set")` would hand the model a credential it has no business
    reading. Anything a build genuinely needs goes in LLM_RUN_ENV.
    """
    keep = {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "HOME", "USERPROFILE",
            "USERNAME", "LANG", "LC_ALL", "TZ", "PYTHONHOME", "PYTHONIOENCODING",
            "PYTHONPATH", "VIRTUAL_ENV", "CONDA_PREFIX", "CONDA_DEFAULT_ENV",
            "JAVA_HOME", "GOPATH", "GOROOT", "CARGO_HOME", "RUSTUP_HOME", "NVM_BIN",
            "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "OS", "MSYSTEM", "SHELL"}
    for extra in os.environ.get("LLM_RUN_ENV", "").split(os.pathsep):
        if extra.strip():
            keep.add(extra.strip())
    env = {k: v for k, v in os.environ.items() if k.upper() in keep and v is not None}
    # Keep scratch files inside the worktree: a test run that writes its cache
    # to %TEMP% outside the tree is invisible to the commit, and one that writes
    # inside is at least caught by ARTIFACT_EXCLUDES.
    scratch = os.path.join(CTX.root, ".factory-tmp")
    os.makedirs(scratch, exist_ok=True)
    env["TEMP"] = env["TMP"] = env["TMPDIR"] = scratch
    return env


# Shell metacharacters: with shell=False these are literal arguments, so a
# model reaching for a pipe needs telling, not a mysterious exit code.
SHELL_OPS = re.compile(r"[<>|;&`]|\$\(")


def _unquoted(cmd):
    """Command line with quoted spans removed. An operator inside an argument is
    data (`python -c "import os; ..."`); outside one it is shell syntax."""
    return re.sub(r"\"[^\"]*\"|'[^']*'", "", cmd)

_ABS_PATH = re.compile(r"^(?:[A-Za-z]:[\\/]|[\\\\]{2}|/)")


def _argv(cmd):
    """Split a command line without a shell.

    posix=False on every platform. Posix mode EATS backslashes, so on Linux
    `python ..\\evil.py` arrived as the token `..evil.py` and the escape check
    never saw the '..'. Keeping the raw characters means the guard inspects what
    the model actually typed; we strip the surrounding quotes ourselves.
    """
    args = [a.strip('"\'') for a in shlex.split(cmd, posix=False)]
    return [a for a in args if a != ""]


def _arg_escapes(arg):
    """Reject arguments that name a path outside the worktree.

    This is a heuristic, not a wall: `pytest ../shared` is stopped, a program
    that resolves a path itself is not. Real isolation means a container.
    """
    if arg.startswith("-"):                      # flags: -o, --cov=src, ...
        arg = arg.split("=", 1)[-1].lstrip("=")
        if not arg:
            return None
    norm = arg.replace("\\", "/")
    if ".." in norm.split("/"):
        return "contains '..'"
    if _ABS_PATH.match(arg):
        try:
            p = os.path.realpath(arg)
        except OSError:
            return "unresolvable absolute path"
        root = CTX.root
        if p != root and not p.startswith(root + os.sep):
            return "names a path outside the worktree"
    return None


def t_run(a):
    cmd = str(a.get("command", "")).strip()
    for rx, why in DENY:
        if re.search(rx, cmd, re.I):
            return f"error: command refused ({why}): {cmd[:120]}"
    m = SHELL_OPS.search(_unquoted(cmd))
    if m:
        return (f"error: no shell available — '{m.group(0)}' is not supported. "
                f"Run one command at a time; stdout and stderr are already merged.")
    try:
        args = _argv(cmd)
    except ValueError as exc:
        return f"error: cannot parse command ({exc})"
    if not args:
        return "error: empty command"
    for arg in args[1:]:
        why = _arg_escapes(arg)
        if why:
            return f"error: argument '{arg[:80]}' {why}; work inside the repository"
    env = _run_env()
    # npm/npx/yarn and pip-installed consoles are .cmd/.bat shims on Windows, and
    # CreateProcess will not run those without cmd.exe. Metacharacters are already
    # rejected above, so cmd here sees a fixed argv, not a command line.
    resolved = shutil.which(args[0], path=env.get("PATH")) or args[0]
    if resolved.lower().endswith((".cmd", ".bat")):
        args = ["cmd", "/c", resolved] + args[1:]
    else:
        args = [resolved] + args[1:]
    try:
        r = subprocess.run(args, cwd=CTX.root, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=300,
                           env=env)
    except subprocess.TimeoutExpired:
        return "error: command timed out after 300s"
    except FileNotFoundError:
        return f"error: '{args[0]}' is not installed or not on PATH"
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
