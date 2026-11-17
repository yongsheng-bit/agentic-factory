"""Security gate. Fails closed on secrets, open (warn) on advisory-only findings.

The diagram this implements had no supply-chain gate at all. This file is the
reason it now has one: an autonomous agent that pulls packages and writes
commits needs a CVE check and, above all, a secret scanner.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess

# Fallback scanner used when gitleaks is not installed. Deliberately broad:
# a false positive that costs a human 10 seconds is cheaper than a leaked key.
SECRET_PATTERNS = [
    (re.compile(r"AKIA[0-9A-Z]{16}"), "AWS access key id"),
    (re.compile(r"(?i)aws_secret_access_key\s*[:=]\s*['\"][A-Za-z0-9/+=]{30,}"), "AWS secret key"),
    (re.compile(r"ghp_[A-Za-z0-9]{36}"), "GitHub PAT"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{22,}"), "GitHub fine-grained PAT"),
    (re.compile(r"sk-[A-Za-z0-9]{20,}"), "OpenAI-style API key"),
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"), "Anthropic API key"),
    (re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"), "Slack token"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "private key"),
    (re.compile(r"(?i)\b(password|passwd|secret|api_key|apikey|token)\b\s*[:=]\s*['\"][^'\"]{8,}['\"]"),
     "hardcoded credential"),
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{20,}"), "bearer token"),
]

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build",
             ".next", "target", ".mypy_cache", "vendor"}

MAX_FILE_BYTES = 1_500_000


def _which(cmd):
    return shutil.which(cmd)


def added_lines(diff):
    """Yield (file, line_no, text) for every line a unified diff ADDS.

    A PR gate should scan what the PR introduces. Scanning the whole checkout
    instead turns any pre-existing fixture - including the fake AWS key in this
    repo's own secrets-gate test - into a finding that blocks every future PR
    from that repo forever.
    """
    out, cur, ln = [], None, 0
    for raw in diff.splitlines():
        if raw.startswith("+++ "):
            cur = raw[4:].strip()
            if cur.startswith("b/"):
                cur = cur[2:]
        elif raw.startswith("@@"):
            m = re.search(r"\+(\d+)", raw)
            ln = int(m.group(1)) - 1 if m else 0
        elif raw.startswith("+++") or raw.startswith("---"):
            continue
        elif raw.startswith("+"):
            ln += 1
            out.append((cur, ln, raw[1:]))
        elif not raw.startswith("-"):
            ln += 1
    return out


def scan_added(diff):
    """Secret findings restricted to the lines a diff adds."""
    findings = []
    for f, line, text in added_lines(diff):
        for rx, why in SECRET_PATTERNS:
            m = rx.search(text)
            if m:
                findings.append({"tool": "regex", "reason": why, "file": f,
                                 "line": line, "match": m.group(0)[:12] + "..."})
    return findings


def scan_secrets(root):
    """Return list of findings. Uses gitleaks when available, regex always."""
    findings = []
    if _which("gitleaks"):
        r = subprocess.run(["gitleaks", "detect", "--source", root, "--no-banner",
                            "--report-format", "json", "--exit-code", "0"],
                           capture_output=True, text=True, timeout=600)
        if r.returncode == 0 and "Leaks found" in (r.stdout + r.stderr):
            findings.append({"tool": "gitleaks", "detail": "gitleaks reported leaks"})

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".git")]
        for fn in filenames:
            p = os.path.join(dirpath, fn)
            try:
                if os.path.getsize(p) > MAX_FILE_BYTES:
                    continue
                with open(p, "r", encoding="utf-8", errors="ignore") as fh:
                    text = fh.read()
            except OSError:
                continue
            for rx, why in SECRET_PATTERNS:
                for m in rx.finditer(text):
                    line = text[:m.start()].count("\n") + 1
                    findings.append({
                        "tool": "regex", "reason": why,
                        "file": os.path.relpath(p, root), "line": line,
                        "match": m.group(0)[:12] + "..."})
    return findings


def scan_dependencies(root):
    """Advisory findings. Returns (findings, tools_run)."""
    findings, tools = [], []
    runs = []
    if _which("osv-scanner"):
        runs.append(("osv-scanner", ["osv-scanner", "scan", "."], root))
    if os.path.exists(os.path.join(root, "package.json")) and _which("npm"):
        runs.append(("npm-audit", ["npm", "audit", "--json"], root))
    if (os.path.exists(os.path.join(root, "requirements.txt"))
            or os.path.exists(os.path.join(root, "pyproject.toml"))) and _which("pip-audit"):
        runs.append(("pip-audit", ["pip-audit", "-r", "requirements.txt"]
                     if os.path.exists(os.path.join(root, "requirements.txt"))
                     else ["pip-audit", "."], root))

    for name, argv, cwd in runs:
        tools.append(name)
        try:
            r = subprocess.run(argv, capture_output=True, text=True, timeout=900,
                               cwd=cwd or root)
            if r.returncode not in (0,):
                findings.append({"tool": name,
                                 "detail": (r.stdout or r.stderr)[:800]})
        except (subprocess.TimeoutExpired, OSError) as exc:
            findings.append({"tool": name, "detail": f"could not run: {exc}"})
    return findings, tools


def check(root, diff=None, fail_on_secret=True):
    """Run the gate. Returns dict with ok + findings.

    With `diff` (the PR's own unified diff) only the added lines are scanned -
    that is what this change introduces. Without it the whole tree is walked,
    which is the right thing for `factory audit` and the wrong thing for a PR
    gate: a fixture that already exists on main would block every PR forever.
    """
    root = os.path.abspath(root)
    secrets = scan_added(diff) if diff is not None else scan_secrets(root)
    deps, tools = scan_dependencies(root)
    return {
        "ok": not (secrets and fail_on_secret),
        "secrets": secrets,
        "scope": "diff" if diff is not None else "tree",
        "dependencies": deps,
        "tools": tools or ["regex-secret-scanner"],
    }
