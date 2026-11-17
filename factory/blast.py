"""Deterministic blast-radius classification.

Deliberately NOT a model. The blast-radius decision gates whether a human ever
sees a change, so its trigger must be the most reliable component available,
not the least. Path rules are auditable, testable and boring.

Anything that cannot be decided from the diff is escalated, never defaulted to
"low".
"""

from __future__ import annotations

import fnmatch
import re

HIGH_GLOBS = [
    "migrations/*", "*/migrations/*", "**/migrations/*", "alembic/*", "db/migrate/*",
    "*.sql", "**/schema.graphql", "schema.*",
    "**/auth/*", "**/authz/*", "**/permissions/*", "**/acl/*",
    "**/crypto/*", "**/payment*/*", "**/billing/*",
    ".github/workflows/*", ".github/actions/*", "**/Dockerfile*", "**/*.tf",
    "**/settings.py", "**/secrets*", "**/*.pem", "**/*.key",
]

MEDIUM_GLOBS = [
    "**/package-lock.json", "**/yarn.lock", "**/pnpm-lock.yaml",
    "**/requirements*.txt", "**/poetry.lock", "**/uv.lock", "**/go.sum",
    "**/api/*", "**/routes/*", "**/models/*",
]

SUSPICIOUS = [
    (re.compile(r"(?i)\b(grant|revoke|bypass|disable)\b.*\b(auth|tls|verify|ssl|rbac|mfa)\b"),
     "auth/TLS relaxation"),
    (re.compile(r"(?i)ignore_errors|except\s*:\s*$|#\s*noqa|eslint-disable"),
     "error suppression"),
    (re.compile(r"(?i)(drop|truncate|delete\s+from)\s"), "destructive data operation"),
    (re.compile(r"(?i)hardcod\w*\s.*(key|secret|password|token)"), "hardcoded credential"),
]

DEFAULTS = {
    "high_files": 25,
    "high_loc": 800,
    "medium_files": 8,
    "medium_loc": 250,
}


def _match(path: str, globs) -> bool:
    """Glob match that works at repo root and at any depth.

    fnmatch treats `**` as `*`, so `**/foo.json` would miss a root-level
    foo.json. We add the stripped and basename candidates explicitly, but only
    when the final path segment is literal — adding `*` as a candidate for
    `**/auth/*` would match every file in the repo.
    """
    p = path.replace("\\", "/")
    for g in globs:
        cands = [g, "**/" + g]
        if g.startswith("**/"):
            cands.append(g[3:])
            last = g.rstrip("/").split("/")[-1]
            if "*" not in last and "?" not in last:
                cands.append(last)
        for c in cands:
            if fnmatch.fnmatch(p, c):
                return True
    return False


def classify(changed_files, loc_added=0, loc_deleted=0, diff_text="", cfg=None):
    """Return (level, reasons). level in {'low','medium','high'}."""
    c = dict(DEFAULTS)
    if cfg:
        c.update(cfg)
    reasons = []
    files = [f for f in changed_files if f]
    loc = int(loc_added) + int(loc_deleted)

    for f in files:
        if _match(f, HIGH_GLOBS):
            reasons.append(f"touches {f}")
    if len(files) >= c["high_files"]:
        reasons.append(f"{len(files)} files changed (>= {c['high_files']})")
    if loc >= c["high_loc"]:
        reasons.append(f"{loc} lines changed (>= {c['high_loc']})")

    if reasons:
        return "high", "; ".join(reasons[:6])

    for f in files:
        if _match(f, MEDIUM_GLOBS):
            reasons.append(f"dependency/API surface: {f}")
            break
    if len(files) >= c["medium_files"]:
        reasons.append(f"{len(files)} files changed")
    if loc >= c["medium_loc"]:
        reasons.append(f"{loc} lines changed")
    if diff_text:
        for rx, why in SUSPICIOUS:
            if rx.search(diff_text):
                reasons.append(why)

    if reasons:
        return "medium", "; ".join(reasons[:6])
    return "low", "no high/medium triggers"


def parse_name_status(porcelain_diff: str):
    """Parse `git diff --name-status` into a list of paths."""
    out = []
    for line in porcelain_diff.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) >= 2:
            out.append(parts[-1].split(" -> ")[-1].strip())
    return out
