"""Agent runner: model-tier routing, wall-clock cap, process-tree kill, cost capture.

Two things this file exists to prevent:
  1. A runaway retry loop turning a $5 job into a $400 job (wall-clock cap +
     cost parsed from every run and handed back to the worker).
  2. A killed-but-not-dead agent. Killing the shell leaves the agent's own
     children alive; on Windows that means an orphaned `node` holding the
     worktree. We kill the whole tree.
"""

from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import sys
import tempfile
import time

PROMPT_FILE_TOKEN = "{prompt_file}"
PROMPT_TOKEN = "{prompt}"


def expand_env(cfg):
    """Replace "env:NAME" strings with the value of environment variable NAME.

    Secrets (GitHub tokens, webhook URLs) live in the environment, not in
    factory.json, so the config file can be committed and pasted into a
    ticket without carrying credentials. A missing variable expands to the
    empty string: the tool that needs it then fails loudly and locally,
    which beats a literal "env:FACTORY_GH_TOKEN" being sent as a password.
    """
    if isinstance(cfg, dict):
        return {k: expand_env(v) for k, v in cfg.items()}
    if isinstance(cfg, list):
        return [expand_env(v) for v in cfg]
    if isinstance(cfg, str) and cfg.startswith("env:"):
        return os.environ.get(cfg[4:], "")
    return cfg


def load_config(path):
    with open(path, "r", encoding="utf-8") as fh:
        return expand_env(json.load(fh))


TIERS = ("cheap", "strong")


def _field(ticket, name, default=None):
    if ticket is None:
        return default
    if isinstance(ticket, dict):
        value = ticket.get(name, default)
    else:
        try:
            value = ticket[name]
        except (IndexError, KeyError):
            return default
    return default if value is None else value


def tier_decision(cfg, ticket):
    """Pick a model tier and say why. Returns (tier, reason).

    A routing input has to exist at routing time. Blast radius is computed from
    the diff, which only exists after the agent already ran, so it could never
    have been the primary signal: every ticket rated low and the strong tier was
    unreachable except by hand. Precedence now:

      1. an explicit per-ticket tier (`factory add --tier strong`) - a human who
         already knows this one is dangerous;
      2. blast medium/high - declared at add time, or carried in from a previous
         attempt's diff, where it really is an input to the retry decision;
      3. escalation - any attempt after the first runs on the strong tier;
      4. cheap.

    Escalation is the load-bearing rule: it costs one cheap failure per ticket,
    needs no predictor, and a failed cheap attempt is the only honest evidence
    that a task was beyond the cheap model. Guessing difficulty from ticket prose
    mis-routes silently and forever.
    """
    tiers = (cfg or {}).get("agent", {}) or {}
    if "cheap" not in tiers or "strong" not in tiers:
        return "strong", "single-tier config"

    explicit = _field(ticket, "tier")
    if explicit in TIERS:
        return explicit, "explicit override"

    blast = _field(ticket, "blast", "low")
    if blast in ("medium", "high"):
        return "strong", f"blast={blast}"

    attempts = int(_field(ticket, "attempts", 0))
    escalate_after = int(tiers.get("escalate_after_attempts", 1))
    if attempts >= escalate_after:
        return "strong", f"escalated after {attempts} failed attempt(s)"

    return "cheap", "default"


def pick_tier(cfg, ticket):
    """Tier only, for callers that do not care why."""
    return tier_decision(cfg, ticket)[0]


def build_argv(spec, prompt, prompt_path):
    raw = spec.get("command")
    if isinstance(raw, str):
        argv = shlex.split(raw, posix=(os.name != "nt"))
    else:
        argv = list(raw)
    out = []
    for a in argv:
        if PROMPT_FILE_TOKEN in a:
            out.append(a.replace(PROMPT_FILE_TOKEN, prompt_path))
        elif PROMPT_TOKEN in a:
            out.append(a.replace(PROMPT_TOKEN, prompt))
        else:
            out.append(a)
    if not any(PROMPT_FILE_TOKEN in a or PROMPT_TOKEN in a for a in argv):
        out.append(prompt)
    return out


def write_prompt(workdir, ticket, attempt, extra="", criteria="", out_dir=None):
    """Write the prompt OUTSIDE the worktree.

    A prompt written inside the checkout becomes an untracked file, which means
    `git add -A` commits it and every agent PR ships its own instructions. The
    first version of this did exactly that; the smoke test caught it.
    """
    out_dir = out_dir or os.path.join(tempfile.gettempdir(), "agentic-factory-prompts")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"ticket-{ticket['id']}-attempt-{attempt}.md")
    body = (
        f"# Task {ticket['ref'] or ticket['id']}: {ticket['title']}\n\n"
        f"{ticket['body']}\n\n"
        f"## Rules\n"
        f"- You are attempt {attempt} of {ticket['max_retries']} in an automated pipeline.\n"
        f"- Work only inside the current directory. Do not touch other checkouts.\n"
        f"- Do not commit, push, or open pull requests; the orchestrator does that.\n"
        f"- If the task is ambiguous or unreproducible, say so explicitly and stop.\n"
        f"- Keep the diff minimal and consistent with existing style.\n"
        + (f"\n## Acceptance criteria\n{criteria}\n" if criteria else "")
        + (f"\n## Previous failure\n{extra}\n" if extra else "")
    )
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)
    return path


def _kill_tree(proc):
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, timeout=60)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _extract_cost(stdout_text, spec):
    """Parse cost from agent output; fall back to token-based estimate."""
    text = stdout_text.strip()
    if not text:
        return 0.0, 0, 0
    blob = None
    try:
        blob = json.loads(text)
    except json.JSONDecodeError:
        for line in reversed(text.splitlines()):
            line = line.strip()
            if line.startswith("{") and line.endswith("}"):
                try:
                    blob = json.loads(line)
                    break
                except json.JSONDecodeError:
                    continue
    if not isinstance(blob, dict):
        return 0.0, 0, 0

    for k in ("total_cost_usd", "cost_usd", "cost"):
        v = blob.get(k)
        if isinstance(v, (int, float)):
            return float(v), 0, 0

    usage = blob.get("usage") or blob.get("token_usage") or {}

    tin = int(usage.get("input_tokens") or usage.get("prompt_tokens")
              or blob.get("input_tokens") or blob.get("prompt_tokens") or 0)
    tout = int(usage.get("output_tokens") or usage.get("completion_tokens")
               or blob.get("output_tokens") or blob.get("completion_tokens") or 0)
    pin = float(spec.get("price_per_mtok_in", 0.0))
    pout = float(spec.get("price_per_mtok_out", 0.0))
    est = (tin * pin + tout * pout) / 1_000_000.0
    return est, tin, tout


ENV_ALLOWLIST = (
    "PATH", "PATHEXT", "COMSPEC", "SYSTEMROOT", "WINDIR", "TEMP", "TMP",
    "TMPDIR", "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "LANG", "LC_ALL",
    "TZ", "PYTHONIOENCODING", "PYTHONUTF8", "PAGER", "GIT_PAGER",
    "GIT_CONFIG_NOSYSTEM", "GIT_TERMINAL_PROMPT",
)


def minimal_env(base=None, allow_extra=(), spec_env=None, env_extra=None):
    """Build a child environment from an allowlist instead of inheriting everything.

    The agent is untrusted code with a shell. Handing it `dict(os.environ)` means
    every credential the orchestrator or its operator happens to carry - GitHub
    tokens, webhooks, cloud keys, database URLs - is one `env` away from the diff
    it writes. A model that decides to exfiltrate, or a ticket that tricks it into
    printing the environment into a commit, then leaks the operator's account
    rather than the ticket's.

    Allowlist, not denylist: a denylist must anticipate every secret, and new
    ones appear the moment someone exports something. Names are matched
    case-insensitively because Windows spells its variables however the installer
    felt that day (`Path`, not `PATH`), while the original spelling is preserved
    so the child sees what it expects.

    `spec_env` and `env_extra` are applied unconditionally afterwards: those are
    the factory deliberately configuring this run, not the ambient environment.
    """
    base = os.environ if base is None else base
    allow = {n.upper() for n in ENV_ALLOWLIST}
    allow |= {n.upper() for n in allow_extra}
    env = {name: value for name, value in base.items() if name.upper() in allow}
    for src in (spec_env, env_extra):
        if src:
            env.update(src)
    return env


def run(cfg, spec, prompt, workdir, prompt_path=None, timeout_s=None, env_extra=None):
    """Run one agent attempt. Returns dict(status, cost_usd, stdout, stderr, seconds, pid)."""
    spec = dict(spec)
    if timeout_s is None:
        timeout_s = int(spec.get("timeout_s", 1800))
    argv = build_argv(spec, prompt, prompt_path or "")
    allow_extra = ((cfg or {}).get("agent", {}) or {}).get("env_allowlist", [])
    env = minimal_env(allow_extra=allow_extra, spec_env=spec.get("env", {}) or {},
                      env_extra=env_extra)

    t0 = time.time()
    try:
        popen_kwargs = dict(cwd=workdir, env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace")
        if os.name != "nt":
            popen_kwargs["start_new_session"] = True
        proc = subprocess.Popen(argv, **popen_kwargs)
    except FileNotFoundError:
        return {"status": "blocked", "cost_usd": 0.0, "stdout": "",
                "stderr": f"agent command not found: {argv[0]}",
                "seconds": 0.0, "pid": None}

    try:
        out, err = proc.communicate(timeout=timeout_s)
        status = "ok" if proc.returncode == 0 else "fail"
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        try:
            out, err = proc.communicate(timeout=30)
        except Exception:
            out, err = "", ""
        return {"status": "timeout", "cost_usd": 0.0, "stdout": out or "",
                "stderr": (err or "") + f"\nwall-clock cap {timeout_s}s exceeded",
                "seconds": time.time() - t0, "pid": proc.pid}

    cost, tin, tout = _extract_cost(out or "", spec)
    return {"status": status, "cost_usd": cost, "tokens_in": tin, "tokens_out": tout,
            "stdout": (out or "")[-8000:], "stderr": (err or "")[-4000:],
            "seconds": time.time() - t0, "pid": proc.pid}
