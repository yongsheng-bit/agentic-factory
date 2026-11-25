"""Prompt audit: what do today's models no longer need from our instructions?

Instruction files bloat the way system prompts did: emphasis markers and
prose rules that were written for weaker models drift newer ones off
course, and this repo's own rule is that a rule earns its place only where
code enforces it. The audit reads the repo's instruction files and reports
prune candidates - it never edits anything.

Findings are plain dicts so `factory audit --json` can be parsed. The CLI
exits 2 when there are findings: the same block-and-fix convention the
test gate uses, so an audit can be wired into any check pipeline.
"""

import os
import re

INSTRUCTION_FILES = ("AGENTS.md", "CLAUDE.md", "GEMINI.md", ".cursorrules")

SIZE_BUDGET_LINES = 80
EMPHASIS_BUDGET = 3

EMPHASIS = re.compile(r"\bIMPORTANT\b|\bNEVER\b|\bALWAYS\b|\bMUST\b|!!")
RULE = re.compile(r"(?i)\b(must|never|always|don'?t|do not|only)\b")
ENFORCER = re.compile(r"(?i)\b(tests?|unittest|gate|check|ci|lint|scanner|"
                      r"enforce|\.py|tools/)\b")
BULLET = re.compile(r"^(-|\*|\d+\.)\s")

IGNORE_FILE = ".factoryauditignore"


def _load_ignore(root):
    path = os.path.join(root, IGNORE_FILE)
    if not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8", errors="ignore") as fh:
        return [ln.strip() for ln in fh if ln.strip() and not ln.startswith("#")]


def _ignored(rel, patterns):
    norm = rel.replace("\\", "/")
    return any(pat in norm for pat in patterns)


def audit_instructions(root):
    """Return prune-candidate findings for the repo's instruction files."""
    findings = []
    seen = {}
    for name in INSTRUCTION_FILES:
        path = os.path.join(root, name)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8", errors="ignore") as fh:
                lines = fh.read().splitlines()
        except OSError:
            continue

        emphasis = sum(len(EMPHASIS.findall(line)) for line in lines)
        if emphasis > EMPHASIS_BUDGET:
            findings.append({
                "kind": "emphasis-bloat", "file": name, "line": 0,
                "detail": f"{emphasis} emphasis markers (IMPORTANT/NEVER/"
                          f"ALWAYS/!!); newer models drift on emphasis - "
                          f"prune to what the code cannot already see"})

        if len(lines) > SIZE_BUDGET_LINES:
            findings.append({
                "kind": "over-budget", "file": name, "line": len(lines),
                "detail": f"{len(lines)} lines over the {SIZE_BUDGET_LINES}"
                          f"-line budget; keep only what the model cannot "
                          f"see from the code"})

        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if not BULLET.match(stripped):
                continue
            if RULE.search(stripped) and not ENFORCER.search(stripped):
                findings.append({
                    "kind": "unenforced-rule", "file": name, "line": i,
                    "detail": stripped[:100] + " - names no gate, check, or "
                          "test; a rule earns its place only where code "
                          "enforces it"})
            seen.setdefault(stripped.lower(), []).append((name, i))

    for key, where in seen.items():
        files = sorted({name for name, _ in where})
        if len(files) > 1:
            name, line = where[0]
            findings.append({
                "kind": "duplicate-rule", "file": name, "line": line,
                "detail": f"identical rule in {len(files)} instruction "
                          f"files ({', '.join(files)}): {key[:80]}"})
    return findings


def audit_repo(root, scan_secrets_fn=None):
    """Secrets over the whole tree plus the instruction-file audit.

    Tree-wide secrets include the fake keys the secret gate tests carry on
    purpose; a repo triages those once in .factoryauditignore instead of
    re-reading them every run - noise gets muted, findings get fixed.
    """
    if scan_secrets_fn is None:
        from . import gates
        scan_secrets_fn = gates.scan_secrets
    ignore = _load_ignore(root)
    findings = []
    for f in scan_secrets_fn(root):
        rel = f.get("file", "")
        if _ignored(rel, ignore):
            continue
        findings.append({"kind": "secret", "file": rel, "line": f.get("line", 0),
                         "detail": f.get("reason", f.get("detail", ""))})
    findings.extend(audit_instructions(root))
    return findings
